# 24x7 Support Sizing with Claude

A browser report app with a small Python backend. The backend holds your Anthropic API key and calls Claude, so the key never reaches the browser.

## What Claude does here

| Feature | How it works |
|---|---|
| Validation | Claude reviews the inputs, the computed results and your written assumptions, and returns structured issues (severity, detail, fix). |
| Executive summary | Claude writes three paragraphs from the current numbers. |
| Planning agent | Claude runs a tool-use loop. It calls `calculate_sizing`, `project_years` and `validate_inputs`, then explains the result. "Apply" loads its scenario into the dashboard. |
| Data fetch | Upload a ticket CSV; the server annualises volumes and fills the inputs. |

The arithmetic lives in `sizing.py`. Claude calls it as a tool and never calculates figures itself, so results are reproducible.

## Demo mode

If `ANTHROPIC_API_KEY` is missing, or Claude cannot be reached during a call, the app answers from saved logic in `demo.py` and labels the answers "demo response". The numbers still come from `sizing.py`. Set `DEMO_MODE=1` in Render to force it on, for example when the venue Wi-Fi is unreliable.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...   # Windows PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."
uvicorn server:app --reload
```

Open http://localhost:8000. The badge in the Claude section shows when the key is connected.

## Deploy on Render

1. Push this folder to a GitHub repository (the `.gitignore` keeps `.env` out).
2. In Render choose **New > Blueprint** and select the repository. It reads `render.yaml`.
3. When prompted, paste `ANTHROPIC_API_KEY`. Create an API key at https://console.anthropic.com and add some credit first.
4. Deploy. Your app is live at `https://support-sizing.onrender.com` (the name may differ).

Without a Blueprint: **New > Web Service**, build command `pip install -r requirements.txt`, start command `uvicorn server:app --host 0.0.0.0 --port $PORT`, and add the environment variable.

Notes for the demo:

- The free plan sleeps after about 15 minutes idle and takes up to a minute to wake. Open the URL a few minutes before presenting.
- Each AI endpoint is limited per visitor (`RATE_LIMIT_PER_10_MIN`, default 40) to protect your credit. Set a spending limit in the Anthropic console too.
- Change the model with the `ANTHROPIC_MODEL` environment variable.

## Suggested demo script

1. Load `static/sample_tickets.csv` and watch the inputs fill.
2. Click **Run validation** and walk through the issues Claude finds.
3. Ask the agent: "What if incidents grow 20% and the Year 1 lever is only 10%?" Show the tool chips, then **Apply** the scenario.
4. Click **Write summary** for the executive paragraph.

## Files

- `server.py` FastAPI app and Claude calls
- `sizing.py` deterministic model
- `static/index.html` the report UI
- `static/sample_tickets.csv` demo data
- `render.yaml` Render Blueprint
