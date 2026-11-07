"""Execution fees: burned on every completed sale, by buyer and seller, and affordable when the sale starts."""

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
        # no offer tax, so the arithmetic below is the price and the fees alone
        "rules": {"offer_tax": 0, "messages": True, "execution_fee": {"buyer": buyer_fee, "seller": seller_fee}},
    }


@pytest.fixture
async def broker(tmp_path):
    b = Broker(world(), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")))
    await b.begin()
    return b


async def trade(b: Broker, buyer: str, token: str, seller: str) -> dict:
    o = await b.make_offer(buyer, token, seller)
    await b.sell(seller, o["offer_id"])
    return await b.buy(buyer, o["offer_id"])


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
    assert res["objective"] == [2, 1, 4]  # fees count as communication, as in market_generator
    assert "execution fees 2" in next(n["body"] for n in broker.feed if n.get("event") == "round_end")


async def test_buyer_must_afford_price_plus_fee(broker):
    o = await broker.make_offer("poor", "A", "s")  # 4 credits: the price, but not price + fee
    with pytest.raises(MarketError) as e:
        await broker.sell("s", o["offer_id"])
    assert e.value.code == "buyer_cannot_pay" and "can't pay 5 credits" in e.value.message
    notice = next(n["body"] for n in broker.feed if n.get("event") == "sale_unaffordable")
    assert "price 4 + execution fee 1" in notice
    assert broker.offers[o["offer_id"]]["status"] == "open"  # the demand signal survives


async def test_seller_must_afford_its_fee_when_the_sale_starts(broker):
    o = await broker.make_offer("b", "Z", "broke")
    with pytest.raises(MarketError) as e:
        await broker.sell("broke", o["offer_id"])  # 0 credits: can't pay its fee out of the sale
    assert e.value.code == "insufficient_credits"
    assert broker.offers[o["offer_id"]]["status"] == "open"


async def test_seller_fee_is_reserved_until_the_sale_completes(broker):
    o = await broker.make_offer("b", "A", "s")
    await broker.sell("s", o["offer_id"])
    assert broker.available("b") == 10 - 4 - 1  # the price and the buyer's fee are reserved
    assert broker.available("s") == 0  # its 1 credit is held for the fee
    with pytest.raises(MarketError) as e:
        await broker.send_message("s", "b", "hi")
    assert e.value.code == "insufficient_credits"
    await broker.withdraw_offer("b", o["offer_id"])
    assert broker.available("s") == 1 and broker.available("b") == 10  # both released with the sale


async def test_without_fees_a_zero_credit_agent_can_still_sell(tmp_path):
    b = Broker(world(buyer_fee=0, seller_fee=0), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")))
    await b.begin()
    await trade(b, "b", "Z", "broke")
    assert b.credits["broke"] == 2 and b.credits["b"] == 8
    assert "execution fee" not in next(n["body"] for n in b.feed if n.get("event") == "sale")


async def test_the_buyer_fee_counts_when_deciding_a_deadlock(tmp_path):
    """y's offer can't be sold: y has the price (2) but not the price plus its fee (3), and nobody can
    afford another offer (tax 3). Without the fee the offer would still be sellable."""
    w = world(buyer_fee=1, seller_fee=0)
    w["nodes"] = {"x": {"credits": 2, "holds": ["Q"], "needs": []}, "y": {"credits": 5, "holds": [], "needs": ["Q"]}}
    w["edges"] = [{"from": "x", "to": "y", "cost": 2}]
    w["rules"] |= {"offer_tax": 3, "messages": False}
    b = Broker(w, EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")))
    await b.begin()
    await b.make_offer("y", "Q", "x")  # y is left with 2
    assert b.results[0]["reason"] == "no agent can afford any action"


async def test_the_seller_fee_counts_when_deciding_a_deadlock(tmp_path):
    """x holds Q and has no credits, so it can't pay its fee to sell; nobody can afford another offer."""
    w = world(buyer_fee=0, seller_fee=1)
    w["nodes"] = {"x": {"credits": 0, "holds": ["Q"], "needs": []}, "y": {"credits": 5, "holds": [], "needs": ["Q"]}}
    w["edges"] = [{"from": "x", "to": "y", "cost": 2}]
    w["rules"] |= {"offer_tax": 3, "messages": False}
    b = Broker(w, EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")))
    await b.begin()
    await b.make_offer("y", "Q", "x")  # y is left with 2: enough for the price, not for another offer
    assert b.results[0]["reason"] == "no agent can afford any action"
