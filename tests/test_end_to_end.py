"""Full local runs with scripted agents: broker rounds, runtimes, memory, telemetry, report."""

import json
from pathlib import Path

from hivemarket.common.events import read_jsonl
from hivemarket.orchestrator.local import run_local
from hivemarket.orchestrator.report import write_report
from hivemarket.orchestrator.run import create_run

EXPERIMENTS = Path(__file__).resolve().parents[1] / "experiments"


async def test_relay_game_passes_in_one_round(tmp_path):
    run_dir = create_run(EXPERIMENTS / "local_smoke.yaml", tmp_path, mode="local", local_base_port=19400)
    results = await run_local(run_dir)
    assert set(results.values()) == {"market_closed"}

    write_report(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text())
    assert [r["outcome"] for r in summary["rounds"]] == ["pass"]  # stop_on_pass
    r = summary["rounds"][0]
    assert r["credits_spent"] == r["ideal_credits"] == 18
    assert summary["agents"]["agent-2"]["relayed"] == 1  # agent-3 got A1 through agent-2
    assert (run_dir / "rounds" / "round-1.json").exists() and (run_dir / "final_state.json").exists()


async def test_failing_rounds_persist_session_and_memory(tmp_path):
    run_dir = create_run(EXPERIMENTS / "local_fail.yaml", tmp_path, mode="local", local_base_port=19450)
    await run_local(run_dir)
    write_report(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text())
    assert [r["outcome"] for r in summary["rounds"]] == ["fail", "fail"]
    assert all("agent-3 declared" in r["reason"] for r in summary["rounds"])
    agents = summary["agents"]
    assert agents["agent-3"]["stuck_declarations"] == 2

    # one session across both rounds: a single runtime_start per agent, turns in both rounds
    traces = read_jsonl(run_dir / "logs" / "agent_traces.jsonl")
    assert sum(e["kind"] == "runtime_start" and e["agent_id"] == "agent-2" for e in traces) == 1
    assert {e["data"]["round"] for e in traces if e["kind"] == "llm_turn" and e["agent_id"] == "agent-2"} == {1, 2}
    # private memory persisted across rounds
    notes = (run_dir / "artifacts" / "agent-2" / "memory" / "notes.md").read_text()
    assert "round 1" in notes and "round 2" in notes
    # the rogue's direct contact is seen by both sides
    assert agents["agent-3"]["indicator_counts"].get("direct_message_tool", 0) >= 1
    assert agents["agent-1"]["indicator_counts"].get("direct_inbound", 0) >= 1
    # no false market-probing indicators: the orchestrator's admin close is logged but belongs to no agent
    market = read_jsonl(run_dir / "logs" / "market.jsonl")
    assert any(e["kind"] == "admin_request" and e["agent_id"] is None for e in market)
    assert not any({"admin_probe", "admin_access", "endpoint_guess"} & set(a["indicator_counts"]) for a in agents.values())


async def test_context_policy_checkpoints_and_restarts(tmp_path):
    import yaml

    cfg = yaml.safe_load((EXPERIMENTS / "local_fail.yaml").read_text())
    cfg["agents"] = [{**g, "max_tokens": 1000} for g in cfg["agents"]]
    # scripted agents report 500 tokens per turn: a 3000-token budget forces mid-round restarts too
    cfg["context"] = {"window_tokens": 6000, "reset_at_fraction": 0.5}
    exp = tmp_path / "ctx.yaml"
    exp.write_text(yaml.safe_dump(cfg))
    run_dir = create_run(exp, tmp_path / "runs", mode="local", local_base_port=19500)
    await run_local(run_dir)

    traces = read_jsonl(run_dir / "logs" / "agent_traces.jsonl")
    resets = [e for e in traces if e["kind"] == "context_reset"]
    reasons = {e["data"]["reason"] for e in resets}
    assert {"new_round", "context_limit"} <= reasons
    checkpoints = [e for e in traces if e["kind"] == "context_checkpoint" and e["agent_id"] == "agent-2"]
    assert any(c["data"]["reason"] == "round_end" and "scripted summary" in c["data"]["summary"] for c in checkpoints)
    restart = next(e for e in resets if e["agent_id"] == "agent-2" and e["data"]["reason"] == "new_round")
    first = restart["data"]["first_message"]
    assert "scripted summary" in first and "notes.md" in first and '"round": 2' in first
    # the game still plays out normally
    summary = json.loads((write_report(run_dir) and run_dir / "summary.json").read_text())
    assert [r["outcome"] for r in summary["rounds"]] == ["fail", "fail"]
    # runtime-made status calls are distinguishable from the agent's own actions in the broker log
    market = read_jsonl(run_dir / "logs" / "market.jsonl")
    assert any(e["kind"] == "http_request" and e["data"].get("origin") == "runtime" for e in market)


def test_context_overflow_detection():
    from hivemarket.agent.runtime import _is_context_overflow

    assert _is_context_overflow(Exception("This model's maximum context length is 32768 tokens"))
    assert _is_context_overflow(Exception("prompt is too long: 210000 tokens > 200000 maximum"))
    assert not _is_context_overflow(Exception("rate limited"))
