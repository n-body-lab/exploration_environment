"""Trace collector for agent runtimes.

Agent containers have no access to the run directory, so their traces are
shipped here over HTTP. The collector stamps the agent identity from the
token (an agent cannot write events as someone else) and the receive time.

In transparency `tool` mode it also serves other agents' traces
(GET /traces/{agent_id}) and logs every read as `trace_read`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from hivemarket.common.config import ExperimentConfig, TransparencyConfig
from hivemarket.common.events import EventLog, JsonlSink, iso, make_event, now
from hivemarket.common.manifest import Manifest, bearer
from hivemarket.common.transparency import project

MAX_RECORDS_PER_READ = 50


def create_app(manifest: Manifest, run_dir: Path, transparency: TransparencyConfig | None = None) -> FastAPI:
    app = FastAPI(title="HiveMarket collector", docs_url=None, redoc_url=None)
    transparency = transparency or TransparencyConfig()
    traces = JsonlSink(run_dir / "logs" / "agent_traces.jsonl")
    own = EventLog("collector", manifest.run_id, JsonlSink(run_dir / "logs" / "collector.jsonl"))
    # agent_id -> [(event ts, projected record)], only kept when someone can read it
    readable: dict[str, list[tuple[float, dict[str, Any]]]] = {a.id: [] for a in manifest.agents}
    agent_ids = [a.id for a in manifest.agents]

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    def unauthorized(request: Request, path: str) -> JSONResponse:
        src_ip = request.client.host if request.client else None
        ip_agent = manifest.agent_by_ip(src_ip)
        own.emit("unauthenticated_request", agent_id=ip_agent.id if ip_agent else None, source_ip=src_ip, path=path)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    @app.post("/events")
    async def events(request: Request):
        src_ip = request.client.host if request.client else None
        agent = manifest.agent_by_token(bearer(request.headers))
        if agent is None:
            return unauthorized(request, "/events")
        payload = await request.json()
        received = now()
        batch = payload if isinstance(payload, list) else [payload]
        watched = transparency.mode == "tool" and transparency.watched_by_someone(agent.id, agent_ids)
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
            if watched:
                rec = project(out, transparency.expose)
                if rec:
                    readable[agent.id].append((out["ts"], rec))
        return {"ok": True, "accepted": len(batch)}

    @app.get("/traces/{target}")
    async def read_traces(target: str, request: Request, since: int = 0):
        reader = manifest.agent_by_token(bearer(request.headers))
        if reader is None:
            return unauthorized(request, f"/traces/{target}")
        if transparency.mode != "tool" or not transparency.can_see(reader.id, target):
            own.emit("trace_read_denied", agent_id=reader.id, reader=reader.id, target=target)
            return JSONResponse({"error": "forbidden", "message": f"You cannot read {target}'s traces."},
                                status_code=403)
        cutoff = now() - transparency.delay_seconds
        available = [rec for ts, rec in readable.get(target, []) if ts <= cutoff]
        since = max(0, since)
        records = available[since: since + MAX_RECORDS_PER_READ]
        next_cursor = since + len(records)
        own.emit("trace_read", agent_id=reader.id, reader=reader.id, target=target, mode="tool",
                 cursor=since, records=len(records), steps=[r.get("step") for r in records],
                 more=next_cursor < len(available))
        return {"agent": target, "records": records, "next_cursor": next_cursor,
                "more": next_cursor < len(available)}

    return app


def main() -> None:
    run_dir = Path(os.environ.get("RUN_DIR", "/run_data"))
    manifest = Manifest.load(run_dir / "manifest.json")
    cfg = ExperimentConfig.load(run_dir / "experiment.yaml")
    uvicorn.run(create_app(manifest, run_dir, cfg.transparency), host="0.0.0.0",
                port=int(os.environ.get("PORT", "8000")), log_level="warning")
