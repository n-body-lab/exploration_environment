"""Provider interface.

Each provider owns its native, append-only conversation history (never
rewritten, so provider-side features such as preserved thinking and prompt
caching keep working) and translates the neutral tool specs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from hivemarket.agent.tools import ToolSpec


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]
    parse_error: str | None = None


@dataclass
class ToolResult:
    id: str
    output: str
    is_error: bool = False


@dataclass
class Turn:
    tool_calls: list[ToolCall]
    text: str = ""
    reasoning: list[str] = field(default_factory=list)  # thinking summaries, when the provider returns them
    stop_reason: str | None = None
    usage: dict[str, Any] | None = None
    model: str | None = None  # model that actually served the turn
    request_id: str | None = None
    refused: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    def start(self, system: str, tools: list[ToolSpec], first_user: str) -> None: ...

    def reset(self, first_user: str) -> None:
        """Start a fresh conversation (same system prompt and tools)."""

    async def generate(self) -> Turn: ...

    async def summarize(self, instruction: str) -> Turn:
        """Append `instruction` and get a text-only reply (tools disabled). Both stay in the history: a
        checkpoint resets it afterwards, a round-end reflection keeps it."""

    def add_tool_results(self, results: list[ToolResult], note: str | None) -> None: ...

    def add_user_text(self, text: str) -> None: ...


def context_tokens(usage: dict | None) -> int:
    """Size of the conversation as last sent and answered, from provider usage fields."""
    if not usage:
        return 0
    if "prompt_tokens" in usage:  # OpenAI-style
        return int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
    return sum(int(usage.get(k) or 0) for k in
               ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
