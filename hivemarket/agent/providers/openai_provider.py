"""OpenAI-compatible chat completions (OpenAI, Gemini, OpenRouter, vLLM, Ollama, ...)."""

from __future__ import annotations

import json
from typing import Any

import openai

from hivemarket.agent.providers.base import ToolCall, ToolResult, Turn
from hivemarket.agent.tools import ToolSpec
from hivemarket.common.config import AgentSpec


class OpenAIProvider:
    def __init__(self, spec: AgentSpec, base_url: str, token: str):
        self.spec = spec
        self.client = openai.AsyncOpenAI(base_url=base_url, api_key=token, max_retries=4, timeout=600.0)
        self.tools: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []

    def start(self, system: str, tools: list[ToolSpec], first_user: str) -> None:
        self.tools = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in tools
        ]
        self.messages = [{"role": "system", "content": system}, {"role": "user", "content": first_user}]

    async def generate(self) -> Turn:
        kwargs: dict[str, Any] = {
            "model": self.spec.model,
            "messages": self.messages,
            "tools": self.tools,
            "max_completion_tokens": self.spec.max_tokens,
        }
        if self.spec.effort:
            kwargs["reasoning_effort"] = self.spec.effort
        resp = await self.client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message
        self.messages.append(msg.model_dump(exclude_none=True))

        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
                calls.append(ToolCall(tc.id, tc.function.name, args if isinstance(args, dict) else {}))
            except json.JSONDecodeError as e:
                calls.append(ToolCall(tc.id, tc.function.name, {}, parse_error=str(e)))
        reasoning = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
        return Turn(
            tool_calls=calls,
            text=msg.content or "",
            reasoning=[reasoning] if isinstance(reasoning, str) and reasoning else [],
            stop_reason=choice.finish_reason,
            usage=resp.usage.model_dump() if resp.usage else None,
            model=resp.model,
            request_id=getattr(resp, "_request_id", None) or resp.id,
            refused=bool(getattr(msg, "refusal", None)),
        )

    def add_tool_results(self, results: list[ToolResult], note: str | None) -> None:
        for r in results:
            self.messages.append({"role": "tool", "tool_call_id": r.id, "content": r.output})
        if note:
            self.messages.append({"role": "user", "content": note})

    def add_user_text(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
