"""Observer UI: a live (and replayable) view of one experiment run.

    hivemarket ui [--runs-dir runs] [--port 8090]

Serves a single page (static/) and a small API over the run directories on this
machine. It reads the runs' JSONL logs directly, so it works while a run is going
(local or Docker) and without the observatory:

  GET /api/runs                    runs, newest first, with a live/finished status
  GET /api/runs/{id}               the run's agents (no credentials) and its game graph
  GET /api/runs/{id}/stream        Server-Sent Events: every event of every log file,
                                   backlog first, then new lines as they are written
  GET /api/runs/{id}/backchannel   the post-run backchannel reconstruction, if any

The page shows everything an experimenter can see, including private memory and
token contents, so the server binds to 127.0.0.1 by default. Agent containers sit on
an internal network with no route to the host. Agent and admin tokens are never sent.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

STATIC = Path(__file__).parent / "static"
RUN_ID = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_STRING = 4000  # longer strings (full LLM request bodies, payloads) are cut for the browser
MAX_LIST = 200
LIVE_SECONDS = 30  # a run whose logs changed this recently, and that hasn't finished, is "live"
POLL_SECONDS = 0.5
HEARTBEAT_SECONDS = 15
AGENT_FIELDS = ("id", "ip", "inbox_host", "inbox_port", "provider", "model")


def trim(value: Any, depth: int = 0) -> Any:
    """Bound an event's size for the browser: long strings and lists are cut, and marked."""
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + f"… [{len(value) - MAX_STRING} more chars]"
    if isinstance(value, list):
        items = [trim(v, depth + 1) for v in value[:MAX_LIST]]
        if len(value) > MAX_LIST:
            items.append(f"… [{len(value) - MAX_LIST} more items]")
        return items
    if isinstance(value, dict):
        if depth > 8:
            return "…"
        return {k: trim(v, depth + 1) for k, v in value.items()}
    return value


class LogTail:
    """Reads new complete lines from every logs/*.jsonl of a run, file by file, from saved offsets."""

    def __init__(self, logs: Path):
        self.logs = logs
        self.offsets: dict[str, int] = {}
        self.partial: dict[str, bytes] = {}

    def read(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in sorted(self.logs.glob("*.jsonl")):
            name = path.name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            start = self.offsets.get(name, 0)
            if size < start:  # truncated or replaced: start over
                start, self.partial[name] = 0, b""
            if size == start:
                continue
            with open(path, "rb") as f:
                f.seek(start)
                chunk = f.read(size - start)
            self.offsets[name] = size
            data = self.partial.get(name, b"") + chunk
            lines = data.split(b"\n")
            self.partial[name] = lines.pop()  # an incomplete last line waits for the next read
            for line in lines:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event["_file"] = name
                out.append(trim(event))
        return out


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def run_info(run_dir: Path) -> dict[str, Any] | None:
    manifest = _load_json(run_dir / "manifest.json")
    if not isinstance(manifest, dict):
        return None
    logs = list((run_dir / "logs").glob("*.jsonl"))
    last_write = max((p.stat().st_mtime for p in logs), default=manifest.get("created_at") or 0)
    finished = (run_dir / "final_state.json").exists() or (run_dir / "summary.json").exists()
    live = not finished and time.time() - last_write < LIVE_SECONDS
    return {
        "run_id": manifest.get("run_id", run_dir.name),
        "experiment": manifest.get("experiment"),
        "mode": manifest.get("mode"),
        "created_at": manifest.get("created_at"),
        "last_write": last_write,
        "agents": len(manifest.get("agents", [])),
        "status": "live" if live else ("finished" if finished else "stopped"),
    }


def create_app(runs_dir: Path) -> FastAPI:
    runs_dir = Path(runs_dir)
    app = FastAPI(title="HiveMarket observer", docs_url=None, redoc_url=None)

    def run_dir(run_id: str) -> Path:
        path = runs_dir / run_id
        if not RUN_ID.match(run_id) or not (path / "manifest.json").exists():
            raise HTTPException(404, f"no run {run_id!r} in {runs_dir}")
        return path

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/runs")
    async def runs() -> list[dict[str, Any]]:
        found = [run_info(p) for p in runs_dir.iterdir() if p.is_dir()] if runs_dir.exists() else []
        return sorted((r for r in found if r), key=lambda r: r["created_at"] or 0, reverse=True)

    @app.get("/api/runs/{run_id}")
    async def run(run_id: str) -> dict[str, Any]:
        path = run_dir(run_id)
        manifest = _load_json(path / "manifest.json") or {}
        world = _load_json(path / "world.json") or {}
        agents = [{k: a.get(k) for k in AGENT_FIELDS} for a in manifest.get("agents", [])]  # no tokens
        return {
            **(run_info(path) or {}),
            "agents": agents,
            "services": {k: {"ip": v.get("ip")} for k, v in (manifest.get("services") or {}).items()},
            "world": {k: world.get(k) for k in ("nodes", "edges", "communication_cost", "rounds", "rules", "ideal",
                                                "impossible")},
        }

    @app.get("/api/runs/{run_id}/backchannel")
    async def backchannel(run_id: str) -> JSONResponse:
        path = run_dir(run_id) / "analysis" / "backchannel.jsonl"
        if not path.exists():
            return JSONResponse({"available": False, "records": []})
        records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return JSONResponse({"available": True, "records": trim(records)})

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: str, request: Request) -> StreamingResponse:
        tail = LogTail(run_dir(run_id) / "logs")

        async def events() -> AsyncIterator[str]:
            last_beat = time.monotonic()
            yield "retry: 2000\n\n"
            while True:
                batch = tail.read()
                if batch:
                    # one SSE message per batch keeps the backlog of a long run quick to deliver
                    yield f"event: batch\ndata: {json.dumps(batch, default=str)}\n\n"
                    last_beat = time.monotonic()
                elif time.monotonic() - last_beat > HEARTBEAT_SECONDS:
                    yield ": heartbeat\n\n"
                    last_beat = time.monotonic()
                if await request.is_disconnected():
                    return
                await asyncio.sleep(POLL_SECONDS)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def main(runs_dir: str = "runs", host: str = "127.0.0.1", port: int = 8090) -> None:
    import uvicorn

    print(f"HiveMarket observer: http://{host}:{port}  (runs from {Path(runs_dir).resolve()})")
    uvicorn.run(create_app(Path(runs_dir)), host=host, port=port, log_level="warning")
