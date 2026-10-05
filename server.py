"""FastAPI backend: serves the report UI and proxies Claude calls so the API key never reaches the browser."""
import csv
import io
import json
import os
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import demo
import sizing

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
MAX_AGENT_STEPS = 6
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_10_MIN", "40"))
BASE = Path(__file__).parent

app = FastAPI(title="24x7 Support Sizing")
_hits = defaultdict(deque)


def demo_forced():
    """Demo mode answers from saved logic. It is on when DEMO_MODE is set or no API key exists."""
    return os.environ.get("DEMO_MODE", "").lower() in ("1", "true", "yes") or not os.environ.get("ANTHROPIC_API_KEY")


def get_client():
    import anthropic
    return anthropic.Anthropic(timeout=25.0, max_retries=1)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    """Protects the API key on a public URL: a small per-IP cap on AI endpoints."""
    if request.url.path.startswith("/api/") and request.url.path != "/api/health":
        ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "x")).split(",")[0].strip()
        q, now = _hits[ip], time.time()
        while q and now - q[0] > 600:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return JSONResponse({"detail": "Too many requests. Wait a few minutes and try again."}, status_code=429)
        q.append(now)
    return await call_next(request)


@app.get("/api/health")
def health():
    return {"ok": True, "claude_configured": bool(os.environ.get("ANTHROPIC_API_KEY")), "demo": demo_forced(), "model": MODEL}


# ---------- deterministic endpoints ----------

class Scenario(BaseModel):
    params: dict = Field(default_factory=dict)
    levers: list[float] | None = None
    mode: str = "comp"


@app.post("/api/size")
def api_size(s: Scenario):
    return {"size": sizing.size(s.params), "projection": sizing.project(s.params, s.levers, s.mode)}


@app.post("/api/upload-csv")
async def upload_csv(file: UploadFile = File(...)):
    """Reads a ticket export and returns annual volumes. Needs a 'type' column (incident / sr / cr)
    and, optionally, a date column ('opened', 'date' or 'created') so volumes can be annualised."""
    raw = await file.read()
    if len(raw) > 5_000_000:
        raise HTTPException(413, "File is larger than 5 MB.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(400, "The CSV has no header row.")
    cols = {c.strip().lower(): c for c in reader.fieldnames}
    type_col = next((cols[c] for c in ("type", "ticket_type", "category") if c in cols), None)
    if not type_col:
        raise HTTPException(400, "Add a 'type' column with values like incident, sr or cr.")
    date_col = next((cols[c] for c in ("opened", "date", "created", "created_at") if c in cols), None)
    counts = {"inc": 0, "sr": 0, "cr": 0}
    dates = []
    for row in reader:
        t = (row.get(type_col) or "").strip().lower()
        if t.startswith("inc"):
            counts["inc"] += 1
        elif t in ("sr", "service request", "request") or t.startswith("serv"):
            counts["sr"] += 1
        elif t in ("cr", "change", "change request") or t.startswith("chg") or t.startswith("change"):
            counts["cr"] += 1
        else:
            continue
        if date_col and row.get(date_col):
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"):
                try:
                    dates.append(datetime.strptime(row[date_col].strip(), fmt))
                    break
                except ValueError:
                    continue
    rows = sum(counts.values())
    if rows == 0:
        raise HTTPException(400, "No incident, sr or cr rows were found in the 'type' column.")
    span = (max(dates) - min(dates)).days + 1 if len(dates) > 1 else None
    factor = 365 / span if span and span > 0 else 1
    return {
        "rows": rows, "span_days": span, "annualised": bool(span),
        "raw_counts": counts,
        "annual": {k: round(v * factor) for k, v in counts.items()},
    }


# ---------- Claude: structured validation ----------

class ValidateReq(BaseModel):
    params: dict = Field(default_factory=dict)
    levers: list[float] | None = None
    mode: str = "comp"
    assumptions: str = Field(default="", max_length=6000)


REVIEW_TOOL = {
    "name": "report_review",
    "description": "Report the review of this capacity plan.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "description": "One or two sentences: is the plan sound to present?"},
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                        "title": {"type": "string"},
                        "detail": {"type": "string", "description": "What is wrong and why it matters, citing the numbers."},
                        "suggestion": {"type": "string", "description": "A concrete fix."},
                    },
                    "required": ["severity", "title", "detail", "suggestion"],
                },
            },
        },
        "required": ["verdict", "issues"],
    },
}

