"""FastAPI dashboard server. Reads Claude Code local files — no API calls.

Run:
    pip install -r requirements.txt
    python app.py        # → http://localhost:8765

Config (optional .env or env vars):
    CLAUDE_HOMES=label:~/path,label:~/path   # override auto-detected paths
    PORT=8765                                 # default port
"""
from __future__ import annotations

import os
from pathlib import Path

# Load .env before importing core (HOMES is resolved at import time)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import core

app = FastAPI(title="Claude Usage Dashboard")

STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/stats")
def api_stats():
    return JSONResponse({
        "windows": core.windowed_stats(),
        "totals": core.aggregate_stats(),
        "by_agent_skill": core.aggregate_by_subagent_and_skill(),
        "by_model": core.aggregate_by_model(),
        "rolling_5h": core.rolling_5h_usage(),
        "plan_usage": core.fetch_oauth_usage(),
        "homes": core.homes_info(),
    })


@app.get("/api/sessions")
def api_sessions(limit: int = 50):
    return JSONResponse(core.list_sessions_enriched(limit=limit))


@app.get("/api/sessions/{session_id}")
def api_session_detail(session_id: str):
    info = core.session_detail(session_id)
    if info is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(info)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8765"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
