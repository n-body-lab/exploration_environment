"""Gateway + provider adapters, end to end over real HTTP with a mocked upstream."""

import asyncio
import json
import socket

import httpx
import pytest
import uvicorn

from hivemarket.agent.providers.anthropic_provider import AnthropicProvider
from hivemarket.agent.providers.base import ToolResult
from hivemarket.agent.providers.openai_provider import OpenAIProvider
from hivemarket.agent.tools import tools_for
from hivemarket.common.config import AgentSpec
from hivemarket.common.events import read_jsonl
from hivemarket.common.manifest import AgentEntry, Manifest, ServiceEntry
from hivemarket.gateway.app import create_app

seen: list[httpx.Request] = []


def upstream(request: httpx.Request) -> httpx.Response:
    seen.append(request)
    if request.url.host == "api.anthropic.com":
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [
                {"type": "thinking", "thinking": "I need P-02.", "signature": "sig"},
                {"type": "text", "text": "Buying."},
                {"type": "tool_use", "id": "toolu_1", "name": "buy", "input": {"item_id": "P-02", "max_price": 20}},
            ],
            "stop_reason": "tool_use", "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }, headers={"request-id": "req_123"})
    return httpx.Response(200, json={
        "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "gpt-x",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "reasoning_content": "thinking about the catalog",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "view_catalog", "arguments": "{}"}}]}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
    })


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
async def gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "real-openai-key")
    port = free_port()
    manifest = Manifest(
        run_id="r", experiment="t", mode="local", admin_token="adm", duration_seconds=60,
        agents=[AgentEntry(id="agent-1", token="tok1", ip="127.0.0.1", inbox_host="127.0.0.1", inbox_port=1,
                           provider="anthropic", model="claude-opus-5-5")],
        services={"gateway": ServiceEntry(ip="127.0.0.1", url=f"http://127.0.0.1:{port}")},
    )
    app = create_app(manifest, tmp_path, transport=httpx.MockTransport(upstream))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    seen.clear()
    yield f"http://127.0.0.1:{port}", tmp_path
    server.should_exit = True
    await task


async def test_anthropic_through_gateway(gateway):
    url, run_dir = gateway
    p = AnthropicProvider(AgentSpec(), f"{url}/anthropic", "tok1")
    p.start("system prompt", tools_for("shell"), "go")
    turn = await p.generate()
    assert turn.tool_calls[0].name == "buy" and turn.tool_calls[0].args["item_id"] == "P-02"
    assert turn.reasoning == ["I need P-02."] and turn.request_id == "req_123"

    sent = seen[-1]
    assert sent.headers["x-api-key"] == "real-anthropic-key"  # agent token swapped for the real key
    assert "server-side-fallback-2026-07-01" in sent.headers["anthropic-beta"]
    body = json.loads(sent.content)
    assert body["fallbacks"] == "default"
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "high"}
    assert {t["name"] for t in body["tools"]} >= {"place_order", "commit_order", "bash"}

    # history is append-only and the thinking block is echoed back unchanged
    p.add_tool_results([ToolResult("toolu_1", "ok")], "[New events] hi")
    await p.generate()
    body2 = json.loads(seen[-1].content)
    assert body2["messages"][1]["content"][0]["type"] == "thinking"
    assert body2["messages"][2]["content"][0]["type"] == "tool_result"

    p.add_tool_results([ToolResult("toolu_1", "ok")], None)  # as the runtime does before a checkpoint
    await p.summarize("write a summary")
    body3 = json.loads(seen[-1].content)
    assert body3["tool_choice"] == {"type": "none"}
    assert body3["messages"][-1]["content"][-1] == {"type": "text", "text": "write a summary"}
    p.reset("fresh")
    assert p.messages == [{"role": "user", "content": "fresh"}]

    log = read_jsonl(run_dir / "logs" / "gateway.jsonl")
    calls = [e for e in log if e["kind"] == "llm_call"]
    assert calls[0]["agent_id"] == "agent-1" and calls[0]["data"]["usage"]["input_tokens"] == 100


async def test_openai_through_gateway(gateway):
    url, run_dir = gateway
    spec = AgentSpec(provider="openai", model="gpt-x", max_tokens=4096, extra_body={"enable_thinking": False})
    p = OpenAIProvider(spec, f"{url}/openai", "tok1", max_tokens_param="max_tokens")
    p.start("sys", tools_for("none"), "go")
    turn = await p.generate()
    assert turn.tool_calls[0].name == "view_catalog"
    assert turn.reasoning == ["thinking about the catalog"]
    assert seen[-1].headers["authorization"] == "Bearer real-openai-key"
    assert str(seen[-1].url) == "https://api.openai.com/v1/chat/completions"
    body = json.loads(seen[-1].content)
    assert body["max_tokens"] == 4096 and "max_completion_tokens" not in body
    assert "reasoning_effort" not in body  # only sent when configured
    assert body["enable_thinking"] is False  # extra_body passthrough
    assert "reasoning_content" not in p.messages[-1]  # reasoning stays out of the history

    await p.summarize("summarize please")
    body = json.loads(seen[-1].content)
    assert body["tool_choice"] == "none" and body["messages"][-1]["content"] == "summarize please"
    p.reset("fresh start")
    assert [m["role"] for m in p.messages] == ["system", "user"]


async def test_gateway_rejects_unknown_token(gateway):
    url, run_dir = gateway
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{url}/anthropic/v1/messages", headers={"x-api-key": "stolen"}, json={})
    assert r.status_code == 401
    assert not seen
    assert any(e["kind"] == "unauthenticated_request" for e in read_jsonl(run_dir / "logs" / "gateway.jsonl"))


async def test_adaption_route_uses_its_key_and_url(gateway, monkeypatch):
    url, _ = gateway
    monkeypatch.setenv("ADAPTION_API_KEY", "real-adaption-key")
    spec = AgentSpec(provider="adaption", model="qwen3.5-397b", max_tokens=1024)
    p = OpenAIProvider(spec, f"{url}/adaption", "tok1")
    p.start("sys", tools_for("none"), "go")
    turn = await p.generate()
    assert turn.tool_calls[0].name == "view_catalog"
    assert str(seen[-1].url) == "https://api.prod.adaptionlabs.ai/api/v1/chat/completions"
    assert seen[-1].headers["authorization"] == "Bearer real-adaption-key"  # agent token swapped for the key
    assert json.loads(seen[-1].content)["max_tokens"] == 1024
