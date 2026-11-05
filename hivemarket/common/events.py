"""Shared event schema and sinks.

Every log line written by any component is one JSON object with the same
envelope, so all sources can be merged into a single timeline:

    event_id   unique id
    run_id     experiment run
    ts         epoch seconds (float, microsecond resolution) when it happened
    ts_iso     same, ISO-8601 UTC
    source     component that observed it: market | gateway | collector |
               agent | monitor | orchestrator
    agent_id   agent the event is attributed to (None for global events)
    kind       event type, e.g. "trade", "llm_turn", "flow"
    data       kind-specific payload
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="microseconds")


def make_event(
    source: str,
    kind: str,
    data: dict[str, Any] | None = None,
    *,
    agent_id: str | None = None,
    run_id: str | None = None,
    ts: float | None = None,
) -> dict[str, Any]:
    ts = now() if ts is None else ts
    return {
        "event_id": uuid.uuid4().hex,
        "run_id": run_id,
        "ts": ts,
        "ts_iso": iso(ts),
        "source": source,
        "agent_id": agent_id,
        "kind": kind,
        "data": data or {},
    }


class JsonlSink:
    """Append-only JSONL file, flushed per line so a crash loses nothing."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, default=str, ensure_ascii=False)
        with self._lock:
            self._f.write(line + "\n")
            self._f.flush()

    def close(self) -> None:
        with self._lock:
            self._f.close()


class EventLog:
    """Emits envelope-wrapped events for one source to a sink."""

    def __init__(self, source: str, run_id: str, sink: JsonlSink, echo: bool = False):
        self.source = source
        self.run_id = run_id
        self.sink = sink
        self.echo = echo

    def emit(self, kind: str, agent_id: str | None = None, **data: Any) -> dict[str, Any]:
        event = make_event(self.source, kind, data, agent_id=agent_id, run_id=self.run_id)
        self.sink.write(event)
        if self.echo:
            print(json.dumps({"kind": kind, "agent_id": agent_id, **data}, default=str)[:400], flush=True)
        return event


def read_jsonl(path: str | os.PathLike) -> list[dict[str, Any]]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out
