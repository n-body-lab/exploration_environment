"""How an agent's session carries across rounds: session = fresh | memory | reflection."""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
import yaml
from pydantic import ValidationError

from hivemarket.agent.prompts import context_text, reflection_text, restart_text
from hivemarket.agent.providers.openai_provider import OpenAIProvider
from hivemarket.agent.runtime import AgentRuntime
from hivemarket.agent.tools import tools_for
from hivemarket.common.config import AgentSpec, ExperimentConfig
from hivemarket.common.events import read_jsonl
from hivemarket.orchestrator.local import run_local
from hivemarket.orchestrator.report import write_report
from hivemarket.orchestrator.run import create_run

EXPERIMENTS = Path(__file__).resolve().parents[1] / "experiments"
TEMPLATE = (Path(__file__).resolve().parents[1] / "prompts" / "reflection.md").read_text()


def config(**extra) -> ExperimentConfig:
    return ExperimentConfig.model_validate({"name": "s", "agents": [{"provider": "scripted", "count": 2}], **extra})


# ------------------------------------------------------------------ config


def test_memory_is_the_default_and_fresh_turns_the_memory_tool_off():
    assert config().session == "memory" and config().memory.enabled
    fresh = config(session="fresh")
    assert not fresh.memory.enabled
    assert ExperimentConfig.model_validate(fresh.model_dump()).session == "fresh"  # its own dump validates


@pytest.mark.parametrize("extra, error", [
    ({"session": "fresh", "memory": {"enabled": True}}, "carries nothing across rounds"),
    ({"context": {"reset_each_round": False}}, "replaced by the experiment's `session`"),
    ({"session": "forgetful"}, "fresh"),
])
def test_conflicting_or_replaced_settings_are_rejected(extra, error):
    with pytest.raises(ValidationError, match=error):
        config(**extra)


# ----------------------------------------------------------------- prompts


def agent_cfg(session, memory=True):
    return SimpleNamespace(session=session, memory=SimpleNamespace(enabled=memory),
                           reflection_template=TEMPLATE, spec=SimpleNamespace(prompt_vars={}))


def test_the_prompt_describes_the_session():
    assert "won't remember earlier rounds" in context_text(agent_cfg("fresh", memory=False))
    assert "continues from one round to the next" in context_text(agent_cfg("memory"))
    assert "reflect on how it went" not in context_text(agent_cfg("memory"))
    assert "reflect on how it went" in context_text(agent_cfg("reflection"))
    assert "memory files" not in restart_text("context_limit", {}, [], None)


def test_the_reflection_prompt_carries_the_round_result():
    notice = {"round": 1, "body": "Round 1 of 3 is over: FAIL (agent-3 declared ...). Ideal cost to solve: 18 credits.",
              "objective": [4, 2, 14]}
    status = {"round": 1, "rounds_total": 3, "task": {"needs": ["A1"]}, "task_completed": True, "credits": 87,
              "tokens_held": {"A1": "X"}, "tokens_bought": {"A1": {"from": "agent-1", "cost": 10}}}
    text = reflection_text(agent_cfg("reflection"), notice, status)
    assert "Round 1 of 3 is over" in text and "Ideal cost to solve: 18" in text
    assert "communication 4, transactions 2, payments 14" in text
    assert "Your task: completed" in text and "Credits left: 87" in text and "A1 from agent-1 for 10" in text
    assert "$" not in text
    late = reflection_text(agent_cfg("reflection"), notice, None)  # the next round had already started
    assert "Not available" in late


# --------------------------------------------- a reflection stays in the conversation


class FakeMarket:
    async def status(self, origin="agent"):
        return 200, {"round": 1, "rounds_total": 2, "task": None, "credits": 5, "tokens_held": {}, "tokens_bought": {}}


async def test_the_reflection_appears_in_the_next_rounds_conversation():
    sent = []

    def upstream(request):
        sent.append(json.loads(request.content))
        reply = "I offered too late; next round I will offer first." if len(sent) == 1 else "ok"
        return httpx.Response(200, json={"id": "c", "object": "chat.completion", "created": 0, "model": "m",
                                         "choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", "content": reply}}]})

    provider = OpenAIProvider(AgentSpec(provider="openai", model="m"), "http://upstream/v1", "k")
    provider.client = openai.AsyncOpenAI(base_url="http://upstream/v1", api_key="k", max_retries=0,
                                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    provider.start("sys", tools_for("none"), "go")
    events = []
    rt = AgentRuntime.__new__(AgentRuntime)  # skip the network setup in __init__
    rt.cfg = agent_cfg("reflection")
    rt.provider, rt.market, rt.steps, rt.budget, rt.pending_checkpoint = provider, FakeMarket(), 0, 10**9, None
    rt.telemetry = SimpleNamespace(emit=lambda kind, **d: events.append((kind, d)))

    await rt._end_of_round({"round": 1, "body": "Round 1 of 2 is over: FAIL.", "objective": [1, 0, 0]})
    assert sent[0]["tool_choice"] == "none"  # tools off for the reflection turn
    [(kind, data)] = events
    assert kind == "round_reflection" and data["round"] == 1 and "offered too late" in data["text"]

    provider.add_user_text("Round 2 of 2 has started.")
    await provider.generate()
    assert any("offered too late" in (m.get("content") or "") for m in sent[1]["messages"])


# ------------------------------------------------- each mode in a real (scripted) run


async def run(tmp_path, session, port):
    cfg = yaml.safe_load((EXPERIMENTS / "local_fail.yaml").read_text()) | {"session": session}
    exp = tmp_path / f"{session}.yaml"
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
    assert '"round": 2' in first  # just the current status
    assert "summar" not in first and "memory" not in first.lower() and "notes.md" not in first
    assert not (run_dir / "artifacts" / "agent-2" / "memory" / "notes.md").exists()


async def test_memory_continues_the_same_conversation(tmp_path):
    _, traces = await run(tmp_path, "memory", 20150)
    assert {e["data"]["session"] for e in traces if e["kind"] == "runtime_start"} == {"memory"}
    assert not [e for e in traces if e["kind"] == "context_reset"]  # no reset at the round boundary
    assert {e["data"]["round"] for e in of(traces, "llm_turn", "agent-2")} == {1, 2}
    assert not [e for e in traces if e["kind"] == "round_reflection"]


async def test_reflection_once_per_agent_per_round_before_the_next_round(tmp_path):
    run_dir, traces = await run(tmp_path, "reflection", 20200)
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

    write_report(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["session"] == "reflection"
    assert {a: s["reflections"] for a, s in summary["agents"].items()} == {"agent-1": 2, "agent-2": 2, "agent-3": 2}
    assert "Agent session across rounds: `reflection` (6 round reflections" in (run_dir / "summary.md").read_text()
