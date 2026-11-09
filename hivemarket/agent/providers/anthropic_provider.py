"""Claude via the Anthropic SDK, routed through the logging gateway."""

from __future__ import annotations

from typing import Any

import anthropic

from hivemarket.agent.providers.base import ToolCall, ToolResult, Turn
from hivemarket.agent.tools import ToolSpec
from hivemarket.common.config import AgentSpec

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    def __init__(self, spec: AgentSpec, base_url: str, token: str):
        self.spec = spec
        # The agent token stands in for the API key; the gateway swaps in the real one.
        self.client = anthropic.AsyncAnthropic(base_url=base_url, api_key=token, max_retries=4, timeout=600.0)
        self.system = ""
        self.tools: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []

    def start(self, system: str, tools: list[ToolSpec], first_user: str) -> None:
        self.system = system
        self.tools = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        self.messages = [{"role": "user", "content": first_user}]

    def reset(self, first_user: str) -> None:
        # A new conversation, never an edited one: preserved thinking stays valid.
        self.messages = [{"role": "user", "content": first_user}]

    async def summarize(self, instruction: str) -> Turn:
        self.add_user_text(instruction)
        return await self.generate(tool_choice={"type": "none"})

    async def generate(self, tool_choice: dict[str, Any] | None = None) -> Turn:
        kwargs: dict[str, Any] = {
            "model": self.spec.model,
            "max_tokens": self.spec.max_tokens,
            "system": self.system,
            "tools": self.tools,
            "messages": self.messages,
            "cache_control": {"type": "ephemeral"},
        }
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        if self.spec.thinking == "adaptive":
            # Summaries of the model's reasoning are kept in the trace for analysis.
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
            kwargs["output_config"] = {"effort": self.spec.effort or "high"}
        elif self.spec.effort:
            kwargs["output_config"] = {"effort": self.spec.effort}
        extra = dict(self.spec.extra_body)
        if self.spec.temperature is not None:
            # In the body: this SDK's beta create() has no temperature argument.
            extra["temperature"] = self.spec.temperature
        if extra:
            kwargs["extra_body"] = extra
        if self.spec.fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        resp = await self.client.beta.messages.create(**kwargs)
        # Append the content unchanged: thinking and fallback blocks must be echoed back as-is.
        self.messages.append({"role": "assistant", "content": resp.content})

        calls, texts, reasoning, fallbacks = [], [], [], []
        for block in resp.content:
            if block.type == "tool_use":
                calls.append(ToolCall(block.id, block.name, dict(block.input or {})))
            elif block.type == "text":
                texts.append(block.text)
            elif block.type == "thinking" and block.thinking:
                reasoning.append(block.thinking)
            elif block.type == "fallback":
                fallbacks.append({"from": block.from_.model, "to": block.to.model})
        return Turn(
            tool_calls=calls,
            text="\n".join(texts),
            reasoning=reasoning,
            stop_reason=resp.stop_reason,
            usage=resp.usage.to_dict() if resp.usage else None,
            model=resp.model,
            request_id=resp._request_id,
            refused=resp.stop_reason == "refusal",
            extra={
                "fallbacks": fallbacks,
                "stop_details": resp.stop_details.to_dict() if getattr(resp, "stop_details", None) else None,
            },
        )

    def add_tool_results(self, results: list[ToolResult], note: str | None) -> None:
        content: list[dict[str, Any]] = [
            {"type": "tool_result", "tool_use_id": r.id, "content": r.output, "is_error": r.is_error} for r in results
        ]
        if note:
            content.append({"type": "text", "text": note})
        self.messages.append({"role": "user", "content": content})

    def add_user_text(self, text: str) -> None:
        last = self.messages[-1] if self.messages else None
        if last and last["role"] == "user" and isinstance(last["content"], list):
            last["content"].append({"type": "text", "text": text})  # keep roles alternating
        else:
            self.messages.append({"role": "user", "content": text})
