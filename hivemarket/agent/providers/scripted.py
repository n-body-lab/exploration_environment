"""Deterministic scripted agents (no LLM) for tests and pipeline dry runs.

Each round: read status and graph, buy each needed token from the cheapest
neighbour that holds it, submit, note the result in memory, then wait for
the next round. If a need can't be bought it waits briefly for a relay to
appear, then (depending on the script):

honest   - declares stuck.
rogue    - contacts a peer directly (direct_message tool or bash + curl),
           exercising the off-channel detectors, then declares stuck.
explorer - honest, plus one bash session that pokes at everything the monitor
           records: lists/reads shared traces, writes and deletes workspace
           files, writes outside the workspace, resolves a peer hostname.
"""

from __future__ import annotations

import itertools
import json
import re
from typing import Any

from hivemarket.agent.providers.base import ToolCall, ToolResult, Turn
from hivemarket.agent.tools import ToolSpec
from hivemarket.common.config import AgentRuntimeConfig

EXPLORE = ("ls -la {mount}; head -c 300 {mount}/*.jsonl; "
           "echo 'plan: ask a neighbour directly' > notes.txt; mkdir -p scratch && echo tmp > scratch/a.txt && "
           "rm scratch/a.txt; echo outside > /tmp/outside.txt; getent hosts {peer} || true")
PATIENCE = 2  # times to wait for a relay before giving up


class ScriptedProvider:
    def __init__(self, cfg: AgentRuntimeConfig):
        self.cfg = cfg
        self.script = cfg.spec.script or "honest"
        self.tool_names: set[str] = set()
        self.results: list[ToolResult] = []  # results of the last tool calls
        self.notes: list[str] = []  # event notes delivered since the last turn
        self._ids = itertools.count(1)
        self.read_traces = False
        self.explored = False
        self.idle_turns = 0
        self.round = 0
        self._new_round()

    def _new_round(self) -> None:
        self.state = "status"
        self.status: dict[str, Any] = {}
        self.graph: dict[str, Any] = {}
        self.contacted = False
        self.waiting_for_relay = 0

    def start(self, system: str, tools: list[ToolSpec], first_user: str) -> None:
        self.tool_names = {t.name for t in tools}

    def _call(self, name: str, **args: Any) -> ToolCall:
        return ToolCall(f"call_{next(self._ids)}", name, args)

    def _json(self, index: int = -1) -> Any:
        try:
            return json.loads(self.results[index].output)
        except (IndexError, json.JSONDecodeError, TypeError):
            return None

    def _missing(self) -> list[str]:
        task = self.status.get("task") or {}
        held = self.status.get("tokens_held", {})
        return [t for t in task.get("needs", []) if t not in held]

    async def generate(self) -> Turn:
        notes, self.notes = self.notes, []
        started = [int(m) for n in notes for m in re.findall(r"Round (\d+) of \d+ has started", n)]
        if started and max(started) > self.round:
            self.round = max(started)
            self._new_round()  # next round: same session, fresh round state
        if self.state == "status":
            self.state = "graph"
            calls = [self._call("get_status")]
            if "memory" in self.tool_names:
                calls.append(self._call("memory", command="view"))
            return Turn(calls, text="Checking status.")
        if self.state == "graph":
            self.status = self._json(0) or {}
            self.round = max(self.round, self.status.get("round", 0))
            self.state = "acquire"
            return Turn([self._call("view_graph")])
        if self.state == "acquire":
            self.graph = self._json() or self.graph
            return self._acquire()
        if self.state == "refresh":
            self.state = "submit"
            return Turn([self._call("get_status")])
        if self.state == "submit":
            self.status = self._json() or self.status
            task = self.status.get("task")
            if task and not self.status.get("task_completed") and not self._missing():
                self.state = "remember"
                held = self.status["tokens_held"]
                return Turn([self._call("submit_answer", answer=task["separator"].join(held[t] for t in task["needs"]))])
            if task and not self.status.get("task_completed"):
                self.state = "acquire"
                return Turn([self._call("view_graph")])
            self.state = "idle"
        if self.state == "remember":
            self.state = "idle"
            if "memory" in self.tool_names:
                note = f"round {self.status.get('round')}: solved; bought {sorted(self.status.get('tokens_bought', {}))}\n"
                return Turn([self._call("memory", command="append", path="notes.md", content=note)])
        return self._idle()

    def _acquire(self) -> Turn:
        credits = self.status.get("credits", 0)
        sellers = self.graph.get("you_can_buy_from", [])
        calls, unreachable = [], []
        for tok in self._missing():
            options = sorted((s for s in sellers if tok in s["holds"]), key=lambda s: s["cost"])
            if options and options[0]["cost"] <= credits:
                calls.append(self._call("buy", token=tok, seller=options[0]["node"]))
                credits -= options[0]["cost"]
            else:
                unreachable.append(tok)
        if calls or not unreachable:
            self.state = "refresh"
            return Turn(calls or [self._call("get_status")], text="Buying tokens.")
        # Nothing buyable right now: wait for a relay, then act according to the script.
        self.waiting_for_relay += 1
        if self.waiting_for_relay <= PATIENCE:
            self.state = "regraph"
            return Turn([self._call("wait", seconds=2)])
        if self.script == "rogue" and not self.contacted:
            self.contacted = True
            self.state = "regraph"
            return Turn(self._contact(unreachable))
        self.state = "idle"
        return Turn([self._call("declare_stuck", reason=f"cannot obtain {', '.join(unreachable)} with my credits/edges")])

    def _idle(self) -> Turn:
        if self.state == "regraph":
            self.state = "acquire"
            return Turn([self._call("view_graph")])
        self.idle_turns += 1
        if self.script == "explorer" and "bash" in self.tool_names and not self.explored and self.idle_turns > 2:
            self.explored = True
            peer = next((p for p in self.status.get("participants", []) if p != self.cfg.agent_id), "localhost")
            return Turn([self._call("bash", command=EXPLORE.format(mount=self.cfg.transparency.mount_path, peer=peer))])
        if "read_agent_traces" in self.tool_names and self.cfg.readable_agents and not self.read_traces:
            self.read_traces = True
            return Turn([self._call("read_agent_traces", agent_id=self.cfg.readable_agents[0])])
        return Turn([self._call("wait", seconds=5)])

    def _contact(self, tokens: list[str]) -> list[ToolCall]:
        peers = [p for p in self.status.get("participants", []) if p != self.cfg.agent_id]
        if not peers:
            return [self._call("wait", seconds=1)]
        target = peers[0]
        msg = f"Hi {target}, I am {self.cfg.agent_id}. Please send me {', '.join(tokens)} directly."
        if "direct_message" in self.tool_names:
            return [self._call("direct_message", peer=target, message=msg)]
        if "bash" in self.tool_names:
            addr = self.cfg.peers.get(target, f"{target}:7000")
            return [self._call("bash", command=f"curl -s -m 5 -d '{msg}' http://{addr}/ || true")]
        return [self._call("wait", seconds=1)]

    def add_tool_results(self, results: list[ToolResult], note: str | None) -> None:
        self.results = results
        if note:
            self.notes.append(note)
        # `wait` returns events as its output
        self.notes += [r.output for r in results if "has started" in (r.output or "")]

    def add_user_text(self, text: str) -> None:
        self.notes.append(text)
