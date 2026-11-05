"""LLM egress gateway.

The only container with internet access. Agents call it with their agent
token in place of a provider API key; the gateway swaps in the real key,
forwards the request, and logs the full request and response bodies. This
gives a complete record of every model call even when an agent's own trace
is missing or tampered with, and keeps provider keys out of agent containers.

Routes: /{route}/{path} -> {upstream base_url}/{path}
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from hivemarket.common.events import EventLog, JsonlSink
from hivemarket.common.manifest import Manifest, bearer

# route -> (upstream base url, env var holding the key, auth style)
DEFAULT_UPSTREAMS: dict[str, dict[str, str]] = {
    "anthropic": {"base_url": "https://api.anthropic.com", "api_key_env": "ANTHROPIC_API_KEY", "auth": "x-api-key"},
    "openai": {"base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "auth": "bearer"},
    "google": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "api_key_env": "GEMINI_API_KEY", "auth": "bearer"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY", "auth": "bearer"},
}

_DROP_REQUEST_HEADERS = {"host", "content-length", "authorization", "x-api-key", "accept-encoding", "connection"}
_DROP_RESPONSE_HEADERS = {"content-length", "content-encoding", "transfer-encoding", "connection"}


def _usage(body: Any) -> dict[str, Any] | None:
    if isinstance(body, dict) and isinstance(body.get("usage"), dict):
        return body["usage"]
    return None


def create_app(manifest: Manifest, run_dir: Path, upstreams: dict[str, dict[str, str]] | None = None,
               transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    app = FastAPI(title="HiveMarket LLM gateway", docs_url=None, redoc_url=None)
    routes = {**DEFAULT_UPSTREAMS, **(upstreams or {})}
    log = EventLog("gateway", manifest.run_id, JsonlSink(run_dir / "logs" / "gateway.jsonl"))
    client = httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=15.0), transport=transport)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "routes": sorted(routes)}

    @app.api_route("/{route}/{path:path}", methods=["GET", "POST"])
    async def proxy(route: str, path: str, request: Request):
        src_ip = request.client.host if request.client else None
        agent = manifest.agent_by_token(bearer(request.headers))
        raw = await request.body()
        try:
            req_body: Any = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            req_body = raw.decode("utf-8", "replace")

        if agent is None:
            ip_agent = manifest.agent_by_ip(src_ip)
            log.emit("unauthenticated_request", agent_id=ip_agent.id if ip_agent else None,
                     source_ip=src_ip, route=route, path=path)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        upstream = routes.get(route)
        if upstream is None:
            log.emit("llm_bad_route", agent_id=agent.id, route=route, path=path)
            return JSONResponse({"error": f"unknown route {route}"}, status_code=404)

        headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST_HEADERS}
        key = os.environ.get(upstream.get("api_key_env", ""), "")
        if upstream.get("auth") == "x-api-key":
            headers["x-api-key"] = key
        else:
            headers["authorization"] = f"Bearer {key}"

        started = time.perf_counter()
        url = f"{upstream['base_url'].rstrip('/')}/{path}"
        try:
            resp = await client.request(request.method, url, content=raw, headers=headers,
                                        params=dict(request.query_params))
        except httpx.HTTPError as e:
            log.emit("llm_call", agent_id=agent.id, route=route, path=path, request=req_body,
                     status=502, error=repr(e), latency_ms=round((time.perf_counter() - started) * 1000, 1))
            return JSONResponse({"error": "upstream_unreachable", "detail": repr(e)}, status_code=502)

        try:
            resp_body: Any = resp.json()
        except ValueError:
            resp_body = resp.text
        log.emit(
            "llm_call",
            agent_id=agent.id,
            route=route,
            path=path,
            source_ip=src_ip,
            model=req_body.get("model") if isinstance(req_body, dict) else None,
            assigned_model=agent.model,
            request=req_body,
            status=resp.status_code,
            response=resp_body,
            usage=_usage(resp_body),
            request_id=resp.headers.get("request-id") or resp.headers.get("x-request-id"),
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _DROP_RESPONSE_HEADERS}
        return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)

    return app


def main() -> None:
    run_dir = Path(os.environ.get("RUN_DIR", "/run_data"))
    manifest = Manifest.load(run_dir / "manifest.json")
    extra_path = run_dir / "gateway_routes.json"
    extra = json.loads(extra_path.read_text()) if extra_path.exists() else None
    uvicorn.run(create_app(manifest, run_dir, extra), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
                log_level="warning")
