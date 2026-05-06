"""FastAPI dashboard server. Reads ~/.claude and ~/.claude-personal locally.

Run:
    pip install -r requirements.txt
    python app.py
    # open http://localhost:8765
"""
from __future__ import annotations

import os
from pathlib import Path

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
