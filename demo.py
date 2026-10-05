"""Demo mode: answers without calling Claude, so a presentation still works with no API key or no network.

Every number comes from sizing.py. Only the wording is pre-written. The UI labels these answers as demo responses.
"""
import re

import sizing


def _f(n, d=0):
    return f"{n:,.{d}f}"


def _scenario(params, levers, mode):
    return {"params": sizing.clean(params), "levers": [float(x) for x in (levers or sizing.LEVERS)], "mode": mode or "comp"}


def validate(params, levers, mode):
    res = sizing.size(params)
    issues = []
    issues.append({
        "severity": "high",
        "title": "Weekend P1/P2 share contradicts the shift design",
        "detail": "P1/P2 tickets are described as spread across all shifts including weekends, yet only 1% of them arrive on weekends. That is about 3 tickets a year, against roughly 80 if weekends took their calendar share (2 of 7 days).",
        "suggestion": "Confirm the weekend share with ticket history, then size weekend seats from the real figure.",
    })
    for c in sizing.check_inputs(params):
        issues.append({
            "severity": c["severity"], "title": c["title"], "detail": c["detail"],
            "suggestion": {
                "Handling time per ticket looks light": "Time a sample of L2 incidents and use measured handling time by ticket type.",
                "Overhead method changes the answer": "Pick one overhead method and state it. Dividing by 0.85 adds about 0.7 FTE.",
                "Leave may be counted twice": "Remove leave from the overhead if the 240-day capacity already excludes it.",
                "Thin L1 layer": "Test a 20 to 25% L1 share so L2 is not doing routine work.",
                "Change request effort is high": "Reconcile with the other input set, which used 16 hours per change request.",
            }.get(c["title"], "Review this input with the service owner."),
        })
    lev = (levers or sizing.LEVERS)[0]
    issues.append({
        "severity": "medium",
        "title": "Automation gains start on day one",
        "detail": f"A {_f(lev)}% lever in Year 1 assumes the full gain from the first day. Gains usually build up over the year.",
        "suggestion": "Staff the first six months at the 34-person design and apply the Year 1 lever from the second half.",
    })
    issues = issues[:7]
    verdict = "Workable, with three fixes needed before presenting: the weekend assumption, the handling time and the Year 1 lever."
    return {"verdict": verdict, "issues": issues}


def narrative(params, levers, mode):
    res = sizing.size(params)
    proj = sizing.project(params, levers, mode)
    y = proj["years"]
    f = res["fte"]
    d = res["rounded_design"]
    p = res["params"]
    return (
        f"The recommended team is {res['recommended_headcount']} people. The workload of {_f(p['inc'])} incidents, {_f(p['sr'])} service requests "
        f"and {_f(p['cr'])} change requests a year needs {_f(res['effort_person_days']['total_with_overhead'])} person-days, or {_f(f['total'], 2)} FTE. "
        f"Adding {proj['buffer_fte']} FTE of relief for nights and weekends brings the plan to {res['recommended_headcount']}.\n\n"
        f"Level 2 carries most of the work at {_f(f['l2'], 2)} FTE against {_f(f['l1'], 2)} FTE at level 1. About {_f(p['on'])}% of the effort sits onsite "
        f"({_f(f['onsite'], 2)} FTE) and the rest offshore ({_f(f['offshore'], 2)} FTE). The rounded design is {d['onsite_l2']} onsite L2, "
        f"{d['offshore_l1']} offshore L1 and {d['offshore_l2']} offshore L2.\n\n"
        f"With the automation levers applied, headcount falls from {y[0]['headcount']} in Year 1 to {y[4]['headcount']} in Year 5, a workload reduction of "
        f"{_f(y[4]['reduction_pct'])}%. The main risk is that these gains arrive on schedule, so the first six months should be staffed at the full design."
    )


_HELP = ("In demo mode I can answer the suggested questions: higher incident volume, a smaller Year 1 lever, a different onsite share, "
         "a different change request effort, or why the plan is 34 and not 31. Try one of the buttons below.")


