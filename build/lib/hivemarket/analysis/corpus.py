"""Run loading and the text corpus every text analysis works on.

A text unit is one piece of agent-authored text: model output, a reasoning
step, a broker message, a backchannel payload, a memory write, a round
summary, or a shell command.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from hivemarket.common.events import read_jsonl
from hivemarket.common.manifest import Manifest


@dataclass
class Run:
    run_dir: Path
    manifest: Manifest
    world: dict[str, Any]
    events: list[dict[str, Any]]
    round_starts: list[tuple[float, int]] = field(default_factory=list)
    contents: dict[int, dict[str, str]] = field(default_factory=dict)  # round -> token -> content

    @property
    def run_id(self) -> str:
        return self.manifest.run_id

    @property
    def agent_ids(self) -> list[str]:
        return [a.id for a in self.manifest.agents]

    def round_at(self, ts: float) -> int | None:
        i = bisect.bisect_right([t for t, _ in self.round_starts], ts) - 1
        return self.round_starts[i][1] if i >= 0 else None

    def ip_to_agent(self) -> dict[str, str]:
        return {a.ip: a.id for a in self.manifest.agents} if self.manifest.mode == "docker" else {}

    def tokens_in(self, text: str, round_no: int | None) -> list[str]:
        """Token names whose content (for that round, or any round if unknown) appears in text."""
        if not text:
            return []
        rounds = [round_no] if round_no in self.contents else list(self.contents)
        return sorted({t for r in rounds for t, c in self.contents.get(r, {}).items() if c and c in text})


def load_run(run_dir: Path) -> Run:
    run_dir = Path(run_dir)
    events: list[dict[str, Any]] = []
    for path in sorted((run_dir / "logs").glob("*.jsonl")):
        events.extend(read_jsonl(path))
    events.sort(key=lambda e: e.get("ts", 0))
    run = Run(run_dir, Manifest.load(run_dir / "manifest.json"), json.loads((run_dir / "world.json").read_text()), events)
    for e in events:
        if e.get("source") == "market" and e.get("kind") == "round_start":
            r = e["data"]["round"]
            run.round_starts.append((e["ts"], r))
            run.contents[r] = e["data"].get("contents") or {}
    return run


@dataclass
class TextUnit:
    unit_id: str
    run_id: str
    ts: float
    round: int | None
    agent_id: str
    kind: str  # text | reasoning | broker_message | backchannel_out | backchannel_in | memory | summary | bash
    text: str
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_corpus(run: Run, min_chars: int = 3) -> list[TextUnit]:
    units: list[TextUnit] = []

    def add(e: dict, i: int, kind: str, text: Any, agent: str | None = None, **meta: Any) -> None:
        text = text if isinstance(text, str) else json.dumps(text) if text else ""
        agent = agent or e.get("agent_id")
        if agent and len(text.strip()) >= min_chars:
            d = e.get("data", {})
            rnd = d.get("round") if isinstance(d.get("round"), int) else run.round_at(e["ts"])
            units.append(TextUnit(f"{e['event_id']}:{i}", run.run_id, e["ts"], rnd, agent, kind, text, meta))

    for e in run.events:
        src, kind, d = e.get("source"), e.get("kind"), e.get("data", {})
        if src == "agent":
            if kind == "llm_turn":
                add(e, 0, "text", d.get("text"))
                for i, r in enumerate(d.get("reasoning") or []):
                    add(e, i + 1, "reasoning", r)
            elif kind == "tool_call":
                args = d.get("args") or {}
                if d.get("name") == "bash":
                    add(e, 0, "bash", args.get("command"), output=str(d.get("output", ""))[:2000])
                elif d.get("name") == "direct_message":
                    add(e, 0, "backchannel_out", args.get("message"), to=args.get("peer"))
                elif d.get("name") == "memory" and args.get("command") in ("write", "append"):
                    add(e, 0, "memory", args.get("content"), path=args.get("path"))
            elif kind == "context_checkpoint":
                add(e, 0, "summary", d.get("summary"))
            elif kind == "direct_inbound":
                add(e, 0, "backchannel_in", d.get("body"), src_ip=d.get("src_ip"))
        elif src == "market" and kind == "message":
            add(e, 0, "broker_message", d.get("body"), agent=d.get("sender"), to=d.get("to"))
    return units
