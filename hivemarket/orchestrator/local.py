"""In-process runner: every service and agent on localhost, no Docker.

For development and tests. There is no network isolation and no packet
capture, so off-channel detection relies on the application-level traces
(direct_inbound, tool calls) and the market's unexplained_knowledge check.
Real LLM agents with a shell are refused unless explicitly allowed, because
the shell would run unsandboxed on the host.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import uvicorn

from hivemarket.agent.runtime import AgentRuntime
from hivemarket.collector.app import create_app as collector_app
from hivemarket.common.config import AgentRuntimeConfig, ExperimentConfig
from hivemarket.common.manifest import Manifest
from hivemarket.gateway.app import create_app as gateway_app
from hivemarket.market.__main__ import build as market_app


async def run_local(run_dir: Path, *, allow_unsandboxed_shell: bool = False, echo: bool = False,
                    grace_seconds: float = 30) -> dict[str, str]:
    manifest = Manifest.load(run_dir / "manifest.json")
    experiment = ExperimentConfig.load(run_dir / "experiment.yaml")
    routes_path = run_dir / "gateway_routes.json"
    routes = json.loads(routes_path.read_text()) if routes_path.exists() else None

    configs = [AgentRuntimeConfig.model_validate_json(p.read_text()) for p in sorted((run_dir / "agents").glob("*.json"))]
    for cfg in configs:
        if cfg.affordance.level != "none" and cfg.provider_kind != "scripted" and not allow_unsandboxed_shell:
            raise SystemExit(f"{cfg.agent_id}: LLM agents with a shell need Docker for sandboxing. "
                             "Use `hivemarket run` (docker) or pass --unsafe-local-shell.")
        cfg.allow_unsandboxed_shell = allow_unsandboxed_shell or cfg.provider_kind == "scripted"

    def port(url: str) -> int:
        return int(url.rsplit(":", 1)[1])

    apps = {
        "market": market_app(run_dir, echo=echo),
        "collector": collector_app(manifest, run_dir, experiment.transparency),
        "gateway": gateway_app(manifest, run_dir, routes),
    }
    servers = [
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port(manifest.services[name].url), log_level="warning"))
        for name, app in apps.items()
    ]
    server_tasks = [asyncio.create_task(s.serve()) for s in servers]
    while not all(s.started for s in servers):
        if any(t.done() for t in server_tasks):
            raise RuntimeError("a service failed to start")
        await asyncio.sleep(0.05)

    runtimes = [AgentRuntime(cfg, inbox_host="127.0.0.1", echo=echo) for cfg in configs]
    agent_tasks = {asyncio.create_task(r.run()): r.cfg.agent_id for r in runtimes}
    results: dict[str, str] = {}
    try:
        done, pending = await asyncio.wait(agent_tasks, timeout=manifest.duration_seconds + grace_seconds)
        for t in pending:
            t.cancel()
        for t in done:
            results[agent_tasks[t]] = t.result() if not t.exception() else f"crashed: {t.exception()!r}"
        for t in pending:
            results[agent_tasks[t]] = "timeout"
    finally:
        async with httpx.AsyncClient() as c:
            await c.post(f"{manifest.services['market'].url}/admin/close",
                         headers={"authorization": f"Bearer {manifest.admin_token}"})
        for s in servers:
            s.should_exit = True
        await asyncio.gather(*server_tasks, return_exceptions=True)
    return results
