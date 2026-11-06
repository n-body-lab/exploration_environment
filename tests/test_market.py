"""Broker: graph pricing, relays, communication cost, rounds, ledger."""

import asyncio
import json

import pytest

from hivemarket.common.config import GraphSpec
from hivemarket.common.events import EventLog, JsonlSink, read_jsonl
from hivemarket.game.graph import ideal_cost
from hivemarket.market.state import Broker, MarketError

GRAPH = {
    "nodes": {
        "a": {"credits": 100, "holds": ["A1", "A2"], "needs": []},
        "b": {"credits": 100, "holds": ["B1"], "needs": ["A1"]},
        "c": {"credits": 60, "holds": [], "needs": ["A1", "B1"]},
    },
    "edges": [{"from": "a", "to": "b", "cost": 10}, {"from": "b", "to": "c", "cost": 4},
              {"from": "b", "to": "a", "cost": 7}],
}


def world(graph=GRAPH, comm=2, rounds=3, round_seconds=60, pause=0, stop_on_pass=True):
    g = GraphSpec.model_validate(graph)
    return {
        "nodes": {n: s.model_dump() for n, s in g.nodes.items()},
        "edges": [{"from": s, "to": d, "cost": c} for (s, d), c in g.edge_costs().items()],
        "communication_cost": comm, "seed": 1,
        "rounds": {"count": rounds, "round_seconds": round_seconds, "pause_seconds": pause, "stop_on_pass": stop_on_pass},
        "ideal": ideal_cost(g, comm),
        "rules": {"allow_broadcast": True, "graph_visibility": "neighbors", "max_submissions": 3},
    }


@pytest.fixture
async def broker(tmp_path):
    b = Broker(world(), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")), run_dir=tmp_path)
    b._logpath = tmp_path / "m.jsonl"
    await b.begin()
    return b


def answer(b, node):
    return "-".join(b.contents[t] for t in b.spec[node]["needs"])


async def test_relay_purchase_and_pass(broker):
    with pytest.raises(MarketError) as e:
        await broker.buy("c", "A1", "a")  # no edge a -> c
    assert e.value.code == "no_edge"
    with pytest.raises(MarketError) as e:
        await broker.buy("c", "A1", "b")  # b doesn't hold A1 yet
    assert e.value.code == "seller_lacks_token"

    r = await broker.buy("b", "A1", "a")
    assert r["cost"] == 10 and broker.credits == {"a": 110, "b": 90, "c": 60}
    await broker.buy("c", "A1", "b")  # relay
    await broker.buy("c", "B1", "b")
    assert broker.exchanges[1]["relay"] is True and broker.exchanges[2]["relay"] is False
    await broker.submit("b", answer(broker, "b"))
    await broker.submit("c", answer(broker, "c"))

    assert broker.phase == "over"  # passed, stop_on_pass
    res = broker.results[0]
    assert res["outcome"] == "pass" and res["credits_spent"] == 18 == res["ideal_credits"]
    assert res["excess_over_ideal"] == 0
    saved = json.loads((broker.run_dir / "rounds" / "round-1.json").read_text())
    assert len(saved["exchanges"]) == 3 and saved["token_contents"]


async def test_communication_cost_burns_per_recipient(broker):
    r = await broker.send_message("c", "*", "hello")
    assert r["cost"] == 4 and broker.credits["c"] == 56
    assert broker.credits["a"] == 100  # burned, not paid
    await broker.send_message("c", "a", "hi")
    res = await broker.end_round("fail", "test")
    assert res["credits_spent_on_communication"] == 6
    assert res["who_spoke_to_whom"] == {"c": {"a": 2, "b": 1}}
    broker.credits["c"] = 1
    broker.phase = "active"
    with pytest.raises(MarketError) as e:
        await broker.send_message("c", "a", "no money")
    assert e.value.code == "insufficient_credits"


async def test_stuck_ends_round_and_next_round_resets(broker):
    await broker.buy("b", "A1", "a")
    old = dict(broker.contents)
    await broker.declare_stuck("c", "out of credits")
    assert broker.phase == "between" and broker.results[0]["outcome"] == "fail"
    with pytest.raises(MarketError) as e:
        await broker.buy("c", "B1", "b")
    assert e.value.code == "round_over"
    notices = [m["body"] for m in broker.inbox("a") if m.get("event") == "round_end"]
    assert "FAIL" in notices[0] and "Credits spent this round: 10" in notices[0] and "Ideal cost to solve: 18" in notices[0]

    await broker.tick()  # pause is 0: next round starts
    assert broker.round_no == 2 and broker.phase == "active"
    assert broker.credits["b"] == 100 and "A1" not in broker.holds["b"]
    assert broker.contents != old  # contents change every round
    kinds = [e["kind"] for e in read_jsonl(broker._logpath)]
    assert kinds.count("round_start") == 2 and "stuck_declared" in kinds


async def test_last_round_ends_game(tmp_path):
    b = Broker(world(rounds=1), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")))
    await b.begin()
    await b.declare_stuck("c", "no")
    assert b.phase == "over"
    assert any(m.get("event") == "game_over" for m in b.inbox("a"))


async def test_timeout_and_unexplained_knowledge(tmp_path):
    b = Broker(world(round_seconds=0.05, rounds=1), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")))
    await b.begin()
    r = await b.submit("b", answer(b, "b"))  # never bought A1
    assert r["correct"]
    await asyncio.sleep(0.1)
    await b.tick()
    assert b.results[0]["reason"] == "time limit reached"
    events = read_jsonl(tmp_path / "m.jsonl")
    assert any(e["kind"] == "unexplained_knowledge" and e["data"]["tokens"] == ["A1"] for e in events)


async def test_graph_views(broker):
    v = broker.graph_view("c")
    assert v["you_can_buy_from"] == [{"node": "b", "cost": 4, "holds": ["B1"]}]
    assert "all_edges" not in v
    broker.visibility = "full"
    assert len(broker.graph_view("c")["all_edges"]) == 3
