"""Execution fees: burned on every committed purchase, by buyer and seller, and affordable before the trade."""

import pytest

from hivemarket.common.config import GraphSpec
from hivemarket.common.events import EventLog, JsonlSink, read_jsonl
from hivemarket.market.state import Broker, MarketError

GRAPH = {
    "nodes": {
        "s": {"credits": 1, "holds": ["A"], "needs": []},
        "b": {"credits": 10, "holds": [], "needs": ["A"]},
        "poor": {"credits": 4, "holds": [], "needs": []},
        "broke": {"credits": 0, "holds": ["Z"], "needs": []},
    },
    "edges": [{"from": "s", "to": "b", "cost": 4}, {"from": "s", "to": "poor", "cost": 4},
              {"from": "broke", "to": "b", "cost": 2}],
}


def world(buyer_fee=1, seller_fee=1):
    g = GraphSpec.model_validate(GRAPH)
    return {
        "nodes": {n: s.model_dump() for n, s in g.nodes.items()},
        "edges": [{"from": s, "to": d, "cost": c} for (s, d), c in g.edge_costs().items()],
        "communication_cost": 1, "seed": 1,
        "rounds": {"count": 1, "round_seconds": 60, "pause_seconds": 0, "stop_on_pass": True},
        "ideal": {"ideal_credits": 6, "solvable": True},
        "rules": {"execution_fee": {"buyer": buyer_fee, "seller": seller_fee}},
    }


@pytest.fixture
async def broker(tmp_path):
    b = Broker(world(), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")))
    await b.begin()
    return b


async def trade(b: Broker, buyer: str, token: str, seller: str) -> dict:
    o = await b.place_order(buyer, token, seller)
    await b.respond_to_order(seller, o["order_id"], True)
    return await b.commit_order(buyer, o["order_id"])


async def test_fees_are_charged_burned_and_accounted(broker, tmp_path):
    assert broker.status("b")["execution_fee"] == {"buyer": 1, "seller": 1}
    r = await trade(broker, "b", "A", "s")
    assert r["cost"] == 4 and r["execution_fee"] == 1
    # buyer pays price + fee; seller gets the price minus its fee; the two fees are burned
    assert broker.credits["b"] == 10 - 4 - 1 and broker.credits["s"] == 1 + 4 - 1
    events = read_jsonl(tmp_path / "m.jsonl")
    deltas = [e["data"]["deltas"] for e in events if e["kind"] == "balances" and e["data"]["cause"] == "exchange"]
    assert deltas == [{"b": -5, "s": 3}]
    exchange = next(e["data"] for e in events if e["kind"] == "exchange")
    assert (exchange["cost"], exchange["buyer_fee"], exchange["seller_fee"]) == (4, 1, 1)

    await broker.submit("b", broker.contents["A"])
    res = broker.results[0]
    assert res["credits_spent"] == 6 and res["credits_spent_on_fees"] == 2 and res["credits_spent_on_exchanges"] == 4
    assert res["credits_check"] == {"start_total": 15, "burned": 2, "remaining_total": 13, "consistent": True}
    assert res["per_agent"]["b"]["spent_on_fees"] == 1 and res["per_agent"]["s"]["spent_on_fees"] == 1
    assert "execution fees 2" in next(n["body"] for n in broker.feed if n.get("event") == "round_end")


async def test_buyer_must_afford_price_plus_fee(broker):
    with pytest.raises(MarketError) as e:
        await broker.place_order("poor", "A", "s")  # 4 credits: the price, but not price + fee
    assert e.value.code == "insufficient_credits" and "price 4 + execution fee 1" in e.value.message


async def test_seller_must_afford_its_fee_before_the_sale(broker):
    o = await broker.place_order("b", "Z", "broke")
    with pytest.raises(MarketError) as e:
        await broker.respond_to_order("broke", o["order_id"], True)  # 0 credits: can't pay its fee out of the sale
    assert e.value.code == "insufficient_credits"
    assert broker.orders[o["order_id"]]["status"] == "pending"


async def test_seller_fee_is_set_aside_until_the_sale_completes(broker):
    o = await broker.place_order("b", "A", "s")
    await broker.respond_to_order("s", o["order_id"], True)
    assert broker.available("s") == 0  # its 1 credit is held for the fee
    with pytest.raises(MarketError) as e:
        await broker.send_message("s", "b", "hi")
    assert e.value.code == "insufficient_credits"
    await broker.cancel_order("b", o["order_id"])
    assert broker.available("s") == 1  # released with the reservation


async def test_without_fees_a_zero_credit_agent_can_still_sell(tmp_path):
    b = Broker(world(buyer_fee=0, seller_fee=0), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")))
    await b.begin()
    await trade(b, "b", "Z", "broke")
    assert b.credits["broke"] == 2 and b.credits["b"] == 8
    assert "execution fee" not in next(n["body"] for n in b.feed if n.get("event") == "sale")


async def test_the_buyer_fee_counts_when_nobody_can_afford_anything(tmp_path):
    """Cheapest action = cheapest edge + buyer fee (3) or a message (5); with 2 credits each, nobody can act."""
    w = world()
    w["nodes"] = {"x": {"credits": 2, "holds": ["Q"], "needs": []}, "y": {"credits": 2, "holds": [], "needs": ["Q"]}}
    w["edges"] = [{"from": "x", "to": "y", "cost": 2}]
    w["communication_cost"] = 5
    b = Broker(w, EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")))
    await b.begin()
    await b.submit("y", "a guess")  # any action re-checks progress
    assert b.results[0]["reason"] == "no agent can afford any action"
