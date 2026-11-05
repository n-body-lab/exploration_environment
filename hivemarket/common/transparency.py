"""What one agent may see of another agent's trace.

Shared by the collector (tool mode) and the monitor sidecar (files mode) so
both modes expose exactly the same records.
"""

from __future__ import annotations

from typing import Any, Iterable

MAX_OUTPUT_CHARS = 4000


def project(event: dict[str, Any], expose: Iterable[str]) -> dict[str, Any] | None:
    """Reduce an agent-trace event to the exposed fields, or None if nothing is exposed."""
    expose = set(expose)
    kind, d = event.get("kind"), event.get("data") or {}
    base = {"ts": event.get("ts_iso") or event.get("ts"), "step": d.get("step")}
    if kind == "llm_turn":
        out: dict[str, Any] = {}
        if "text" in expose and d.get("text"):
            out["text"] = d["text"]
        if "reasoning" in expose and d.get("reasoning"):
            out["reasoning"] = d["reasoning"]
        if "tool_calls" in expose and d.get("tool_calls"):
            out["tool_calls"] = [{"name": c["name"], "args": c["args"]} for c in d["tool_calls"]]
        return {**base, "type": "turn", **out} if out else None
    if kind == "tool_call" and "tool_results" in expose:
        output = str(d.get("output", ""))
        return {**base, "type": "tool_result", "tool": d.get("name"), "is_error": d.get("is_error"),
                "output": output[:MAX_OUTPUT_CHARS]}
    return None