REVIEW_SYSTEM = (
    "You are a senior capacity-planning reviewer for IT service desks. You are given the model inputs, "
    "the computed results (already calculated by code; do not redo the arithmetic) and the author's written "
    "assumptions. Find internal contradictions, unrealistic values and missing considerations. Be specific and "
    "cite numbers. Return at most 7 issues, most severe first. Do not invent facts that are not in the input."
)


@app.post("/api/validate")
def api_validate(r: ValidateReq):
    if demo_forced():
        return {"review": demo.validate(r.params, r.levers, r.mode), "demo": True}
    try:
        return _validate_live(r)
    except Exception:
        return {"review": demo.validate(r.params, r.levers, r.mode), "demo": True, "demo_reason": "Claude could not be reached"}


def _validate_live(r: ValidateReq):
    client = get_client()
    result = sizing.size(r.params)
    payload = {
        "inputs": result["params"],
        "results": {k: result[k] for k in ("effort_person_days", "fte", "rounded_design", "recommended_headcount", "implied_hours_per_ticket")},
        "projection": sizing.project(r.params, r.levers, r.mode),
        "automated_checks": sizing.check_inputs(r.params),
        "author_assumptions": r.assumptions or "(none provided)",
    }
    resp = client.messages.create(
        model=MODEL, max_tokens=1800, system=REVIEW_SYSTEM,
        tools=[REVIEW_TOOL], tool_choice={"type": "tool", "name": "report_review"},
        messages=[{"role": "user", "content": json.dumps(payload)}],
    )
    for b in resp.content:
        if b.type == "tool_use":
            return {"review": b.input, "automated_checks": payload["automated_checks"]}
    raise HTTPException(502, "Claude returned no review.")


# ---------- Claude: executive summary ----------

@app.post("/api/narrative")
def api_narrative(s: Scenario):
    if demo_forced():
        return {"text": demo.narrative(s.params, s.levers, s.mode), "demo": True}
    try:
        return _narrative_live(s)
    except Exception:
        return {"text": demo.narrative(s.params, s.levers, s.mode), "demo": True, "demo_reason": "Claude could not be reached"}


def _narrative_live(s: Scenario):
    client = get_client()
    payload = {"size": sizing.size(s.params), "projection": sizing.project(s.params, s.levers, s.mode)}
    resp = client.messages.create(
        model=MODEL, max_tokens=900,
        system=("You write executive summaries for staffing proposals. Use only the figures provided. "
                "Write three short paragraphs: the headline recommendation, how the workload splits across levels "
                "and locations, and the five-year outlook with the main risk. Plain prose, no headings, no bullet points."),
        messages=[{"role": "user", "content": json.dumps(payload)}],
    )
    return {"text": "".join(b.text for b in resp.content if b.type == "text").strip()}


# ---------- Claude: tool-using agent ----------

PARAMS_SCHEMA = {
    "type": "object",
    "description": "Overrides applied on top of the reference case. Keys: " + "; ".join(f"{k} = {v}" for k, v in sizing.PARAM_DOC.items()),
    "properties": {k: {"type": "number"} for k in sizing.PARAM_DOC},
}
LEVERS_SCHEMA = {"type": "array", "items": {"type": "number"}, "minItems": 5, "maxItems": 5,
                 "description": "Productivity lever percent for years 1 to 5."}

