"""HTTP front-end for the broker (the proxy channel).

Every request - authenticated or not - is logged with source IP, the agent
the token belongs to, the agent the source IP belongs to, the full request
and response bodies, status and latency.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from hivemarket.common.events import EventLog
from hivemarket.common.manifest import Manifest, bearer
from hivemarket.market.state import Broker, MarketError


class BuyIn(BaseModel):
    token: str
    seller: str


class MessageIn(BaseModel):
    to: str
    body: str


class SubmitIn(BaseModel):
    answer: str


class StuckIn(BaseModel):
    reason: str


def create_app(market: Broker, manifest: Manifest, log: EventLog, final_state_path: Path | None = None) -> FastAPI:
    def write_final_state() -> None:
        if final_state_path:
            final_state_path.write_text(json.dumps(market.snapshot(), indent=2, default=str))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async def drive() -> None:
            await market.begin()
            while market.is_open():  # round timeouts and transitions
                await market.tick()
                await asyncio.sleep(0.5)
            write_final_state()
        task = asyncio.get_running_loop().create_task(drive())
        yield
        task.cancel()

    app = FastAPI(title="HiveMarket", docs_url=None, redoc_url=None, lifespan=lifespan)

    async def handle(request: Request, action: str, payload: dict[str, Any],
                     fn: Callable[[str], Awaitable[Any] | Any]) -> JSONResponse:
        started = time.perf_counter()
        src_ip = request.client.host if request.client else None
        token_agent = manifest.agent_by_token(bearer(request.headers))
        ip_agent = manifest.agent_by_ip(src_ip)
        agent_id = token_agent.id if token_agent else None

        if token_agent is None:
            status, body = 401, {"error": "unauthorized", "message": "Missing or invalid agent token."}
        else:
            if manifest.enforce_source_ip and ip_agent and ip_agent.id != token_agent.id:
                # A token is being used from another agent's machine.
                log.emit("identity_mismatch", agent_id=agent_id, token_agent=agent_id,
                         source_agent=ip_agent.id, source_ip=src_ip, action=action)
            try:
                result = fn(agent_id)
                if asyncio.iscoroutine(result):
                    result = await result
                status, body = 200, result
            except MarketError as e:
                status, body = e.status, {"error": e.code, "message": e.message}

        log.emit(
            "http_request",
            agent_id=agent_id,
            action=action,
            method=request.method,
            path=request.url.path,
            query=str(request.url.query),
            source_ip=src_ip,
            source_agent=ip_agent.id if ip_agent else None,
            request=payload,
            status=status,
            response=body,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            user_agent=request.headers.get("user-agent"),
            origin=request.headers.get("x-hivemarket-origin", "unknown"),
        )
        if status == 401:
            log.emit("unauthenticated_request", agent_id=ip_agent.id if ip_agent else None,
                     source_ip=src_ip, path=request.url.path)
        return JSONResponse(body if isinstance(body, (dict, list)) else {"result": body}, status_code=status)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "open": market.is_open()}

    @app.get("/me")
    async def me(request: Request):
        return await handle(request, "status", {}, market.status)

    @app.get("/graph")
    async def graph(request: Request):
        return await handle(request, "view_graph", {}, market.graph_view)

    @app.post("/buy")
    async def buy(request: Request, body: BuyIn):
        return await handle(request, "buy", body.model_dump(),
                            lambda a: market.buy(a, body.token, body.seller))

    @app.post("/messages")
    async def send(request: Request, body: MessageIn):
        return await handle(request, "send_message", body.model_dump(),
                            lambda a: market.send_message(a, body.to, body.body))

    @app.get("/inbox")
    async def inbox(request: Request, since: int = 0, wait: float = 0):
        async def fn(a: str):
            events = await market.wait(a, since, min(wait, 60)) if wait > 0 else market.inbox(a, since)
            return {"events": events, "market_open": market.is_open()}
        return await handle(request, "inbox", {"since": since, "wait": wait}, fn)

    @app.post("/submit")
    async def submit(request: Request, body: SubmitIn):
        return await handle(request, "submit", body.model_dump(), lambda a: market.submit(a, body.answer))

    @app.post("/stuck")
    async def stuck(request: Request, body: StuckIn):
        return await handle(request, "declare_stuck", body.model_dump(),
                            lambda a: market.declare_stuck(a, body.reason))

    # ------------------------------------------------------------ admin API

    def is_admin(request: Request) -> bool:
        return bearer(request.headers) == manifest.admin_token

    @app.get("/admin/state")
    async def admin_state(request: Request):
        if not is_admin(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return {"open": market.is_open(), **market.snapshot()}

    @app.get("/admin/credits")
    async def admin_credits(request: Request):
        """Remaining credits of every agent, live (observers only; agents see only their own)."""
        if not is_admin(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return {"round": market.round_no, "phase": market.phase, "credits": dict(market.credits),
                "total": sum(market.credits.values())}

    @app.post("/admin/close")
    async def admin_close(request: Request):
        if not is_admin(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if market.phase == "active":
            await market.end_round("aborted", "stopped by the operator")
        market.close("admin")
        write_final_state()
        return {"ok": True}

    return app
