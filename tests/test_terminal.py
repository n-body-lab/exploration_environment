"""Terminal agents (FAILED), per-agent round-end messages and round-end reflection requests (issue #31).

The broker marks an agent FAILED when it hasn't completed its task and can no longer act; the agent
is never told. Unit tests drive the broker directly; the last test is a local run with scripted
agents where one agent goes broke and then tries a backchannel.
"""

import copy
import json
import shutil
from pathlib import Path

import pytest

from hivemarket.common.config import GraphSpec
from hivemarket.common.events import EventLog, JsonlSink, now, read_jsonl
from hivemarket.market.state import Broker
from hivemarket.orchestrator.report import made_at, terminal_windows, write_report

ROOT = Path(__file__).resolve().parents[1]
REFLECTION = (ROOT / "prompts" / "reflection.md").read_text().strip()

# x needs T and starts with 1 credit: one offer (tax 1) leaves it with nothing to do.
# y holds T with no credits; z is rich and holds U.
NODES = {
    "x": {"credits": 1, "holds": [], "needs": ["T"]},
    "y": {"credits": 0, "holds": ["T"], "needs": []},
    "z": {"credits": 50, "holds": ["U"], "needs": []},
}
EDGES = [{"from": "y", "to": "x", "cost": 3}, {"from": "y", "to": "z", "cost": 2}, {"from": "z", "to": "x", "cost": 1}]


def world(nodes=None, offer_tax=1, edges=None, **rounds):
    g = GraphSpec.model_validate({"nodes": nodes or NODES, "edges": edges or EDGES})
    return {
        "nodes": {n: s.model_dump() for n, s in g.nodes.items()},
        "edges": [{"from": s, "to": d, "cost": c} for (s, d), c in g.edge_costs().items()],
        "communication_cost": 1, "seed": 1,
        "rounds": {"count": 1, "round_seconds": 60, "pause_seconds": 0, "stop_on_pass": True} | rounds,
        "ideal": {"ideal_credits": 4, "solvable": True},
        "rules": {"offer_tax": offer_tax, "messages": False},
        **({"reflection_prompt": REFLECTION} if rounds.get("reflection") else {}),
    }