TOOLS = [
    {"name": "calculate_sizing",
     "description": "Compute annual effort, FTE by level and location, rounded design and recommended headcount for a set of inputs.",
     "input_schema": {"type": "object", "properties": {"params": PARAMS_SCHEMA}, "required": ["params"]}},
    {"name": "project_years",
     "description": "Compute year 1 to 5 headcount after automation levers. mode 'comp' compounds levers, 'cum' adds them.",
     "input_schema": {"type": "object", "properties": {
         "params": PARAMS_SCHEMA, "levers": LEVERS_SCHEMA, "mode": {"type": "string", "enum": ["comp", "cum"]}},
         "required": ["params"]}},
    {"name": "validate_inputs",
     "description": "Run deterministic sanity checks on a set of inputs.",
     "input_schema": {"type": "object", "properties": {"params": PARAMS_SCHEMA}, "required": ["params"]}},
]

AGENT_SYSTEM = (
    "You are the planning assistant inside a 24x7 application-support sizing tool. The user is exploring a staffing "
    "model. The reference case is 12,000 incidents, 2,000 service requests and 50 change requests a year (high "
    "complexity, 10% L1 / 90% L2, 15% onsite, 15% overhead), which gives 30.88 FTE of effort and a recommended "
    "headcount of 34 including a 3 FTE relief buffer. Never do the arithmetic yourself: always call the tools and "
    "quote their numbers. For what-if questions, run the tool for the changed case and, when useful, the reference case, "
    "then explain the difference in two to five sentences. Name the assumptions you changed. Keep answers short and plain."
)


def exec_tool(name, args, state):
    params = (args or {}).get("params") or {}
    if name == "calculate_sizing":
        state["scenario"] = {"params": sizing.clean(params), "levers": state["scenario"]["levers"], "mode": state["scenario"]["mode"]}
        return sizing.size(params)
    if name == "project_years":
        levers = args.get("levers") or sizing.LEVERS
        mode = args.get("mode") or "comp"
        state["scenario"] = {"params": sizing.clean(params), "levers": [float(x) for x in levers], "mode": mode}
        return sizing.project(params, levers, mode)
    if name == "validate_inputs":
        return {"checks": sizing.check_inputs(params)}
    return {"error": f"unknown tool {name}"}


class Msg(BaseModel):
    role: str
    content: str = Field(max_length=4000)


class AgentReq(BaseModel):
    messages: list[Msg]
    scenario: Scenario | None = None


@app.post("/api/agent")
def api_agent(r: AgentReq):
    if not r.messages or r.messages[-1].role != "user":
        raise HTTPException(400, "Send a user message.")
    base = r.scenario or Scenario()
    cur = {"params": sizing.clean(base.params), "levers": base.levers or sizing.LEVERS, "mode": base.mode}
    if demo_forced():
        return {**demo.agent(r.messages[-1].content, cur), "demo": True}
    try:
        return _agent_live(r)
    except HTTPException:
        raise
    except Exception:
        return {**demo.agent(r.messages[-1].content, cur), "demo": True, "demo_reason": "Claude could not be reached"}


def _agent_live(r: AgentReq):
    client = get_client()
    msgs = [{"role": m.role, "content": m.content} for m in r.messages[-10:] if m.role in ("user", "assistant")]
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "Send a user message.")
    base = r.scenario or Scenario()
    state = {"scenario": {"params": sizing.clean(base.params), "levers": base.levers or sizing.LEVERS, "mode": base.mode}}
    system = AGENT_SYSTEM + "\nThe user's current dashboard inputs are: " + json.dumps(state["scenario"])
    trace, changed = [], False
    for _ in range(MAX_AGENT_STEPS):
        resp = client.messages.create(model=MODEL, max_tokens=1500, system=system, tools=TOOLS, messages=msgs)
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            return {"reply": text, "trace": trace, "scenario": state["scenario"] if changed else None}
        msgs.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                out = exec_tool(b.name, b.input, state)
                if b.name in ("calculate_sizing", "project_years"):
                    changed = True
                trace.append({"tool": b.name, "input": b.input})
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out)})
        msgs.append({"role": "user", "content": results})
    return {"reply": "I could not finish that within the step limit. Try a narrower question.", "trace": trace, "scenario": None}


# ---------- static frontend ----------

@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
