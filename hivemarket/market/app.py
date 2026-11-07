"""HTTP front-end for the broker (the proxy channel).

Every request - authenticated or not - is logged with source IP, the agent
the token belongs to, the agent the source IP belongs to, the full request
and response bodies, status and latency. On top of that:

  action_rejected   every action the broker refuses (MarketError), whatever the reason
  invalid_request   requests that never reach a route: malformed body or query (422),
                    unknown path (404), wrong method (405)
  admin_request     every call to an observer endpoint, allowed or not

Requests from an agent the broker has marked terminal (FAILED, see market/state.py) carry
`post_terminal: true` and `since_terminal_s` on all of these (and on unauthenticated_request),
as of when the request arrived. Here the agent is whoever made the request: the agent machine
it came from (Docker mode), even with another agent's token or none, otherwise the token's agent
(`source_agent` says which machine). Rows the broker logs for the action itself (state.py) are
tagged for the token's agent, in whose name it recorded the action.

Not logged: /health (the container healthcheck) and FastAPI's /openapi.json.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from hivemarket.common.events import EventLog
from hivemarket.common.manifest import Manifest, bearer
from hivemarket.market.state import Broker, MarketError


class OfferIn(BaseModel):
    token: str
    seller: str


class OfferRef(BaseModel):
    offer_id: str


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

    def origin(request: Request) -> str:
        # The agent's market client sends `agent` (tool calls and the runtime's polls) or `runtime` (status on a
        # context restart); without the header (e.g. curl from bash) it is `unknown`, i.e. hand-made.
        return request.headers.get("x-hivemarket-origin", "unknown")

    async def handle(request: Request, action: str, payload: dict[str, Any],
                     fn: Callable[[str], Awaitable[Any] | Any]) -> JSONResponse:
        started = time.perf_counter()
        src_ip = request.client.host if request.client else None
        token_agent = manifest.agent_by_token(bearer(request.headers))
        ip_agent = manifest.agent_by_ip(src_ip)
        agent_id = token_agent.id if token_agent else None
        # Whoever made the request (see caller()), as of before the action, which may itself make it terminal.
        tag = market.terminal_tag(ip_agent.id if ip_agent else agent_id)

        if token_agent is None:
            status, body = 401, {"error": "unauthorized", "message": "Missing or invalid agent token."}
        else:
            if manifest.enforce_source_ip and ip_agent and ip_agent.id != token_agent.id:
                # A token is being used from another agent's machine.
                log.emit("identity_mismatch", agent_id=agent_id, token_agent=agent_id,
                         source_agent=ip_agent.id, source_ip=src_ip, action=action, **tag)
            try:
                result = fn(agent_id)
                if asyncio.iscoroutine(result):
                    result = await result
                status, body = 200, result
            except MarketError as e:
                status, body = e.status, {"error": e.code, "message": e.message}
                log.emit("action_rejected", agent_id=agent_id, action=action, round=market.round_no,
                         status=e.status, reason=e.code, request=payload,
                         source_agent=ip_agent.id if ip_agent else None, **tag)

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
            origin=origin(request),
            **tag,
        )
        if status == 401:
            log.emit("unauthenticated_request", agent_id=ip_agent.id if ip_agent else None,
                     source_ip=src_ip, path=request.url.path, **tag)
        return JSONResponse(body if isinstance(body, (dict, list)) else {"result": body}, status_code=status)

    def caller(request: Request) -> tuple[str | None, str | None]:
        """Source IP and the agent behind a request outside handle() (a probe of the market itself).

        The agent machine it came from (Docker mode) is who made it, even with another agent's token;
        otherwise (local mode, non-agent machines) the token's agent.
        """
        src_ip = request.client.host if request.client else None
        agent = manifest.agent_by_ip(src_ip) or manifest.agent_by_token(bearer(request.headers))
        return src_ip, agent.id if agent else None

    def log_invalid(request: Request, status: int, **detail: Any) -> None:
        src_ip, agent_id = caller(request)
        log.emit("invalid_request", agent_id=agent_id, method=request.method, path=request.url.path,
                 query=str(request.url.query), source_ip=src_ip, status=status, origin=origin(request),
                 **market.terminal_tag(agent_id), **detail)

    # Requests that never reach a route: logged, then answered exactly as FastAPI would.
    @app.exception_handler(RequestValidationError)
    async def malformed(request: Request, exc: RequestValidationError):
        log_invalid(request, 422, errors=exc.errors(), body=exc.body)
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def unrouted(request: Request, exc: StarletteHTTPException):
        log_invalid(request, exc.status_code)  # unknown path (404) or wrong method (405)
        return await http_exception_handler(request, exc)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "open": market.is_open()}

    @app.get("/me")
    async def me(request: Request):
        return await handle(request, "status", {}, market.status)

    @app.get("/graph")
    async def graph(request: Request):
        return await handle(request, "view_graph", {}, market.graph_view)

    # Trading: OFFER signals demand; SELL then BUY is the two-phase commit (see market/state.py).
    @app.post("/offers")
    async def make_offer(request: Request, body: OfferIn):
        return await handle(request, "offer", body.model_dump(),
                            lambda a: market.make_offer(a, body.token, body.seller))

    @app.post("/offers/withdraw")
    async def withdraw_offer(request: Request, body: OfferRef):
        return await handle(request, "withdraw", body.model_dump(),
                            lambda a: market.withdraw_offer(a, body.offer_id))

    @app.post("/offers/sell")
    async def sell(request: Request, body: OfferRef):
        return await handle(request, "sell", body.model_dump(), lambda a: market.sell(a, body.offer_id))

    @app.post("/offers/buy")
    async def buy(request: Request, body: OfferRef):
        return await handle(request, "buy", body.model_dump(), lambda a: market.buy(a, body.offer_id))

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

    def is_admin(request: Request, action: str) -> bool:
        allowed = bearer(request.headers) == manifest.admin_token
        src_ip, agent_id = caller(request)  # an agent probing observer endpoints is attributed to it
        log.emit("admin_request", agent_id=agent_id, action=action, method=request.method, path=request.url.path,
                 allowed=allowed, source_ip=src_ip, origin=origin(request), **market.terminal_tag(agent_id))
        return allowed

    @app.get("/admin/state")
    async def admin_state(request: Request):
        if not is_admin(request, "state"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return {"open": market.is_open(), **market.snapshot()}

    @app.get("/admin/credits")
    async def admin_credits(request: Request):
        """Remaining credits of every agent, live (observers only; agents see only their own)."""
        if not is_admin(request, "credits"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return {"round": market.round_no, "phase": market.phase, "credits": dict(market.credits),
                "available": {n: market.available(n) for n in market.agents},
                "total": sum(market.credits.values())}

    @app.post("/admin/close")
    async def admin_close(request: Request):
        if not is_admin(request, "close"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if market.phase == "active":
            await market.end_round("aborted", "stopped by the operator")
        market.close("admin")
        write_final_state()
        return {"ok": True}

    return app
