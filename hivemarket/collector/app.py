"""Trace collector for agent runtimes.

Agent containers have no access to the run directory, so their traces are
shipped here over HTTP. The collector stamps the agent identity from the
token (an agent cannot write events as someone else) and the receive time.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from hivemarket.common.events import EventLog, JsonlSink, iso, make_event, now
from hivemarket.common.manifest import Manifest, bearer


def create_app(manifest: Manifest, run_dir: Path) -> FastAPI:
    app = FastAPI(title="HiveMarket collector", docs_url=None, redoc_url=None)
    traces = JsonlSink(run_dir / "logs" / "agent_traces.jsonl")
    own = EventLog("collector", manifest.run_id, JsonlSink(run_dir / "logs" / "collector.jsonl"))

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.post("/events")
    async def events(request: Request):
        src_ip = request.client.host if request.client else None
        agent = manifest.agent_by_token(bearer(request.headers))
        if agent is None:
            ip_agent = manifest.agent_by_ip(src_ip)
            own.emit("unauthenticated_request", agent_id=ip_agent.id if ip_agent else None,
                     source_ip=src_ip, path="/events")
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        payload = await request.json()
        received = now()
        batch = payload if isinstance(payload, list) else [payload]
        for ev in batch:
            if not isinstance(ev, dict):
                continue
            out = make_event("agent", str(ev.get("kind", "unknown")), ev.get("data") or {},
                             agent_id=agent.id, run_id=manifest.run_id, ts=float(ev.get("ts", received)))
            out["received_ts"] = received
            out["received_iso"] = iso(received)
            out["source_ip"] = src_ip
            if ev.get("claimed_agent_id") not in (None, agent.id):
                out["data"]["claimed_agent_id"] = ev["claimed_agent_id"]
            traces.write(out)
        return {"ok": True, "accepted": len(batch)}

    return app


def main() -> None:
    run_dir = Path(os.environ.get("RUN_DIR", "/run_data"))
    manifest = Manifest.load(run_dir / "manifest.json")
    uvicorn.run(create_app(manifest, run_dir), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
                log_level="warning")
