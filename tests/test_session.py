"""How an agent's session carries across rounds (session = fresh | memory) and round-end reflections
(rounds.reflection)."""

import json
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest
import yaml
from pydantic import ValidationError

from hivemarket.agent.prompts import context_text, restart_text
from hivemarket.agent.providers.anthropic_provider import AnthropicProvider
from hivemarket.agent.providers.base import Turn
from hivemarket.agent.providers.openai_provider import OpenAIProvider
from hivemarket.agent.runtime import AgentRuntime
from hivemarket.agent.tools import tools_for
from hivemarket.common.config import AgentSpec, ExperimentConfig
from hivemarket.common.events import read_jsonl
from hivemarket.orchestrator.local import run_local
from hivemarket.orchestrator.report import write_report
from hivemarket.orchestrator.run import create_run

EXPERIMENTS = Path(__file__).resolve().parents[1] / "experiments"


def config(**extra) -> ExperimentConfig:
    return ExperimentConfig.model_validate({"name": "s", "agents": [{"provider": "scripted", "count": 2}], **extra})


# ------------------------------------------------------------------ config


def test_memory_is_the_default_and_fresh_turns_the_memory_tool_off():
    assert config().session == "memory" and config().memory.enabled and not config().rounds.reflection
    fresh = config(session="fresh")
    assert not fresh.memory.enabled
    assert ExperimentConfig.model_validate(fresh.model_dump()).session == "fresh"  # its own dump validates


@pytest.mark.parametrize("extra, error", [
    ({"session": "fresh", "memory": {"enabled": True}}, "carries nothing across rounds"),
    ({"context": {"reset_each_round": False}}, "replaced by the experiment's `session`"),
    ({"session": "forgetful"}, "fresh"),
    ({"session": "reflection"}, "replaced by rounds.reflection: true"),
])
def test_conflicting_or_replaced_settings_are_rejected(extra, error):
    with pytest.raises(ValidationError, match=error):
        config(**extra)


# ----------------------------------------------------------------- prompts


def agent_cfg(session, memory=True, reflection=False):
    return SimpleNamespace(session=session, memory=SimpleNamespace(enabled=memory), reflection=reflection,
                           spec=SimpleNamespace(prompt_vars={}))


def test_the_prompt_describes_the_session():
    assert "won't remember earlier rounds" in context_text(agent_cfg("fresh", memory=False))
    assert "continues from one round to the next" in context_text(agent_cfg("memory"))
    assert "reflect on how it went" not in context_text(agent_cfg("memory"))
    kept = context_text(agent_cfg("memory", reflection=True))
    assert "reflect on how it went; your reflection stays in your conversation." in kept
    dropped = context_text(agent_cfg("fresh", memory=False, reflection=True))
    assert "reflect on how it went." in dropped and "stays" not in dropped  # fresh carries nothing over
    assert "memory files" not in restart_text("context_limit", {}, [], None)


# --------------------------------------------- a reflection stays in the conversation


def reflection_runtime(provider, market):
    rt = AgentRuntime.__new__(AgentRuntime)  # skip the network setup in __init__
    rt.cfg = agent_cfg("memory", reflection=True)
    rt.cfg.context = SimpleNamespace(summary_chars=3000)
    rt.cfg.affordance = SimpleNamespace(deliver_direct_messages=True)
    rt.provider, rt.market, rt.inbox = provider, market, SimpleNamespace(drain=lambda: [])
    rt.steps = rt.steps_in_round = rt.cursor = 0
    rt.round_no, rt.last_round_ended, rt.round_deadline = 1, 1, None
    rt.budget, rt.pending_checkpoint, rt.round_over, rt.market_closed = 10**9, None, None, False
    rt.summaries, rt.events, rt.restarts = [], [], 0
    rt.telemetry = SimpleNamespace(emit=lambda kind, **d: rt.events.append((kind, d)))
    return rt


class EmptyMarket:
    """The inbox is empty."""

    async def inbox(self, cursor, wait=0):
        return 200, {"events": [], "market_open": True}


REFLECTION = "I offered too late; next round I will offer first."


