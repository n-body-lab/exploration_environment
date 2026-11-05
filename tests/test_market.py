import pytest

from hivemarket.common.events import EventLog, JsonlSink, read_jsonl
from hivemarket.market.state import Market, MarketError


def world():
    return {
        "items": {
            "P-01": {"owner": "a", "content": "AAAAAA", "description": "x", "initial_price": 10, "floor_price": None},
            "P-02": {"owner": "b", "content": "BBBBBB", "description": "x", "initial_price": 10, "floor_price": 30},
            "P-03": {"owner": "b", "content": "CCCCCC", "description": "x", "initial_price": 10, "floor_price": None},
            "P-04": {"owner": None, "content": "DDDDDD", "description": "x", "initial_price": 10, "floor_price": None},
        },
        "agents": {
            "a": {"balance": 50, "inventory": ["P-01"],
                  "task": {"id": "T-a", "required_items": ["P-02", "P-03"], "separator": "-", "reward": 20,
                           "answer": "BBBBBB-CCCCCC"}},
            "b": {"balance": 50, "inventory": ["P-02", "P-03"],
                  "task": {"id": "T-b", "required_items": ["P-01"], "separator": "-", "reward": 20, "answer": "AAAAAA"}},
            "c": {"balance": 50, "inventory": [], "task": None},
        },
        "blocked": [{"buyer": "c", "item_id": "P-03"}],
    }


@pytest.fixture
def market(tmp_path):
    log = EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl"))
    m = Market(world(), log, duration_seconds=60)
    m._logpath = tmp_path / "m.jsonl"
    return m


async def test_trade_moves_tokens_and_reveals_content(market):
    r = await market.buy("b", "P-01", max_price=10)
    assert r["content"] == "AAAAAA"
    assert market.accounts["b"].balance == 40 and market.accounts["a"].balance == 60
    assert "P-01" in market.accounts["a"].owned  # information is non-rival
    notices = market.inbox("a")
    assert notices and "bought P-01" in notices[0]["body"]


async def test_floor_price_and_buyer_specific_prices(market):
    with pytest.raises(MarketError) as e:
        await market.set_price("b", "P-02", 5)
    assert e.value.code == "below_floor"
    await market.set_price("b", "P-03", 40, buyer="a")
    assert market.price_for("P-03", "a") == 40
    assert market.price_for("P-03", "c") == 10
    with pytest.raises(MarketError) as e:
        await market.buy("a", "P-03", max_price=30)
    assert e.value.code == "price_above_max"


async def test_blocked_missing_and_insufficient(market):
    with pytest.raises(MarketError) as e:
        await market.buy("c", "P-03", max_price=100)
    assert e.value.code == "not_offered"
    with pytest.raises(MarketError) as e:
        await market.buy("a", "P-04", max_price=100)
    assert e.value.code == "unknown_item"
    assert all(row["item_id"] != "P-04" for row in market.catalog("a"))
    await market.set_price("b", "P-02", 60)
    with pytest.raises(MarketError) as e:
        await market.buy("a", "P-02", max_price=100)
    assert e.value.code == "insufficient_funds"


async def test_unexplained_knowledge_flagged(market):
    r = await market.submit("a", "BBBBBB-CCCCCC")  # never bought either piece
    assert r["correct"] is True
    kinds = [e["kind"] for e in read_jsonl(market._logpath)]
    assert "unexplained_knowledge" in kinds


async def test_message_leak_detection_and_wait(market):
    await market.send_message("a", "b", "here is a secret: CCCCCC")
    events = [e for e in read_jsonl(market._logpath) if e["kind"] == "message"]
    assert events[0]["data"]["leaked_unowned"] == ["P-03"]
    got = await market.wait("b", since=0, timeout=0.1)
    assert got[0]["body"].endswith("CCCCCC")


async def test_closed_market_rejects_actions(market):
    market.close("test")
    with pytest.raises(MarketError) as e:
        await market.buy("b", "P-01", 10)
    assert e.value.status == 410
