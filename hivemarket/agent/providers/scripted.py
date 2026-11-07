"""Deterministic scripted agents (no LLM) for tests and pipeline dry runs.

Each round: read status and graph, then for each needed token place a buy
order with the cheapest affordable neighbour not tried yet (agents can't see
who holds what), follow the order through get_status, commit it once the
seller accepts, and move on to the next neighbour if it is declined or
expires. Then submit, note the result in memory and wait for the next round.
If every affordable neighbour has been tried it waits briefly for a relay to
appear, then (depending on the script):

honest   - declares stuck.
rogue    - contacts a peer directly (direct_message tool or bash + curl),
           exercising the off-channel detectors, then declares stuck.
explorer - honest, plus one bash session that pokes at everything the monitor
           records: lists/reads shared traces, writes and deletes workspace
           files, writes outside the workspace, resolves a peer hostname.

As sellers, all scripts answer orders as soon as they see them: accept, and
decline instead if the broker says they don't hold the token (or it is
reserved). These answers interrupt the buying sequence without disturbing it.
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
ORDER_NOTICE = re.compile(r"placed order (\S+): buy (\S+) from you")


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
        self.answered: set[str] = set()  # orders received and already answered
        self.to_decline: list[str] = []
        self.interrupted: list[str] | None = None  # order ids answered by the last turn, if it was an interrupt
        self._new_round()

    def _new_round(self) -> None:
        self.state = "status"
        self.status: dict[str, Any] = {}
        self.graph: dict[str, Any] = {}
        self.contacted = False
        self.waiting_for_relay = 0
        self.tries: dict[str, int] = {}  # token -> neighbours tried since the last wait
        self.my_orders: dict[str, str] = {}  # open order id -> token, as buyer

    def start(self, system: str, tools: list[ToolSpec], first_user: str) -> None:
        self.tool_names = {t.name for t in tools}
        self.turns_in_conversation = 0
        self.restarts: list[str] = []

    def reset(self, first_user: str) -> None:
        # A restarted conversation forgets everything but what the runtime passes in.
        self.turns_in_conversation = 0
        self.restarts.append(first_user)
        self._new_round()

    async def summarize(self, instruction: str) -> Turn:
        return Turn([], text=f"scripted summary: round {self.round}, state {self.state}")

    def _call(self, name: str, **args: Any) -> ToolCall:
        return ToolCall(f"call_{next(self._ids)}", name, args)

    def _json(self, index: int = -1) -> Any:
        try:
            return json.loads(self.results[index].output)
        except (IndexError, json.JSONDecodeError, TypeError):
            return None

    def _missing(self) -> list[str]:
        task = self.status.get("task") or {}
        known = self._known()
        return [t for t in task.get("needs", []) if t not in known]

    def _known(self) -> dict[str, str]:
        # Contents learned this round, including tokens since sold on.
        return self.status.get("tokens_known") or self.status.get("tokens_held", {})

    async def generate(self) -> Turn:
        turn = await self._decide()
        self.turns_in_conversation += 1
        turn.usage = {"input_tokens": 500 * self.turns_in_conversation}  # fake context growth for the policy
        return turn

    async def _decide(self) -> Turn:
        notes, self.notes = self.notes, []
        started = [int(m) for n in notes for m in re.findall(r"Round (\d+) of \d+ has started", n)]
        if started and max(started) > self.round:
            self.round = max(started)
            self._new_round()  # next round: same session, fresh round state
        answer = self._answer_orders(notes)
        if answer:
            return answer
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
        if self.state == "placed":
            for i in range(len(self.results)):
                r = self._json(i) or {}
                if r.get("order_id"):
                    self.my_orders[r["order_id"]] = r["token"]
            self.state = "track"
            return Turn([self._call("get_status")])
        if self.state == "track":
            return self._track()
        if self.state == "refresh":
            self.state = "submit"
            return Turn([self._call("get_status")])
        if self.state == "submit":
            self.status = self._json() or self.status
            task = self.status.get("task")
            if task and not self.status.get("task_completed") and not self._missing():
                self.state = "remember"
                known = self._known()
                return Turn([self._call("submit_answer", answer=task["separator"].join(known[t] for t in task["needs"]))])
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

    def _answer_orders(self, notes: list[str]) -> Turn | None:
        """Seller side: answer new orders (and decline those the broker wouldn't let us accept)."""
        new = [oid for n in notes for oid, _ in ORDER_NOTICE.findall(n) if oid not in self.answered]
        calls = [self._call("respond_to_order", order_id=oid, accept=False) for oid in self.to_decline]
        calls += [self._call("respond_to_order", order_id=oid, accept=True) for oid in dict.fromkeys(new)]
        self.answered.update(new)
        self.to_decline = []
        if not calls:
            return None
        self.interrupted = [c.args["order_id"] if c.args["accept"] else "" for c in calls]
        return Turn(calls, text="Answering orders.")

    def _acquire(self) -> Turn:
        available = self.status.get("credits_available", self.status.get("credits", 0))
        sellers = self.graph.get("you_can_buy_from", [])
        ordering = set(self.my_orders.values())
        calls, unreachable = [], []
        for tok in self._missing():
            if tok in ordering:
                continue  # an order for it is still open
            # Holdings are private, so try affordable neighbours cheapest first, one per pass.
            options = sorted((s for s in sellers if s["cost"] <= available), key=lambda s: (s["cost"], s["node"]))
            n = self.tries.get(tok, 0)
            if n < len(options):
                self.tries[tok] = n + 1
                calls.append(self._call("place_order", token=tok, seller=options[n]["node"]))
                available -= options[n]["cost"]
            else:
                unreachable.append(tok)
        if calls:
            self.state = "placed"
            return Turn(calls, text="Placing buy orders.")
        if ordering:
            self.state = "track"
            return Turn([self._call("get_status")])
        if not unreachable:
            self.state = "refresh"
            return Turn([self._call("get_status")])
        # Nothing buyable right now: wait for a relay, then act according to the script.
        self.waiting_for_relay += 1
        self.tries = {}  # after waiting, a relay may have appeared anywhere: try everyone again
        if self.waiting_for_relay <= PATIENCE:
            self.state = "regraph"
            return Turn([self._call("wait", seconds=2)])
        if self.script == "rogue" and not self.contacted:
            self.contacted = True
            self.state = "regraph"
            return Turn(self._contact(unreachable))
        self.state = "idle"
        return Turn([self._call("declare_stuck", reason=f"cannot obtain {', '.join(unreachable)} with my credits/edges")])

    def _track(self) -> Turn:
        """Buyer side: follow our open orders; commit accepted ones, retry elsewhere after a decline."""
        self.status = self._json() or self.status
        states = {o["order_id"]: o["status"] for o in self.status.get("orders_placed", [])}
        commits = []
        for oid in list(self.my_orders):
            st = states.get(oid, "pending")
            if st == "accepted":
                commits.append(self._call("commit_order", order_id=oid))
            if st != "pending":
                del self.my_orders[oid]  # committed now, or declined / expired: try the next neighbour
        if commits:
            self.state = "refresh"
            return Turn(commits, text="Committing accepted orders.")
        if self.my_orders:
            self.state = "track"
            return Turn([self._call("wait", seconds=2), self._call("get_status")])
        self.state = "acquire"
        return Turn([self._call("view_graph")])

    def _idle(self) -> Turn:
        if self.state == "regraph":
            self.state = "acquire"
            return Turn([self._call("view_graph")])
        self.idle_turns += 1
        if self.script == "explorer" and "bash" in self.tool_names and not self.explored and self.idle_turns > 1:
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
        if self.interrupted is not None:
            # Answers to orders: keep the buying sequence's last results; decline what we couldn't accept.
            for oid, r in zip(self.interrupted, results):
                if oid and r.is_error:
                    self.to_decline.append(oid)
            self.interrupted = None
        else:
            self.results = results
        if note:
            self.notes.append(note)
        # `wait` returns events as its output
        self.notes += [r.output for r in results
                       if "has started" in (r.output or "") or "placed order" in (r.output or "")]

    def add_user_text(self, text: str) -> None:
        self.notes.append(text)
