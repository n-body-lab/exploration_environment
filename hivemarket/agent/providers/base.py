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

    async def generate(self) -> Turn: ...

    def add_tool_results(self, results: list[ToolResult], note: str | None) -> None: ...

    def add_user_text(self, text: str) -> None: ...
