"""Agent runtime: the LLM <-> tools loop for one agent.

How the session carries across rounds is the experiment's `session` (cfg.session):
  fresh       at each round end, wait for the next round and start a new conversation holding only
              the system prompt and the current status (no summaries, no memory tool)
  memory      nothing at round end: the next round's notices arrive in the same conversation
  reflection  as memory, plus one reflection turn (tools off) when the round ends; it stays in
              the conversation
Within a round, every mode restarts the conversation when it passes the context budget (after a
summary) or when the provider reports overflow.

Traced events (sent to the collector):
  runtime_start       system prompt, tool list, spec, affordance, session
  llm_turn            text, reasoning summaries, tool calls, usage, serving model, latency
  llm_error           provider/API failures
  tool_call           name, args, output, error flag, duration
  events_delivered    market messages/notices and direct connections shown to the model
  direct_inbound      (from inbox.py) every connection to the agent's direct port
  round_reflection    round, text, usage (session: reflection)
  context_checkpoint  summary written before a budget restart
  context_reset       a new conversation: reason new_round (fresh) | context_limit | context_overflow
  runtime_stop        reason and step count
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from hivemarket.agent.inbox import DirectInbox, DirectMessage
from hivemarket.agent.market_client import MarketClient
from hivemarket.agent.prompts import (checkpoint_prompt, kickoff_text, reflection_text, render_system_prompt,
                                     restart_text)
from hivemarket.agent.providers import make_provider
from hivemarket.agent.providers.base import ToolResult, context_tokens
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
        self.steps = 0  # LLM calls over the whole session
        self.steps_in_round = 0
        self.round_no = 0
        self.last_round_ended = 0  # from the broker's round_end notices
        self.market_closed = False
        self.round_deadline: float | None = None
        self.summaries: list[str] = []  # written by the agent at each checkpoint
        self.pending_checkpoint: str | None = None  # set when the context budget is passed
        self.round_over: dict[str, Any] | None = None  # a round_end notice awaiting the session's round-end step
        self.budget = int(cfg.context.window_tokens * cfg.context.reset_at_fraction)
        self.restarts = 0

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
        if self.round_deadline and not self.market_closed:
            left = f" - round {self.round_no}, about {max(0, int(self.round_deadline - now()))}s left"
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
                new = body.get("events", [])
                market_events.extend(new)
                for e in new:
                    if e.get("event") == "round_start":  # the session carries on into the new round
                        self.round_no = e.get("round", self.round_no + 1)
                        self.round_deadline = e.get("deadline")
                        self.steps_in_round = 0
                    elif e.get("event") == "round_end":
                        self.last_round_ended = max(self.last_round_ended, e.get("round", self.round_no))
                        if self.cfg.session != "memory":
                            self.round_over = e
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
            self.round_no = status.get("round", 0)
            if status.get("seconds_left_in_round") is not None:
                self.round_deadline = now() + status["seconds_left_in_round"]
            system = render_system_prompt(self.cfg, status)
            tools = tools_for_config(self.cfg)
            self.provider.start(system, tools, kickoff_text(status))
            self.telemetry.emit("runtime_start", system_prompt=system, tools=[t.name for t in tools],
                                spec=self.cfg.spec.model_dump(), affordance=self.cfg.affordance.model_dump(),
                                peers_disclosed=self.cfg.peers, initial_status=status, session=self.cfg.session)
            reason = await self._loop()
        except Exception as e:
            self.telemetry.emit("runtime_crash", error=repr(e))
            raise
        finally:
            self.telemetry.emit("runtime_stop", reason=reason, steps=self.steps)
            await self.inbox.stop()
            await self.telemetry.close()
        return reason

    # ------------------------------------------------------- context policy

    async def _checkpoint(self, reason: str) -> None:
        """Ask the agent for a summary for its future self (tools disabled)."""
        text, error = "", None
        try:
            turn = await self.provider.summarize(checkpoint_prompt(self.cfg))
            self.steps += 1
            text = (turn.text or "").strip()[: self.cfg.context.summary_chars]
            usage = turn.usage
        except Exception as e:
            error, usage = repr(e), None
        if text:
            self.summaries.append(f"[round {self.round_no}, {reason.replace('_', ' ')}] {text}")
        self.telemetry.emit("context_checkpoint", round=self.round_no, reason=reason, summary=text,
                            usage=usage, error=error)

    async def _status(self) -> dict[str, Any]:
        try:
            _, status = await self.market.status(origin="runtime")
        except Exception:
            status = {}
        return status

    async def _restart(self, reason: str) -> None:
        """Continue in a fresh conversation mid-round: summaries + memory + current status."""
        status = await self._status()
        memory = self.tools.memory.dump(self.cfg.context.memory_chars_in_restart) if self.cfg.memory.enabled else None
        self._reset(reason, restart_text(reason, status, self.summaries[-self.cfg.context.max_summaries:], memory))

    def _reset(self, reason: str, first: str) -> None:
        self.provider.reset(first)
        self.restarts += 1
        self.telemetry.emit("context_reset", round=self.round_no, reason=reason, restart_number=self.restarts,
                            first_message=first, summaries=len(self.summaries))

    # ----------------------------------------------------------- round end

    async def _end_of_round(self, notice: dict[str, Any]) -> None:
        """The session's step when the broker announces a round is over (never called in `memory`)."""
        if self.cfg.session == "reflection":
            await self._reflect(notice)
            return
        # fresh: the next round starts a new conversation with nothing carried over
        await self._await_round_start()
        if not self.market_closed:
            self.summaries.clear()
            self._reset("new_round", kickoff_text(await self._status()))

    async def _reflect(self, notice: dict[str, Any]) -> None:
        """One turn (tools off) reflecting on the round; it stays in the conversation for the next round.

        Not counted against the per-round step limit (the runtime asks for it), but it is part of
        the conversation, so it counts toward the context budget.
        """
        status = await self._status()
        own = status if status.get("round") == notice.get("round") else None  # reset once the next round starts
        text, usage, error = "", None, None
        try:
            turn = await self.provider.summarize(reflection_text(self.cfg, notice, own))
            self.steps += 1
            text, usage = (turn.text or "").strip(), turn.usage
        except Exception as e:
            error = repr(e)
        self.telemetry.emit("round_reflection", round=notice.get("round"), text=text, usage=usage, error=error)
        if context_tokens(usage) > self.budget and not self.pending_checkpoint:
            self.pending_checkpoint = "context_limit"

    async def _await_round_start(self) -> str | None:
        """Wait until a round after the last ended one is under way.

        Compares with the round that ended, not the round number when we started waiting:
        the round_end and the next round_start notices often arrive in the same batch (e.g.
        after a slow model turn), and by then round_no already points at the new round.
        """
        notes = []
        while not self.market_closed and self.round_no <= self.last_round_ended:
            note = await self._collect_events(wait=30)
            if note:
                notes.append(note)
        # A round that ended while we were waiting was never played: nothing to do for it.
        self.round_over = None
        return "\n".join(notes) or None

    async def _wait_for_next_round(self) -> str | None:
        notes = []
        while not self.market_closed and self.steps_in_round > 0:
            note = await self._collect_events(wait=30)
            if note:
                notes.append(note)
        return "\n".join(notes) or None

    async def _loop(self) -> str:
        errors = 0
        limit = self.cfg.max_steps_per_round or self.cfg.spec.max_steps
        while True:
            if self.round_over:  # before the market check: the last round is reflected on too
                notice, self.round_over = self.round_over, None
                await self._end_of_round(notice)
                continue
            if self.market_closed:
                return "market_closed"
            if self.pending_checkpoint:
                reason, self.pending_checkpoint = self.pending_checkpoint, None
                await self._checkpoint(reason)
                await self._restart(reason)
                continue
            if self.steps_in_round >= limit:
                self.telemetry.emit("round_step_limit", round=self.round_no, steps=self.steps_in_round, limit=limit)
                note = await self._wait_for_next_round()
                if self.market_closed:
                    return "market_closed"
                self.provider.add_user_text(note or "A new round has started.")
                continue
            started = time.perf_counter()
            try:
                turn = await self.provider.generate()
            except Exception as e:
                if _is_context_overflow(e):
                    # Too long even to summarize: restart from earlier summaries and memory.
                    self.telemetry.emit("context_overflow", round=self.round_no, error=repr(e)[:500])
                    await self._restart("context_overflow")
                    continue
                errors += 1
                self.telemetry.emit("llm_error", step=self.steps, error=repr(e), consecutive=errors)
                if errors >= 5:
                    return "llm_errors"
                await asyncio.sleep(5 * errors)
                continue
            errors = 0
            self.steps += 1
            self.steps_in_round += 1
            self.telemetry.emit(
                "llm_turn", step=self.steps, round=self.round_no, latency_ms=round((time.perf_counter() - started) * 1000, 1),
                text=turn.text, reasoning=turn.reasoning,
                tool_calls=[{"id": c.id, "name": c.name, "args": c.args} for c in turn.tool_calls],
                stop_reason=turn.stop_reason, usage=turn.usage, model=turn.model,
                request_id=turn.request_id, **turn.extra,
            )
            if turn.refused:
                return "refusal"
            if context_tokens(turn.usage) > self.budget and not self.pending_checkpoint:
                self.pending_checkpoint = "context_limit"
            if not turn.tool_calls and "<tool_call>" in (turn.text or ""):
                # Typical of OpenAI-compatible servers without a tool-call parser (e.g. vLLM without
                # --enable-auto-tool-choice): the model "calls" tools in plain text and nothing runs.
                self.telemetry.emit("tool_call_in_text", round=self.round_no, text=turn.text[:2000])

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
                self.telemetry.emit("tool_call", step=self.steps, round=self.round_no, tool_call_id=call.id, name=call.name,
                                    args=call.args, output=out, is_error=err,
                                    duration_ms=round((time.perf_counter() - t0) * 1000, 1))
                results.append(ToolResult(call.id, out, err))
            self.provider.add_tool_results(results, await self._collect_events(wait=0))


def _is_context_overflow(e: Exception) -> bool:
    text = str(e).lower()
    return any(k in text for k in ("context length", "context_length", "prompt is too long", "too many tokens",
                                   "maximum context", "context window", "input is too long"))