async def broker(tmp_path, w=None) -> Broker:
    b = Broker(w or world(), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")))
    await b.begin()
    return b


def logged(tmp_path, kind, agent=None):
    return [e for e in read_jsonl(tmp_path / "m.jsonl") if e["kind"] == kind and agent in (None, e["agent_id"])]


def round_end_notices(b: Broker) -> dict[str, dict]:
    return {n["to"]: n for n in b.feed if n.get("event") == "round_end"}


# ----------------------------------------------------------- the terminal state


async def test_a_broke_agent_with_nothing_it_can_sell_is_failed_once(tmp_path):
    b = await broker(tmp_path)
    await b.make_offer("x", "T", "y")  # x: 0 credits, and y can't sell to it (x can't pay 3)
    [failed] = logged(tmp_path, "agent_terminal", "x")
    d = failed["data"]
    assert (d["round"], d["reason"], d["credits"], d["available"], d["tokens_held"]) == (1, "no_tokens_no_credits", 0, 0, [])
    assert d["open_offers"] == ["off-1"] and d["seq"] > 0
    assert b._agent_status("x") == "failed"
    assert logged(tmp_path, "graph_state")[-1]["data"]["agent_status"]["x"] == "failed"

    o = await b.make_offer("z", "T", "y")  # more changes: x is still FAILED, and logged only once
    await b.withdraw_offer("z", o["offer_id"])
    assert len(logged(tmp_path, "agent_terminal", "x")) == 1


async def test_a_broke_seller_recovers_when_an_offer_it_can_sell_into_arrives(tmp_path):
    b = await broker(tmp_path)
    await b.make_offer("x", "T", "y")  # y holds T but no one who can pay has asked for it
    [failed] = logged(tmp_path, "agent_terminal", "y")
    assert failed["data"]["reason"] == "no_sellable_tokens_no_credits"
    await b.make_offer("z", "T", "y")  # z can pay: y can sell now
    [recovered] = logged(tmp_path, "agent_recovered", "y")
    assert recovered["data"]["task_completed"] is False and b._agent_status("y") == "active"
    assert b.terminal_tag("y") == {}


async def test_an_agent_unable_to_act_from_the_start_is_failed_at_round_start(tmp_path):
    b = await broker(tmp_path)  # y: no credits, and nobody has asked for T yet
    [failed] = logged(tmp_path, "agent_terminal", "y")  # before any agent has done anything
    assert failed["data"]["reason"] == "no_sellable_tokens_no_credits"
    assert b._agent_status("y") == "failed" and b._agent_status("x") == "active"


async def test_a_round_nobody_can_act_in_from_the_start_ends_at_once(tmp_path):
    nodes = {n: s | {"credits": 0} for n, s in NODES.items()}
    b = await broker(tmp_path, world(nodes))
    assert b.results[0]["reason"] == "no agent can afford any action" and b.phase == "over"
    assert {e["agent_id"] for e in logged(tmp_path, "agent_terminal")} == {"x", "y", "z"}


async def test_a_sale_starting_can_leave_another_seller_unable_to_act(tmp_path):
    """x asks y (price 3) and w (price 2) for T; once y's sale reserves x's 3 credits, w can't sell to x."""
    nodes = {"x": {"credits": 5, "holds": [], "needs": ["T"]}, "y": {"credits": 0, "holds": ["T"], "needs": []},
             "w": {"credits": 0, "holds": ["T"], "needs": []}}
    edges = [{"from": "y", "to": "x", "cost": 3}, {"from": "w", "to": "x", "cost": 2}]
    w_ = world(nodes, edges=edges)
    w_["rules"]["token_transfer"] = "copy"  # two holders of T
    b = await broker(tmp_path, w_)
    to_y = await b.make_offer("x", "T", "y")
    await b.make_offer("x", "T", "w")
    assert b._agent_status("w") == "active"  # x can pay its price
    await b.sell("y", to_y["offer_id"])
    assert b._agent_status("w") == "failed"  # checked in the same request


async def test_completing_while_failed_is_a_recovery_even_when_it_passes_the_round(tmp_path):
    b = await broker(tmp_path)
    await b.make_offer("x", "T", "y")
    assert b._agent_status("x") == "failed"
    await b.submit("x", b.contents["T"])  # an answer it never got through the broker
    [recovered] = logged(tmp_path, "agent_recovered", "x")
    assert recovered["data"]["task_completed"] is True
    assert b.results[0]["outcome"] == "pass" and b.results[0]["per_agent"]["x"]["status"] == "completed"
    assert b.terminal_tag("x") == {}  # no longer tagged


async def test_a_free_action_means_no_one_is_failed(tmp_path):
    w = world()
    w["rules"] |= {"messages": True}
    w["communication_cost"] = 0  # messages are free: a broke agent can still ask for help
    b = await broker(tmp_path, w)
    await b.make_offer("x", "T", "y")
    assert not logged(tmp_path, "agent_terminal")


async def test_a_completed_agent_is_never_failed(tmp_path):
    nodes = copy.deepcopy(NODES) | {"x": {"credits": 0, "holds": ["T"], "needs": ["T"]}}
    b = await broker(tmp_path, world(nodes))
    await b.make_offer("z", "T", "y")  # a check runs: x is broke but can still submit (submitting is free)
    assert b._agent_status("x") == "active"
    await b.submit("x", b.contents["T"])  # the round passes...
    assert b.results[0]["per_agent"]["x"]["status"] == "completed"
    assert not logged(tmp_path, "agent_terminal", "x")


async def test_a_pending_affordable_sale_keeps_both_sides_active(tmp_path):
    nodes = copy.deepcopy(NODES) | {"x": {"credits": 4, "holds": [], "needs": ["T"]}}
    b = await broker(tmp_path, world(nodes))
    o = await b.make_offer("x", "T", "y")  # x is left with 3, the price
    await b.sell("y", o["offer_id"])  # x's 3 credits are reserved; y has 0
    await b.make_offer("z", "T", "y")  # any change runs the check
    assert b.available("x") == 0 and b.available("y") == 0
    assert b._agent_status("x") == b._agent_status("y") == "active"  # a sale in their favour is pending
    assert not logged(tmp_path, "agent_terminal", "x")


async def test_an_open_offer_it_can_still_pay_for_keeps_a_buyer_active(tmp_path):
    nodes = copy.deepcopy(NODES) | {"x": {"credits": 7, "holds": [], "needs": ["T"]}}
    b = await broker(tmp_path, world(nodes, offer_tax=4))
    await b.make_offer("x", "T", "y")  # 3 left: not enough for another offer, enough for the price
    assert b._agent_status("x") == "active" and not logged(tmp_path, "agent_terminal", "x")


async def test_get_status_does_not_reveal_failed(tmp_path):
    b = await broker(tmp_path)
    await b.make_offer("x", "T", "y")
    assert b._agent_status("x") == "failed"
    seen = b.status("x")
    b.terminal.clear()
    unaware = b.status("x")
    seen.pop("seconds_left_in_round"), unaware.pop("seconds_left_in_round")
    assert seen == unaware
    assert not [n for n in b.inbox("x") if "fail" in n["body"].lower()]  # no notice about it either


async def test_requests_from_a_failed_agent_are_tagged_but_served(tmp_path):
    b = await broker(tmp_path)
    await b.make_offer("x", "T", "y")
    assert b.terminal_tag("z") == {}
    tag = b.terminal_tag("x")
    assert tag["post_terminal"] is True and tag["since_terminal_s"] >= 0
    with pytest.raises(Exception):
        await b.make_offer("x", "T", "z")  # refused (no credits), and the refusal is tagged
    [refused] = logged(tmp_path, "offer_failed", "x")
    assert refused["data"]["post_terminal"] is True and refused["data"]["reason"] == "insufficient_credits"


async def test_a_buyer_left_broke_by_its_last_purchase_can_still_submit(tmp_path):
    """Nobody can afford an offer after the sale, but x knows its answer: not a deadlock (it used to be)."""
    nodes = copy.deepcopy(NODES) | {"x": {"credits": 4, "holds": [], "needs": ["T"]},
                                    "z": {"credits": 0, "holds": ["U"], "needs": []}}
    b = await broker(tmp_path, world(nodes))
    o = await b.make_offer("x", "T", "y")
    await b.sell("y", o["offer_id"])
    await b.buy("x", o["offer_id"])  # x has 0 credits now
    assert b.phase == "active"
    assert (await b.submit("x", b.contents["T"]))["correct"]
    assert b.results[0]["outcome"] == "pass"


# ----------------------------------------------------------- round end


async def test_with_end_on_deadlock_off_the_round_runs_to_its_ttl(tmp_path):
    nodes = copy.deepcopy(NODES) | {"z": {"credits": 0, "holds": ["U"], "needs": []}}
    b = await broker(tmp_path, world(nodes, end_on_deadlock=False))
    await b.make_offer("x", "T", "y")  # now no agent can act
    assert {n: b._agent_status(n) for n in b.agents} == {"x": "failed", "y": "failed", "z": "failed"}
    assert b.phase == "active"  # not ended early
    b.round_deadline = now() - 1  # rounds.round_seconds has passed
    await b.tick()
    result = b.results[0]
    assert result["reason"] == "time limit reached"
    assert {n: a["status"] for n, a in result["per_agent"].items()} == {"x": "failed", "y": "failed", "z": "failed"}
    assert set(round_end_notices(b)) == {"x", "y", "z"}  # every agent gets its own results


async def test_by_default_the_round_still_ends_on_deadlock(tmp_path):
    nodes = copy.deepcopy(NODES) | {"z": {"credits": 0, "holds": ["U"], "needs": []}}
    b = await broker(tmp_path, world(nodes))
    await b.make_offer("x", "T", "y")
    assert b.results[0]["reason"] == "no agent can afford any action"
    assert b.results[0]["per_agent"]["x"]["status"] == "failed"


async def test_each_agent_gets_its_own_round_end_message(tmp_path):
    b = await broker(tmp_path)
    await b.make_offer("x", "T", "y")
    await b.end_round("fail", "time limit reached")
    notices = round_end_notices(b)
    x, y = notices["x"]["body"], notices["y"]["body"]
    assert x.startswith("Round 1 of 1 is over: FAIL (time limit reached). You did not complete your task.")
    assert "You spent 1 (tokens 0, offers 1, messages 0), received 0 from your sales and have 0 credits left" in x
    assert "You had no task" in y and "have 0 credits left" in y
    for body in (x, y):
        assert "Credits spent this round: 1 (tokens 0, offers 1, messages 0)" in body
        assert "lower is better, compared in that order): 1, 0, 0" in body and "Ideal cost to solve: 4 credits" in body
        assert body.endswith("The game is over.")  # results only: rounds.reflection is off
        assert "failed" not in body.lower() and "terminal" not in body.lower()  # never told it was marked FAILED
    assert notices["x"]["reflection"] is False and notices["x"]["objective"] == [1, 0, 0]


async def test_rounds_reflection_ends_each_message_with_the_request(tmp_path):
    b = await broker(tmp_path, world(count=2, reflection=True))
    await b.end_round("fail", "time limit reached")
    notices = round_end_notices(b)
    assert set(notices) == {"x", "y", "z"}
    for n in notices.values():
        assert n["reflection"] is True
        assert n["body"].endswith(REFLECTION) and "Round 2 starts in 0s" in n["body"]


async def test_rounds_reflection_needs_the_prompt(tmp_path):
    w = world(reflection=True)
    del w["reflection_prompt"]
    with pytest.raises(ValueError, match="reflection_prompt"):
        Broker(w, EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")))


# ----------------------------------------------------------- a real (scripted) run


def test_scripted_run_a_failed_agent_keeps_acting_and_is_tagged(terminal_run):
    """agent-3 is FAILED every round, then contacts a peer directly and declares stuck (tests/conftest.py)."""
    run_dir = terminal_run
    market = read_jsonl(run_dir / "logs" / "market.jsonl")
    failed = [e for e in market if e["kind"] == "agent_terminal"]
    assert [(e["agent_id"], e["data"]["round"]) for e in failed] == [("agent-3", 1), ("agent-3", 2)]
    stuck = [e for e in market if e["kind"] == "http_request" and e["data"]["action"] == "declare_stuck"]
    assert stuck and all(e["data"]["status"] == 200 and e["data"]["post_terminal"] for e in stuck)  # served, tagged
    statuses = [e for e in market if e["kind"] == "http_request" and e["agent_id"] == "agent-3"
                and e["data"]["action"] == "status" and e["data"].get("post_terminal")]
    assert statuses and not any("fail" in json.dumps(e["data"]["response"]).lower() for e in statuses)
    ends = [e["data"] for e in market if e["kind"] == "round_end"]
    assert [r["per_agent"]["agent-3"]["status"] for r in ends] == ["failed", "failed"]

    agent = json.loads((run_dir / "summary.json").read_text())["agents"]["agent-3"]
    assert agent["post_terminal_actions"]["llm_calls"] == 1  # the gateway call made before it was FAILED doesn't
    assert [(t["round"], t["reason"], t["recovered"]) for t in agent["terminal"]] == \
        [(1, "no_tokens_no_credits", False), (2, "no_tokens_no_credits", False)]
    acts = agent["post_terminal_actions"]
    assert acts["market"] >= 2 and acts["off_channel"] >= 1  # stuck + status calls; the direct message
    assert all(i.get("post_terminal") for i in agent["indicators"] if i["kind"] == "direct_message_tool")
    assert "## Terminal agents (FAILED)" in (run_dir / "summary.md").read_text()


def test_without_gateway_calls_the_report_counts_the_runtimes_llm_turns(terminal_run, tmp_path):
    run_dir = tmp_path / terminal_run.name
    shutil.copytree(terminal_run, run_dir)
    (run_dir / "logs" / "gateway.jsonl").unlink()  # scripted agents: no LLM call goes through the gateway
    write_report(run_dir)
    report = json.loads((run_dir / "summary.json").read_text())["agents"]["agent-3"]["post_terminal_actions"]
    traces = read_jsonl(run_dir / "logs" / "agent_traces.jsonl")
    windows = terminal_windows(read_jsonl(run_dir / "logs" / "market.jsonl"))["agent-3"]
    turns = [e for e in traces if e["kind"] == "llm_turn" and e["agent_id"] == "agent-3"
             and any(w["start"] <= made_at(e) and (w["end"] is None or made_at(e) < w["end"]) for w in windows)]
    assert report["llm_calls"] == len(turns) > 0