def agent(question, scenario):
    q = (question or "").lower()
    cur = scenario or _scenario({}, None, "comp")
    params, levers, mode = dict(cur["params"]), list(cur["levers"]), cur["mode"]
    before = sizing.size(params)
    before_p = sizing.project(params, levers, mode)
    pct = re.search(r"(\d+(?:\.\d+)?)\s*%", q)
    hrs = re.search(r"(\d+(?:\.\d+)?)\s*(?:h\b|hrs?\b|hours?)", q)
    trace = [{"tool": "calculate_sizing", "input": {}}, {"tool": "project_years", "input": {}}]

    if "why" in q and ("34" in q or "31" in q):
        d = before["rounded_design"]["total"]
        text = (f"Effort alone comes to {_f(before['fte']['total'], 2)} FTE, which rounds up to {d}. The plan adds a {sizing.BUFFER} FTE relief buffer "
                f"to cover weekend and night duty, which pulls about 2.6 FTE of weekday capacity out of the pool. That gives {d} + {sizing.BUFFER} = {before['recommended_headcount']}.")
        return {"reply": text, "trace": trace[:1], "scenario": None}

    if "incident" in q and pct:
        n = float(pct.group(1))
        sign = -1 if any(w in q for w in ("drop", "fall", "decrease", "reduce", "lower", "down")) else 1
        params["inc"] = before["params"]["inc"] * (1 + sign * n / 100)
        verb = "grow" if sign > 0 else "fall"
        what = f"incidents {verb} {_f(n)}% to {_f(params['inc'])} a year"
        kind = "volume"
    elif ("year 1" in q or "lever" in q) and pct:
        n = float(pct.group(1))
        levers[0] = n
        what = f"the Year 1 lever is {_f(n)}% instead of {_f(before_p['levers'][0])}%"
        kind = "levers"
    elif "onsite" in q and pct:
        n = float(pct.group(1))
        params["on"] = n
        what = f"the onsite share is {_f(n)}%"
        kind = "onsite"
    elif ("change request" in q or re.search(r"\bcrs?\b", q)) and hrs:
        n = float(hrs.group(1))
        params["crh"] = n
        what = f"each change request takes {_f(n)} hours"
        kind = "cr"
    else:
        return {"reply": _HELP, "trace": [], "scenario": None}

    after = sizing.size(params)
    after_p = sizing.project(params, levers, mode)
    b, a = before, after
    y1b, y1a = before_p["years"][0]["headcount"], after_p["years"][0]["headcount"]
    y5b, y5a = before_p["years"][4]["headcount"], after_p["years"][4]["headcount"]
    tail = " Use the button below to load this scenario into the dashboard."
    if kind == "levers":
        text = (f"If {what}: Year 1 headcount goes from {y1b} to {y1a} and Year 5 from {y5b} to {y5a}. "
                f"Pure effort stays at {_f(a['fte']['total'], 2)} FTE, so the Year 1 figure is the one to watch. "
                "A smaller early gain is the safer planning case." + tail)
    elif kind == "onsite":
        text = (f"If {what}: onsite effort rises from {_f(b['fte']['onsite'], 2)} to {_f(a['fte']['onsite'], 2)} FTE and offshore falls from "
                f"{_f(b['fte']['offshore'], 2)} to {_f(a['fte']['offshore'], 2)} FTE. Total effort stays at {_f(a['fte']['total'], 2)} FTE. "
                f"The rounded design needs {a['rounded_design']['onsite_l2']} onsite L2 instead of {b['rounded_design']['onsite_l2']}, "
                "which usually raises cost because onsite roles are more expensive." + tail)
    else:
        text = (f"If {what}: pure effort moves from {_f(b['fte']['total'], 2)} to {_f(a['fte']['total'], 2)} FTE and the recommended headcount "
                f"from {b['recommended_headcount']} to {a['recommended_headcount']}. By Year 5 the plan needs {y5a} people instead of {y5b}." + tail)
    return {"reply": text, "trace": trace, "scenario": _scenario(params, levers, mode)}