def openai_provider(sent):
    def upstream(request):
        sent.append(json.loads(request.content))
        reply = REFLECTION if len(sent) == 1 else "ok"
        return httpx.Response(200, json={"id": "c", "object": "chat.completion", "created": 0, "model": "m",
                                         "choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", "content": reply}}]})

    p = OpenAIProvider(AgentSpec(provider="openai", model="m"), "http://upstream/v1", "k")
    p.client = openai.AsyncOpenAI(base_url="http://upstream/v1", api_key="k", max_retries=0,
                                  http_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    return p


def anthropic_provider(sent):
    import httpx2  # the anthropic SDK's own HTTP client

    def upstream(request):
        body = json.loads(request.content)
        sent.append(body)
        if body["messages"][-1]["role"] != "user":  # current Claude models reject an assistant prefill
            return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                          "message": "prefill not supported"}})
        reply = REFLECTION if len(sent) == 1 else "ok"
        return httpx2.Response(200, json={"id": "m", "type": "message", "role": "assistant", "model": "claude",
                                         "content": [{"type": "text", "text": reply}], "stop_reason": "end_turn",
                                         "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}})

    p = AnthropicProvider(AgentSpec(), "http://upstream", "k")
    p.client = anthropic.AsyncAnthropic(base_url="http://upstream", api_key="k", max_retries=0,
                                        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(upstream)))
    return p


@pytest.mark.parametrize("make_provider", [openai_provider, anthropic_provider])
async def test_the_reflection_appears_in_the_next_rounds_conversation(make_provider):
    sent = []
    provider = make_provider(sent)
    provider.start("sys", tools_for("none"), "go")
    rt = reflection_runtime(provider, EmptyMarket())

    await rt._end_of_round(end(1, 1))
    [(kind, data)] = rt.events
    assert kind == "round_reflection" and data["round"] == 1 and "offered too late" in data["text"]
    assert rt.steps_in_round == 0  # the runtime asked for it: not one of the agent's steps this round
    assert provider.messages[-1]["role"] == "user"  # so the next request isn't an assistant prefill

    await provider.generate()  # the agent's next turn, exactly as the loop would send it
    assert "offered too late" in json.dumps(sent[1]["messages"])


# ---------------------------------------------------------- loop edge cases


class FakeProvider:
    """Every turn calls no tool; records what the runtime adds to the conversation."""

    def __init__(self):
        self.log = []

    async def generate(self):
        self.log.append("generate")
        return Turn([], text="thinking", usage={"input_tokens": 100})

    async def summarize(self, instruction):
        self.log.append(("summarize", instruction))
        return Turn([], text="my reflection" if "reflect" in instruction else "my summary")

    def add_user_text(self, text):
        self.log.append(("user", text))

    def reset(self, first_user):
        self.log.append(("reset", first_user))


class ScriptedMarket:
    """Serves queued inbox batches of (events, market_open); /me reports the round the market is in."""

    def __init__(self, batches):
        self.batches, self.round = list(batches), 1

    async def inbox(self, cursor, wait=0):
        events, market_open = self.batches.pop(0) if self.batches else ([], False)
        self.round = max([e["round"] for e in events if e["event"] == "round_start"] + [self.round])
        return 200, {"events": events, "market_open": market_open}

    async def status(self, origin="agent"):
        return 200, {"round": self.round, "rounds_total": 2, "task": {"needs": ["A1"]}, "task_completed": False,
                     "credits": 7, "tokens_held": {}, "tokens_bought": {}}


def end(rnd, seq, reflection=True):
    """A broker round-end notice; with rounds.reflection its body ends with the reflection request."""
    body = f"Round {rnd} of 2 is over: FAIL. You did not complete your task."
    return {"seq": seq, "ts": 0, "type": "notice", "event": "round_end", "round": rnd, "objective": [1, 0, 0],
            "reflection": reflection, "body": body + ("\n\nBefore you continue, reflect on this round." if reflection else "")}


def begin(rnd, seq):
    return {"seq": seq, "ts": 0, "type": "notice", "body": f"Round {rnd} has started.", "event": "round_start",
            "round": rnd}


def loop_runtime(session, batches, limit=10):
    provider = FakeProvider()
    rt = reflection_runtime(provider, ScriptedMarket(batches))
    rt.cfg.session = session
    rt.cfg.max_steps_per_round, rt.cfg.idle_wait_seconds = limit, 0
    rt.last_round_ended = 0
    return rt, provider


async def test_the_last_round_is_reflected_on_when_the_game_ends_during_an_idle_wait():
    rt, _ = loop_runtime("memory", [([end(1, 1)], False)])  # round_end and the close in one batch
    assert await rt._loop() == "market_closed"
    assert [d["round"] for k, d in rt.events if k == "round_reflection"] == [1]


async def test_out_of_steps_the_reflection_answers_the_brokers_round_end_message():
    rt, provider = loop_runtime("memory", [([], True), ([end(1, 1)], True), ([begin(2, 2)], True)], limit=1)
    assert await rt._loop() == "market_closed"
    [prompt] = [x[1] for x in provider.log if x[0:1] == ("summarize",)]
    assert prompt == end(1, 1)["body"]  # the agent's own result and the request, as the broker wrote them


async def test_without_a_reflection_request_memory_takes_no_round_end_step():
    rt, provider = loop_runtime("memory", [([end(1, 1, reflection=False)], False)])
    assert await rt._loop() == "market_closed"
    assert not [k for k, _ in rt.events if k == "round_reflection"]
    assert not [x for x in provider.log if x[0:1] == ("summarize",)]


async def test_fresh_reflects_then_starts_the_next_round_without_it():
    rt, provider = loop_runtime("fresh", [([begin(2, 2)], True)])
    rt.last_round_ended = 1
    await rt._end_of_round(end(1, 1))
    assert [d["round"] for k, d in rt.events if k == "round_reflection"] == [1]
    [(_, first)] = [x for x in provider.log if x[0:1] == ("reset",)]
    assert "my reflection" not in first and rt.summaries == []  # fresh carries nothing over


async def test_fresh_drops_a_budget_checkpoint_left_over_from_the_ended_round():
    rt, provider = loop_runtime("fresh", [([begin(2, 2)], True)])
    rt.pending_checkpoint, rt.round_over, rt.last_round_ended = "context_limit", end(1, 1, reflection=False), 1
    rt.summaries = ["[round 1, context limit] old"]
    await rt._end_of_round(rt.round_over)
    assert rt.pending_checkpoint is None and rt.summaries == []
    [(_, first)] = [x for x in provider.log if x[0:1] == ("reset",)]
    assert first.startswith("Round 2 of 2 is under way")


# ------------------------------------------------- each mode in a real (scripted) run


async def run(tmp_path, session, port, reflection=False):
    cfg = yaml.safe_load((EXPERIMENTS / "local_fail.yaml").read_text()) | {"session": session}
    cfg["rounds"]["reflection"] = reflection
    exp = tmp_path / f"{session}-{reflection}.yaml"
    exp.write_text(yaml.safe_dump(cfg))
    run_dir = create_run(exp, tmp_path / "runs", mode="local", local_base_port=port)
    await run_local(run_dir)
    return run_dir, read_jsonl(run_dir / "logs" / "agent_traces.jsonl")


def of(traces, kind, agent):
    return [e for e in traces if e["kind"] == kind and e["agent_id"] == agent]


async def test_fresh_starts_every_round_from_nothing(tmp_path):
    run_dir, traces = await run(tmp_path, "fresh", 20100)
    [start] = of(traces, "runtime_start", "agent-2")
    assert start["data"]["session"] == "fresh" and "memory" not in start["data"]["tools"]
    [reset] = [e for e in of(traces, "context_reset", "agent-2") if e["data"]["reason"] == "new_round"]
    first = reset["data"]["first_message"]
    assert first.startswith("Round 2 of 2 is under way") and '"round": 2' in first  # just the current status
    assert "summar" not in first.lower() and "memory" not in first.lower() and "notes.md" not in first
    assert not (run_dir / "artifacts" / "agent-2" / "memory" / "notes.md").exists()
    write_report(run_dir)
    agent = json.loads((run_dir / "summary.json").read_text())["agents"]["agent-2"]
    assert (agent["round_resets"], agent["context_restarts"], agent["reflections"]) == (1, 0, 0)


async def test_memory_continues_the_same_conversation(tmp_path):
    _, traces = await run(tmp_path, "memory", 20150)
    assert {e["data"]["session"] for e in traces if e["kind"] == "runtime_start"} == {"memory"}
    assert not [e for e in traces if e["kind"] == "context_reset"]  # no reset at the round boundary
    assert {e["data"]["round"] for e in of(traces, "llm_turn", "agent-2")} == {1, 2}
    assert not [e for e in traces if e["kind"] == "round_reflection"]
    # rounds.reflection off: the round-end message reports results only
    ends = [m for e in traces if e["kind"] == "events_delivered" for m in e["data"]["market_events"]
            if m.get("event") == "round_end"]
    assert ends and not any(m["reflection"] or "reflect" in m["body"] for m in ends)


async def test_reflection_once_per_agent_per_round_before_the_next_round(tmp_path):
    run_dir, traces = await run(tmp_path, "memory", 20200, reflection=True)
    assert (run_dir / "reflection_prompt.md").exists()
    market = read_jsonl(run_dir / "logs" / "market.jsonl")
    ended = {e["data"]["round"]: e["ts"] for e in market if e["kind"] == "round_end"}
    assert set(ended) == {1, 2}
    for agent in ("agent-1", "agent-2", "agent-3"):
        reflections = of(traces, "round_reflection", agent)
        assert sorted(r["data"]["round"] for r in reflections) == [1, 2]  # exactly one per round
        r1 = next(r for r in reflections if r["data"]["round"] == 1)
        assert "scripted reflection" in r1["data"]["text"] and r1["ts"] > ended[1]
        next_turn = min(e["ts"] for e in of(traces, "llm_turn", agent) if e["data"]["round"] == 2)
        assert r1["ts"] < next_turn  # before the agent's first action of round 2
    assert not [e for e in traces if e["kind"] == "context_reset"]  # one session, as in memory
    delivered = [m for e in traces if e["kind"] == "events_delivered" for m in e["data"]["market_events"]
                 if m.get("event") == "round_end"]
    # each agent's own message, ending with the request from prompts/reflection.md
    assert delivered and all(m["reflection"] and m["body"].endswith("reply with your reflection only.")
                             for m in delivered)
    assert any("You had no task" in m["body"] for m in delivered)  # agent-1's own result

    write_report(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["session"] == "memory"
    assert {a: s["reflections"] for a, s in summary["agents"].items()} == {"agent-1": 2, "agent-2": 2, "agent-3": 2}
    assert summary["reflection"] is True
    assert "Agent session across rounds: `memory`, round-end reflection on (6 round reflections" in \
        (run_dir / "summary.md").read_text()
