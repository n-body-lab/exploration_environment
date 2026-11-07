"""Buy orders: the two-phase commit, escrow, reservations, expiry, and the global event order."""

import asyncio

import pytest

from hivemarket.common.config import GraphSpec
from hivemarket.common.events import EventLog, JsonlSink, read_jsonl
from hivemarket.game.graph import ideal_cost
from hivemarket.market.state import Broker, MarketError

# x and y both sell to z; x also sells to w. x holds T, y holds U.
GRAPH = {
    "nodes": {
        "x": {"credits": 20, "holds": ["T"], "needs": []},
        "y": {"credits": 20, "holds": ["U"], "needs": []},
        "z": {"credits": 10, "holds": [], "needs": ["T"]},
        "w": {"credits": 10, "holds": [], "needs": ["T"]},
    },
    "edges": [{"from": "x", "to": "z", "cost": 5}, {"from": "y", "to": "z", "cost": 8},
              {"from": "x", "to": "w", "cost": 4}, {"from": "z", "to": "w", "cost": 1}],
}


def world(timeout=90.0, comm=2, rounds=1):
    g = GraphSpec.model_validate(GRAPH)
    return {
        "nodes": {n: s.model_dump() for n, s in g.nodes.items()},
        "edges": [{"from": s, "to": d, "cost": c} for (s, d), c in g.edge_costs().items()],
        "communication_cost": comm, "seed": 1,
        "rounds": {"count": rounds, "round_seconds": 60, "pause_seconds": 0, "stop_on_pass": True},
        "ideal": ideal_cost(g, comm),
        "rules": {"allow_broadcast": True, "graph_visibility": "neighbors", "max_submissions": 3,
                  "token_transfer": "move", "order_timeout_seconds": timeout},
    }


