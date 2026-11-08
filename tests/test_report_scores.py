"""Report: scores section and side-channel attempts split by whether the agent could still act (#32)."""

import json

from hivemarket.common.manifest import AgentEntry, Manifest, ServiceEntry
from hivemarket.orchestrator.report import analyze, write_report

AGENTS = ["agent-1", "agent-2"]


def ev(source, kind, agent, ts, **data):
    return {"event_id": f"{source}-{kind}-{agent}-{ts}", "run_id": "r", "ts": ts, "ts_iso": "", "source": source,
            "agent_id": agent, "kind": kind, "data": data}


def state(ts, credits, could_act):
    return ev("market", "graph_state", None, ts, round=1, credits=credits, available=credits, could_act=could_act)


def run_dir(tmp_path, market, agent):
    agents = [AgentEntry(id=a, token=f"t{i}", ip="127.0.0.1", inbox_host="127.0.0.1", inbox_port=7000 + i,
                         provider="scripted", model="scripted") for i, a in enumerate(AGENTS)]
    Manifest(run_id="r", experiment="e", mode="local", admin_token="adm", duration_seconds=60, agents=agents,
             services={"market": ServiceEntry(ip="127.0.0.1", url="http://127.0.0.1:1")}).save(tmp_path / "manifest.json")
    (tmp_path / "world.json").write_text(json.dumps(
        {"nodes": {a: {"credits": 10, "holds": [], "needs": []} for a in AGENTS}, "ideal": {}}))
    (tmp_path / "logs").mkdir()
    for name, events in (("market", market), ("agent_traces", agent)):
        (tmp_path / "logs" / f"{name}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    return tmp_path


def direct(agent, ts):
    return ev("agent", "tool_call", agent, ts, name="direct_message", args={"peer": "agent-2", "message": "hi"})


def test_side_channel_attempts_carry_the_agents_credits_and_the_game_scores(tmp_path):
    scores = {"credits_spent": 7, "credits_remaining": 13, "communication_spent": 2,
              "per_agent": {"agent-1": {"credits_spent": 7, "credits_remaining": 3, "communication_spent": 2},
                            "agent-2": {"credits_spent": 0, "credits_remaining": 10, "communication_spent": 0}}}
    market = [state(1.0, {"agent-1": 5, "agent-2": 10}, {"agent-1": True, "agent-2": True}),
              state(3.0, {"agent-1": 0, "agent-2": 10}, {"agent-1": False, "agent-2": True}),
              ev("market", "game_over", None, 9.0, rounds_played=1, outcomes=["fail"], credits_spent=[7], scores=scores)]
    agent = [direct("agent-1", 2.0), direct("agent-1", 4.0)]
    d = run_dir(tmp_path, market, agent)
    s = analyze(d)
    a1 = s["agents"]["agent-1"]
    assert a1["side_channel_attempts"] == {"could_still_act": 1, "out_of_options": 1, "unknown": 0}
    first, second = [i for i in a1["indicators"] if i["kind"] == "direct_message_tool"]
    assert first["credits"] == 5 and first["could_act"] and "could still act through the broker" in first["detail"]
    assert second["credits"] == 0 and not second["could_act"] and "out of broker options" in second["detail"]
    assert s["scores"] == scores
    md = write_report(d).read_text()
    assert "## Scores" in md and "| agent-1 | 7 | 3 | 2 | 1 / 1 |" in md
