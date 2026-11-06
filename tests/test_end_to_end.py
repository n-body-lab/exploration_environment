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
