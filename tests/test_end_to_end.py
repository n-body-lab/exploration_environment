"""Full local run with scripted agents: services, runtimes, telemetry, report."""

import json
from pathlib import Path

from hivemarket.orchestrator.local import run_local
from hivemarket.orchestrator.report import write_report
from hivemarket.orchestrator.run import create_run

EXPERIMENT = Path(__file__).resolve().parents[1] / "experiments" / "local_smoke.yaml"


async def test_local_smoke_run(tmp_path):
    run_dir = create_run(EXPERIMENT, tmp_path, mode="local", local_base_port=19400)
    results = await run_local(run_dir)
    assert set(results.values()) == {"market_closed"}

    write_report(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text())
    agents = summary["agents"]
    assert agents["agent-1"]["task_completed"] and agents["agent-2"]["task_completed"]
    assert agents["agent-3"]["impossible"] == ["unaffordable"]
    assert not agents["agent-3"]["task_completed"]

    # the rogue agent's direct contact is seen by both sides
    assert agents["agent-3"]["indicator_counts"].get("direct_message_tool", 0) >= 1
    receivers = [a for a, s in agents.items() if s["indicator_counts"].get("direct_inbound")]
    assert receivers
    assert agents["agent-3"]["seconds_from_first_buy_failure_to_violation"] is None or \
        agents["agent-3"]["seconds_from_first_buy_failure_to_violation"] >= 0

    for f in ("market.jsonl", "agent_traces.jsonl"):
        assert (run_dir / "logs" / f).stat().st_size > 0
    assert (run_dir / "final_state.json").exists()
    assert (run_dir / "timeline.jsonl").exists()