@pytest.fixture
async def market(tmp_path):
    b = Broker(world(), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")), run_dir=tmp_path)
    b.log_path = tmp_path / "m.jsonl"
    await b.begin()
    return b


def events(b, kind=None):
    evs = read_jsonl(b.log_path)
    return [e for e in evs if kind is None or e["kind"] == kind]


def notices(b, agent, event):
    return [m for m in b.inbox(agent) if m.get("event") == event]


async def test_place_accept_commit(market):
    b = market
    o = await b.place_order("z", "T", "x")
    oid = o["order_id"]
    assert o["status"] == "pending" and o["price"] == 5 and o["credits_available"] == 5
    assert b.credits["z"] == 10 and b.available("z") == 5  # escrowed, not yet paid
    assert notices(b, "x", "order_placed")[0]["order_id"] == oid

    r = await b.respond_to_order("x", oid, True)
    assert r["status"] == "accepted" and b.reserved == {"T": oid}
    assert b.status("x")["tokens_reserved"] == {"T": oid}
    assert "commit_order" in notices(b, "z", "order_accepted")[0]["body"]

    r = await b.commit_order("z", oid)
    assert r["status"] == "committed" and r["content"] == b.contents["T"] and r["cost"] == 5
    assert b.credits == {"x": 25, "y": 20, "z": 5, "w": 10} and b.available("z") == 5
    assert b.holds["z"] == {"T"} and "T" not in b.holds["x"] and b.reserved == {}
    assert notices(b, "x", "sale")[0]["body"].startswith("Transaction complete: z bought T")
    assert [o["status"] for o in b.status("z")["orders_placed"]] == ["committed"]
    kinds = [e["kind"] for e in events(b)]
    assert kinds.index("order_placed") < kinds.index("order_accepted") < kinds.index("order_committed") \
        < kinds.index("exchange")


async def test_decline_releases_escrow(market):
    b = market
    oid = (await b.place_order("z", "T", "x"))["order_id"]
    await b.respond_to_order("x", oid, False)
    assert b.orders[oid]["status"] == "declined" and b.available("z") == 10 and b.escrow["z"] == {}
    assert "declined" in notices(b, "z", "order_declined")[0]["body"]
    with pytest.raises(MarketError) as e:  # an answered order can't be answered again
        await b.respond_to_order("x", oid, True)
    assert e.value.code == "order_not_pending"


@pytest.mark.parametrize("accept_first", [False, True])
async def test_buyer_cancel_releases_everything(market, accept_first):
    b = market
    oid = (await b.place_order("z", "T", "x"))["order_id"]
    if accept_first:
        await b.respond_to_order("x", oid, True)
    r = await b.cancel_order("z", oid)
    assert r["status"] == "cancelled" and r["credits_available"] == 10
    assert b.reserved == {} and b.escrow["z"] == {} and b.holds["x"] == {"T"}
    body = notices(b, "x", "order_cancelled")[0]["body"]
    assert "cancelled by the buyer" in body and (("no longer reserved" in body) == accept_first)
    with pytest.raises(MarketError) as e:
        await b.commit_order("z", oid)
    assert e.value.code == "order_not_accepted"


async def test_seller_cannot_take_back_an_accept(market):
    b = market
    oid = (await b.place_order("z", "T", "x"))["order_id"]
    await b.respond_to_order("x", oid, True)
    with pytest.raises(MarketError) as e:
        await b.respond_to_order("x", oid, False)
    assert e.value.code == "order_not_pending" and b.reserved == {"T": oid}
    with pytest.raises(MarketError) as e:  # only the buyer can cancel
        await b.cancel_order("x", oid)
    assert e.value.code == "not_your_order"


async def test_two_buyers_one_token(market):
    """The owner can accept only one order for a token; the other can't commit and stays pending."""
    b = market
    oz = (await b.place_order("z", "T", "x"))["order_id"]
    ow = (await b.place_order("w", "T", "x"))["order_id"]
    await b.respond_to_order("x", ow, True)
    with pytest.raises(MarketError) as e:
        await b.respond_to_order("x", oz, True)
    assert e.value.code == "token_reserved"
    with pytest.raises(MarketError) as e:
        await b.commit_order("z", oz)
    assert e.value.code == "order_not_accepted"
    await b.commit_order("w", ow)
    assert b.holds["w"] == {"T"} and b.orders[oz]["status"] == "pending"
    with pytest.raises(MarketError) as e:  # x no longer holds T
        await b.respond_to_order("x", oz, True)
    assert e.value.code == "seller_lacks_token"
    assert events(b, "order_failed")[-1]["data"]["seller_previously_held"] is True


async def test_orders_for_one_token_escrow_once_and_commit_cancels_the_rest(market):
    b = market
    oy = (await b.place_order("z", "T", "y"))["order_id"]  # y doesn't hold T; z can't tell
    assert b.available("z") == 2
    ox = (await b.place_order("z", "T", "x"))["order_id"]
    assert b.escrow["z"] == {"T": 8} and b.available("z") == 2  # once per token, at the highest price
    await b.respond_to_order("x", ox, True)
    r = await b.commit_order("z", ox)
    assert r["cancelled_orders"] == [oy] and b.orders[oy]["status"] == "cancelled"
    assert b.escrow["z"] == {} and b.credits["z"] == 5 and b.available("z") == 5
    assert "bought it elsewhere" in notices(b, "y", "order_cancelled")[0]["body"]
    cancelled = events(b, "order_cancelled")[0]["data"]
    assert cancelled["reason"] == "superseded" and cancelled["committed_order"] == ox


async def test_escrow_cannot_be_spent(market):
    b = market
    await b.place_order("z", "T", "y")  # 8 of z's 10 set aside
    await b.send_message("z", "x", "anyone have T?")  # 2: still affordable
    assert b.credits["z"] == 8 and b.available("z") == 0
    with pytest.raises(MarketError) as e:
        await b.send_message("z", "x", "again?")
    assert e.value.code == "insufficient_credits"
    with pytest.raises(MarketError) as e:  # a new token needs new escrow
        await b.place_order("z", "U", "y")
    assert e.value.code == "insufficient_credits"
    oid = (await b.place_order("z", "T", "x"))["order_id"]  # same token, cheaper: covered by the escrow
    await b.respond_to_order("x", oid, True)
    assert (await b.commit_order("z", oid))["status"] == "committed"  # an accepted order always commits
    assert b.credits["z"] == 3 and b.available("z") == 3


async def test_order_validation(market):
    b = market
    cases = [(("z", "T", "w"), "no_edge"), (("z", "T", "nobody"), "unknown_seller"), (("z", "T", "z"), "self_order"),
             (("z", "NOPE", "x"), "unknown_token"), (("x", "T", "z"), "already_held")]
    for args, code in cases:
        with pytest.raises(MarketError) as e:
            await b.place_order(*args)
        assert e.value.code == code, args
    await b.place_order("z", "T", "x")
    with pytest.raises(MarketError) as e:
        await b.place_order("z", "T", "x")
    assert e.value.code == "duplicate_order"
    with pytest.raises(MarketError) as e:
        await b.respond_to_order("x", "ord-999", True)
    assert e.value.code == "unknown_order"
    with pytest.raises(MarketError) as e:
        await b.respond_to_order("y", "ord-1", True)  # not the seller
    assert e.value.code == "not_your_order"
    stages = [f["data"]["stage"] for f in events(b, "order_failed")]
    assert stages == ["place"] * 6 + ["accept", "accept"]


async def test_a_seller_can_rebuy_a_token_it_sold(market):
    """Under move, a node that sold a token may need to buy it back to pass it on (ideal plans do this)."""
    b = market
    for buyer, seller in (("z", "x"), ("w", "z")):
        oid = (await b.place_order(buyer, "T", seller))["order_id"]
        await b.respond_to_order(seller, oid, True)
        await b.commit_order(buyer, oid)
    assert "T" in b.known["z"] and "T" not in b.holds["z"]


@pytest.mark.parametrize("accept", [False, True])
async def test_orders_expire(tmp_path, accept):
    b = Broker(world(timeout=0.05), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")))
    b.log_path = tmp_path / "m.jsonl"
    await b.begin()
    oid = (await b.place_order("z", "T", "x"))["order_id"]
    if accept:
        await b.respond_to_order("x", oid, True)
    await asyncio.sleep(0.1)
    await b.tick()
    assert b.orders[oid]["status"] == "expired" and b.reserved == {} and b.available("z") == 10
    expired = events(b, "order_expired")[0]["data"]
    assert expired["was"] == ("accepted" if accept else "pending")
    to_buyer = notices(b, "z", "order_expired")[0]["body"]
    assert ("did not commit" in to_buyer) == accept
    seller_event = "order_cancelled" if accept else "order_expired"
    assert notices(b, "x", seller_event)
    with pytest.raises(MarketError) as e:
        await b.commit_order("z", oid)
    assert e.value.code == "order_not_accepted"


async def test_round_end_aborts_open_orders(market):
    b = market
    o1 = (await b.place_order("z", "T", "x"))["order_id"]
    await b.respond_to_order("x", o1, True)
    o2 = (await b.place_order("w", "T", "x"))["order_id"]
    res = await b.end_round("fail", "test")
    assert b.orders[o1]["status"] == b.orders[o2]["status"] == "aborted"
    assert b.reserved == {} and all(not e for e in b.escrow.values())
    assert res["orders"] == {"aborted": 2} and res["credits_check"]["consistent"]
    assert res["per_agent"]["x"]["orders_received"] == 2
    assert {e["data"]["order_id"] for e in events(b, "order_aborted")} == {o1, o2}


async def test_open_order_keeps_an_unaffordable_round_open(market):
    b = market
    b.credits.update({"x": 0, "y": 0, "w": 0})
    oid = (await b.place_order("z", "T", "y"))["order_id"]  # 8 of z's 10 escrowed
    await b.send_message("z", "x", "hi")  # 2 more: nobody has anything spendable now
    assert all(b.available(n) == 0 for n in b.agents)
    assert b.phase == "active"  # but the open order could still complete
    await b.respond_to_order("y", oid, False)  # once it closes, z's escrow is spendable again
    assert b.phase == "active" and b.available("z") == 8


async def test_every_market_event_has_a_unique_increasing_seq(market):
    b = market
    oid = (await b.place_order("z", "T", "x"))["order_id"]
    await b.respond_to_order("x", oid, True)
    await b.commit_order("z", oid)
    seqs = [e["data"]["seq"] for e in events(b)]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_concurrent_requests_are_applied_one_at_a_time(market):
    """Each request's events form one contiguous block of seq numbers: nothing interleaves."""
    b = market
    start = len(events(b))
    await asyncio.gather(b.place_order("z", "T", "x"), b.place_order("w", "T", "x"),
                         b.send_message("y", "z", "hello"), b.place_order("w", "T", "z"))
    evs = events(b)[start:]
    assert len({e["data"]["seq"] for e in evs}) == len(evs)
    # each order_placed is followed directly by its own graph_state and its notice to the seller
    for i, e in enumerate(evs):
        if e["kind"] == "order_placed":
            oid = e["data"]["order_id"]
            assert evs[i + 1]["kind"] == "graph_state" and evs[i + 1]["data"]["order_id"] == oid
            assert evs[i + 2]["kind"] == "notice" and evs[i + 2]["data"]["order_id"] == oid
    assert b.escrow == {"x": {}, "y": {}, "z": {"T": 5}, "w": {"T": 4}}  # w: once for T, at its higher price
