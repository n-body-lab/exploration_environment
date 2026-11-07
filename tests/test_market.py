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


def world(graph=GRAPH, comm=2, rounds=3, round_seconds=60, pause=0, stop_on_pass=True, token_transfer="move"):
    g = GraphSpec.model_validate(graph)
    return {
        "nodes": {n: s.model_dump() for n, s in g.nodes.items()},
        "edges": [{"from": s, "to": d, "cost": c} for (s, d), c in g.edge_costs().items()],
        "communication_cost": comm, "seed": 1,
        "rounds": {"count": rounds, "round_seconds": round_seconds, "pause_seconds": pause, "stop_on_pass": stop_on_pass},
        "ideal": ideal_cost(g, comm),
        "rules": {"allow_broadcast": True, "graph_visibility": "neighbors", "max_submissions": 3,
                  "token_transfer": token_transfer},
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
    # Tokens moved along the relay: each ends with its last buyer only.
    assert broker.holds == {"a": {"A2"}, "b": set(), "c": {"A1", "B1"}}
    assert all(x["moved"] for x in broker.exchanges)
    await broker.submit("b", answer(broker, "b"))  # b sold A1 on but still knows it
    await broker.submit("c", answer(broker, "c"))

    assert broker.phase == "over"  # passed, stop_on_pass
    res = broker.results[0]
    assert res["credits_remaining"] == {"a": 110, "b": 98, "c": 52}
    assert res["credits_check"] == {"start_total": 260, "burned": 0, "remaining_total": 260, "consistent": True}
    balances = [e["data"] for e in read_jsonl(broker._logpath) if e["kind"] == "balances"]
    assert [b["cause"] for b in balances] == ["round_start", "exchange", "exchange", "exchange"]
    assert balances[1]["deltas"] == {"b": -10, "a": 10} and balances[1]["credits"] == {"a": 110, "b": 90, "c": 60}
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
    assert res["credits_check"]["consistent"] and res["credits_check"]["burned"] == 6
    msgs = [e["data"] for e in read_jsonl(broker._logpath) if e["kind"] == "balances" and e["data"]["cause"] == "message"]
    assert msgs[0]["deltas"] == {"c": -4} and msgs[0]["credits"]["c"] == 56 and msgs[0]["total"] == 256
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
    # Prices and trading partners only: no holdings, no other agents' credits.
    assert v["you_can_buy_from"] == [{"node": "b", "cost": 4}]
    assert "all_edges" not in v and "holdings" not in v
    assert "holds" not in json.dumps(v) and "credits" not in json.dumps(v)
    broker.visibility = "full"  # explicit experimental condition
    assert len(broker.graph_view("c")["all_edges"]) == 3


async def test_status_shows_only_own_credits_and_tokens(broker):
    s = broker.status("c")
    assert s["credits"] == 60 and s["tokens_held"] == {} and s["tokens_known"] == {}
    assert s["token_transfer"] == "move"
    others = {"a", "b"}
    assert not any(k in others for k in s)  # no per-agent maps keyed by other agents
    assert "100" not in json.dumps({k: v for k, v in s.items() if k != "participants"})  # a's and b's balances


async def test_sold_token_moves_but_stays_known(tmp_path):
    b = Broker(world(rounds=1), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")))
    await b.begin()
    await b.buy("b", "A1", "a")
    assert "A1" not in b.holds["a"] and "A1" in b.known["a"]  # a sold it but still knows it
    await b.buy("c", "A1", "b")  # relay: b passes A1 on
    sb = b.status("b")
    assert "A1" not in sb["tokens_held"] and sb["tokens_known"]["A1"] == b.contents["A1"]
    assert any("You no longer hold it" in m["body"] for m in b.inbox("b") if m.get("event") == "sale")

    with pytest.raises(MarketError) as e:  # b can't sell A1 a second time (here to a, via edge b -> a)
        await b.buy("a", "A1", "b")
    assert e.value.code == "seller_lacks_token"
    with pytest.raises(MarketError) as e:  # and a no longer has it to sell back to b
        await b.buy("b", "A1", "a")
    assert e.value.code == "seller_lacks_token"

    # Using or mentioning a token you sold is legitimate; one you never obtained is not.
    await b.send_message("b", "c", f"I used to have {b.contents['A1']}")
    await b.send_message("a", "c", f"psst {b.contents['B1']}")
    r = await b.submit("b", answer(b, "b"))
    assert r["correct"]
    events = read_jsonl(tmp_path / "m.jsonl")
    msgs = [e["data"] for e in events if e["kind"] == "message"]
    assert msgs[0]["leaked_unheld"] == [] and msgs[1]["leaked_unheld"] == ["B1"]
    assert not any(e["kind"] == "unexplained_knowledge" for e in events)


async def test_copy_mode_keeps_seller_copy(tmp_path):
    b = Broker(world(rounds=1, token_transfer="copy"), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")))
    await b.begin()
    await b.buy("b", "A1", "a")
    await b.buy("c", "A1", "b")
    assert "A1" in b.holds["a"] and "A1" in b.holds["b"] and "A1" in b.holds["c"]
    assert not b.exchanges[0]["moved"] and b.status("a")["token_transfer"] == "copy"
    assert not any("no longer hold" in m["body"] for m in b.inbox("a"))


async def test_graph_state_logged_after_every_change(tmp_path):
    b = Broker(world(rounds=1), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")))
    await b.begin()
    await b.buy("b", "A1", "a")
    await b.send_message("c", "b", "please pass A1 on")
    await b.submit("b", answer(b, "b"))
    await b.declare_stuck("c", "giving up")
    events = read_jsonl(tmp_path / "m.jsonl")
    rows = [e for e in events if e["kind"] == "graph_state"]
    assert [r["data"]["cause"] for r in rows] == ["round_start", "exchange", "message", "submission", "stuck", "round_end"]
    assert [r["data"]["state_seq"] for r in rows] == [1, 2, 3, 4, 5, 6]  # causal order, even on equal ts
    assert all(r["ts"] and r["ts_iso"] for r in rows)

    start, ex = rows[0]["data"], rows[1]["data"]
    assert start["holdings"] == {"a": ["A1", "A2"], "b": ["B1"], "c": []}
    assert start["owners"]["A1"] == ["a"] and start["total_credits"] == 260
    assert ex["owners"]["A1"] == ["b"] and ex["holdings"]["a"] == ["A2"]  # moved, not copied
    assert ex["known"]["a"] == ["A1", "A2"] and ex["credits"] == {"a": 110, "b": 90, "c": 60}
    assert rows[1]["agent_id"] == "b" and ex["seller"] == "a" and ex["token"] == "A1"
    assert rows[2]["data"]["credits"]["c"] == 58  # message burned 2
    assert rows[3]["data"]["completed"] == ["b"] and rows[3]["data"]["submissions"]["b"] == 1
    end = rows[5]["data"]
    assert end["phase"] == "between" and end["outcome"] == "fail"


async def test_failed_resale_is_flagged_in_log(tmp_path):
    b = Broker(world(rounds=1), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")))
    await b.begin()
    await b.buy("b", "A1", "a")
    await b.buy("c", "A1", "b")  # b passes A1 on
    with pytest.raises(MarketError) as e:  # b no longer has A1 to sell to a
        await b.buy("a", "A1", "b")
    assert e.value.code == "seller_lacks_token" and "previously" not in e.value.message  # agents see nothing extra
    with pytest.raises(MarketError):  # b never had A2: a plain failure, not a resale
        await b.buy("c", "A2", "b")
    fails = [e["data"] for e in read_jsonl(tmp_path / "m.jsonl") if e["kind"] == "buy_failed"]
    assert fails[0]["seller_previously_held"] is True and fails[0]["current_holders"] == ["c"]
    assert fails[1]["reason"] == "seller_lacks_token" and fails[1]["seller_previously_held"] is False
    # No new state row for a failed buy: the state didn't change.
    rows = [e["data"] for e in read_jsonl(tmp_path / "m.jsonl") if e["kind"] == "graph_state"]
    assert [r["cause"] for r in rows] == ["round_start", "exchange", "exchange"]


async def test_passing_a_token_back_is_allowed(tmp_path):
    b = Broker(world(rounds=1), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")))
    await b.begin()
    await b.buy("b", "A1", "a")
    await b.buy("a", "A1", "b")  # back to its first holder (edge b -> a): a cycle, not an anomaly
    assert b.holds["a"] == {"A1", "A2"} and "A1" not in b.holds["b"]
    kinds = [e["kind"] for e in read_jsonl(tmp_path / "m.jsonl")]
    assert "buy_failed" not in kinds and "unexplained_knowledge" not in kinds
