"""Agent runtime round transitions and provider history, with fakes (no Docker, no LLM)."""

import json
from types import SimpleNamespace

import httpx
import openai

from hivemarket.agent.providers.openai_provider import OpenAIProvider
from hivemarket.agent.runtime import AgentRuntime
from hivemarket.agent.tools import tools_for
from hivemarket.common.config import AgentSpec


class FakeMarket:
    """Serves queued inbox batches; then reports the market closed."""

    def __init__(self, batches):
        self.batches = list(batches)

    async def inbox(self, cursor, wait=0):
        if self.batches:
            return 200, {"events": self.batches.pop(0), "market_open": True}
        return 200, {"events": [], "market_open": False}


def runtime(batches):
    rt = AgentRuntime.__new__(AgentRuntime)  # skip the network setup in __init__
    rt.cfg = SimpleNamespace(context=SimpleNamespace(reset_each_round=True),
                             affordance=SimpleNamespace(deliver_direct_messages=True))
    rt.telemetry = SimpleNamespace(emit=lambda *a, **k: None)
    rt.inbox = SimpleNamespace(drain=lambda: [])
    rt.market = FakeMarket(batches)
    rt.cursor = rt.steps_in_round = 0
    rt.round_no, rt.last_round_ended, rt.round_deadline = 1, 0, None
    rt.market_closed, rt.pending_checkpoint = False, None
    return rt


def notice(seq, event, rnd):
    return {"seq": seq, "ts": 0, "from": "broker", "type": "notice", "body": event, "event": event, "round": rnd}


async def test_round_end_and_start_in_one_batch_does_not_skip_a_round():
    # A slow model turn: round 1 ends and round 2 starts before the agent next polls.
    rt = runtime([[notice(1, "round_end", 1), notice(2, "round_start", 2)]])
    await rt._collect_events()
    assert rt.round_no == 2 and rt.last_round_ended == 1 and rt.pending_checkpoint == "round_end"
    await rt._await_round_start()
    assert rt.round_no == 2 and not rt.market_closed  # plays round 2 instead of waiting for round 3


async def test_await_round_start_waits_for_next_round_and_drops_stale_checkpoint():
    rt = runtime([[notice(1, "round_end", 1)], [notice(2, "round_start", 2)]])
    await rt._collect_events()
    assert rt.round_no == 1 and rt.last_round_ended == 1
    await rt._await_round_start()
    assert rt.round_no == 2 and rt.pending_checkpoint is None


async def test_reply_cut_off_while_reasoning_is_not_kept_in_history():
    sent = []

    def upstream(request):
        sent.append(json.loads(request.content))
        if len(sent) == 1:  # hit max_tokens mid-reasoning: no content, no tool calls
            msg, finish = {"role": "assistant", "content": None, "reasoning_content": "long thought..."}, "length"
        else:
            msg, finish = {"role": "assistant", "content": "ok"}, "stop"
        return httpx.Response(200, json={"id": "c", "object": "chat.completion", "created": 0, "model": "q",
                                         "choices": [{"index": 0, "finish_reason": finish, "message": msg}]})

    p = OpenAIProvider(AgentSpec(provider="adaption", model="q"), "http://upstream/v1", "k")
    p.client = openai.AsyncOpenAI(base_url="http://upstream/v1", api_key="k", max_retries=0,
                                  http_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    p.start("sys", tools_for("none"), "go")
    turn = await p.generate()
    assert turn.stop_reason == "length" and not turn.tool_calls and turn.reasoning == ["long thought..."]
    p.add_user_text("No new events. Continue.")
    await p.generate()
    assert [m["role"] for m in sent[1]["messages"]] == ["system", "user", "user"]  # no empty assistant message
    assert p.messages[-1] == {"role": "assistant", "content": "ok"}
