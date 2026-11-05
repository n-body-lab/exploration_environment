"""Agent runtime: the LLM <-> tools loop for one agent.

Traced events (sent to the collector):
  runtime_start     system prompt, tool list, spec, affordance
  llm_turn          text, reasoning summaries, tool calls, usage, serving model, latency
  llm_error         provider/API failures
  tool_call         name, args, output, error flag, duration
  events_delivered  market messages/notices and direct connections shown to the model
  direct_inbound    (from inbox.py) every connection to the agent's direct port
  runtime_stop      reason and step count
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from hivemarket.agent.inbox import DirectInbox, DirectMessage
from hivemarket.agent.market_client import MarketClient
from hivemarket.agent.prompts import kickoff_text, render_system_prompt
from hivemarket.agent.providers import make_provider
from hivemarket.agent.providers.base import ToolResult
from hivemarket.agent.telemetry import Telemetry
from hivemarket.agent.tools import ToolExecutor, tools_for_config
from hivemarket.common.config import AgentRuntimeConfig
from hivemarket.common.events import now


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%SZ")


class AgentRuntime:
    def __init__(self, cfg: AgentRuntimeConfig, *, telemetry: Telemetry | None = None,
                 inbox_host: str = "0.0.0.0", echo: bool = True):
        self.cfg = cfg
        self.telemetry = telemetry or Telemetry(cfg.collector_url, cfg.token, cfg.agent_id, echo=echo)
        self.market = MarketClient(cfg.market_url, cfg.token, collector_url=cfg.collector_url)
        self.inbox = DirectInbox(cfg.inbox_port, self.telemetry, host=inbox_host)
        self.tools = ToolExecutor(cfg, self.market, self._wait_tool)
        self.provider = make_provider(cfg)
        self.cursor = 0
        self.steps = 0
        self.market_closed = False
        self.closes_at: float | None = None

    # ------------------------------------------------------------- events

    def _render(self, market_events: list[dict[str, Any]], direct: list[DirectMessage]) -> str | None:
        lines = []
        for e in market_events:
            if e.get("type") == "message":
                to = " (to everyone)" if e.get("to") == "*" else ""
                lines.append(f"- [{_clock(e['ts'])}] Message from {e['from']}{to}: {e['body']}")
            else:
                lines.append(f"- [{_clock(e['ts'])}] Market notice: {e['body']}")
        if self.cfg.affordance.deliver_direct_messages:
            for d in direct:
                lines.append(f"- [{_clock(d.ts)}] Incoming connection on your port {self.cfg.inbox_port} "
                             f"from {d.src_ip} ({d.protocol}): {d.body}")
        if not lines:
            return None
        left = ""
        if self.closes_at:
            left = f" - about {max(0, int(self.closes_at - now()))}s left before the market closes"
        return f"[New events{left}]\n" + "\n".join(lines)

    async def _collect_events(self, wait: float = 0) -> str | None:
        deadline = time.monotonic() + wait
        market_events: list[dict[str, Any]] = []
        direct: list[DirectMessage] = []
        while True:
            remaining = deadline - time.monotonic()
            try:
                status, body = await self.market.inbox(self.cursor, wait=min(5.0, max(0.0, remaining)))
            except Exception as e:
                self.telemetry.emit("market_unreachable", error=repr(e))
                status, body = 0, {}
            if status == 200:
                market_events.extend(body.get("events", []))
                if market_events:
                    self.cursor = max(e["seq"] for e in market_events)
                if not body.get("market_open", True):
                    self.market_closed = True
            direct.extend(self.inbox.drain())
            if market_events or direct or self.market_closed or time.monotonic() >= deadline:
                break
            if status != 200:
                await asyncio.sleep(1)
        note = self._render(market_events, direct)
        if market_events or direct:
            self.telemetry.emit("events_delivered", market_events=market_events,
                                direct=[d.__dict__ for d in direct], rendered=note,
                                direct_suppressed=not self.cfg.affordance.deliver_direct_messages)
        return note

    async def _wait_tool(self, seconds: int) -> str:
        note = await self._collect_events(wait=seconds)
        if self.market_closed:
            return (note + "\n" if note else "") + "The market is closed."
        return note or f"No new events in the last {seconds}s."

    # --------------------------------------------------------------- loop

    async def _initial_status(self) -> dict[str, Any]:
        for _ in range(60):
            try:
                status, body = await self.market.status()
                if status == 200:
                    return body
            except Exception:
                pass
            await asyncio.sleep(1)
        raise RuntimeError("market unreachable")

    async def run(self) -> str:
        self.telemetry.start()
        await self.inbox.start()
        reason = "error"
        try:
            status = await self._initial_status()
            if status.get("seconds_remaining") is not None:
                self.closes_at = now() + status["seconds_remaining"]
            system = render_system_prompt(self.cfg, status)
            tools = tools_for_config(self.cfg)
            self.provider.start(system, tools, kickoff_text(status))
            self.telemetry.emit("runtime_start", system_prompt=system, tools=[t.name for t in tools],
                                spec=self.cfg.spec.model_dump(), affordance=self.cfg.affordance.model_dump(),
                                peers_disclosed=self.cfg.peers, initial_status=status)
            reason = await self._loop()
        except Exception as e:
            self.telemetry.emit("runtime_crash", error=repr(e))
            raise
        finally:
            self.telemetry.emit("runtime_stop", reason=reason, steps=self.steps)
            await self.inbox.stop()
            await self.telemetry.close()
        return reason

    async def _loop(self) -> str:
        errors = 0
        while True:
            if self.market_closed:
                return "market_closed"
            if self.steps >= self.cfg.spec.max_steps:
                return "max_steps"
            started = time.perf_counter()
            try:
                turn = await self.provider.generate()
            except Exception as e:
                errors += 1
                self.telemetry.emit("llm_error", step=self.steps, error=repr(e), consecutive=errors)
                if errors >= 5:
                    return "llm_errors"
                await asyncio.sleep(5 * errors)
                continue
            errors = 0
            self.steps += 1
            self.telemetry.emit(
                "llm_turn", step=self.steps, latency_ms=round((time.perf_counter() - started) * 1000, 1),
                text=turn.text, reasoning=turn.reasoning,
                tool_calls=[{"id": c.id, "name": c.name, "args": c.args} for c in turn.tool_calls],
                stop_reason=turn.stop_reason, usage=turn.usage, model=turn.model,
                request_id=turn.request_id, **turn.extra,
            )
            if turn.refused:
                return "refusal"

            if not turn.tool_calls:
                note = await self._collect_events(wait=self.cfg.idle_wait_seconds)
                if self.market_closed:
                    return "market_closed"
                self.provider.add_user_text(
                    note or "No new events. Continue working toward your objective, or call `wait`.")
                continue

            results = []
            for call in turn.tool_calls:  # sequential: market actions are order-sensitive
                t0 = time.perf_counter()
                if call.parse_error:
                    out, err = f"Invalid JSON arguments: {call.parse_error}", True
                else:
                    out, err = await self.tools.run(call.name, call.args)
                self.telemetry.emit("tool_call", step=self.steps, tool_call_id=call.id, name=call.name,
                                    args=call.args, output=out, is_error=err,
                                    duration_ms=round((time.perf_counter() - t0) * 1000, 1))
                results.append(ToolResult(call.id, out, err))
            self.provider.add_tool_results(results, await self._collect_events(wait=0))
