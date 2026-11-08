"""Trading: OFFER (taxed demand signal), SELL / BUY (two-phase commit), WITHDRAW, and the event order."""

import asyncio
from pathlib import Path

import pytest

from hivemarket.common.config import GraphSpec
from hivemarket.common.events import EventLog, JsonlSink, read_jsonl
from hivemarket.game.graph import ideal_cost
from hivemarket.market.state import Broker, MarketError

# x holds T and sells to z and w; y holds U and sells to z; z sells to w.
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


def world(graph=GRAPH, timeout=90.0, comm=0, tax=1, rounds=1):
    g = GraphSpec.model_validate(graph)
    return {
        "nodes": {n: s.model_dump() for n, s in g.nodes.items()},
        "edges": [{"from": s, "to": d, "cost": c} for (s, d), c in g.edge_costs().items()],
        "communication_cost": comm, "seed": 1,
        "rounds": {"count": rounds, "round_seconds": 60, "pause_seconds": 0, "stop_on_pass": True},
        "ideal": ideal_cost(g, 0, "move", transfer_tax=tax),
        "rules": {"allow_broadcast": True, "graph_visibility": "neighbors", "max_submissions": 3,
                  "token_transfer": "move", "offer_tax": tax, "sale_timeout_seconds": timeout},
    }


def broker(tmp_path, **kw):
    b = Broker(world(**kw), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")), run_dir=tmp_path)
    b.log_path = tmp_path / "m.jsonl"
    return b


@pytest.fixture
async def market(tmp_path):
    b = broker(tmp_path)
    await b.begin()
    return b


def events(b, kind=None):
    return [e for e in read_jsonl(b.log_path) if kind is None or e["kind"] == kind]


def notices(b, agent, event):
    return [m for m in b.inbox(agent) if m.get("event") == event]


async def trade(b, buyer, token, seller):
    oid = (await b.make_offer(buyer, token, seller))["offer_id"]
    await b.sell(seller, oid)
    return await b.buy(buyer, oid)


async def test_offer_is_a_taxed_signal_that_locks_nothing(market):
    b = market
    r = await b.make_offer("z", "U", "x")  # x doesn't hold U: still a valid offer (and the edge x -> z exists)
    assert r["status"] == "open" and r["fee_paid"] == 1 and r["price"] == 5
    assert b.credits["z"] == 9 and b.available("z") == 9 and b.escrow["z"] == {}  # tax burned, nothing reserved
    n = notices(b, "x", "offer_made")[0]
    assert n["offer_id"] == r["offer_id"] and "offers to buy U from you" in n["body"]
    assert events(b, "offer_made")[0]["data"]["seller_holds"] is False
    w = await b.withdraw_offer("z", r["offer_id"])
    assert w["status"] == "withdrawn" and b.credits["z"] == 9  # withdrawing is free, the tax isn't refunded
    assert "withdrew offer" in notices(b, "x", "offer_withdrawn")[0]["body"]


async def test_sell_then_buy(market):
    b = market
    oid = (await b.make_offer("z", "T", "x"))["offer_id"]
    s = await b.sell("x", oid)
    assert s["status"] == "pending" and b.reserved == {"T": oid}
    assert b.escrow["z"] == {oid: 5} and b.available("z") == 4 and b.credits["z"] == 9
    assert "is selling you T" in notices(b, "z", "sale_started")[0]["body"]
    r = await b.buy("z", oid)
    assert r["status"] == "executed" and r["content"] == b.contents["T"] and r["cost"] == 5
    assert b.credits == {"x": 25, "y": 20, "z": 4, "w": 10}
    assert b.holds["z"] == {"T"} and "T" not in b.holds["x"] and b.reserved == {} and b.escrow["z"] == {}
    assert notices(b, "x", "sale")[0]["body"].startswith("Transaction complete: z bought T")
    kinds = [e["kind"] for e in events(b)]
    assert kinds.index("offer_made") < kinds.index("sale_started") < kinds.index("sale_completed") < kinds.index("exchange")


async def test_a_failed_sell_leaves_the_offer_open(market):
    b = market
    oid = (await b.make_offer("z", "T", "y"))["offer_id"]  # y doesn't hold T
    with pytest.raises(MarketError) as e:
        await b.sell("y", oid)
    assert e.value.code == "seller_lacks_token" and b.offers[oid]["status"] == "open"
    b.credits["z"] = 3  # can't pay x's price of 5 right now
    oid2 = (await b.make_offer("z", "T", "x"))["offer_id"]
    with pytest.raises(MarketError) as e:
        await b.sell("x", oid2)
    assert e.value.code == "buyer_cannot_pay" and b.offers[oid2]["status"] == "open" and b.reserved == {}
    assert "you have 2 available. The offer stays open" in notices(b, "z", "sale_unaffordable")[0]["body"]
    b.credits["z"] = 10  # credits arrive later (e.g. from a sale): the same offer can now be sold
    await b.sell("x", oid2)
    assert (await b.buy("z", oid2))["status"] == "executed"


async def test_only_the_executed_offer_closes(market):
    """Arre's example: an offer outlives a sale to someone else, and can be acted on when the token comes back."""
    b = market
    to_x = (await b.make_offer("z", "T", "x"))["offer_id"]   # z asks x
    to_y = (await b.make_offer("z", "T", "y"))["offer_id"]   # z also asks y
    w_x = (await b.make_offer("w", "T", "x"))["offer_id"]    # w asks x too
    await b.sell("x", to_x)
    r = await b.buy("z", to_x)
    assert r["your_other_open_offers_for_this_token"] == [to_y]  # still open, as the spec wants
    assert b.offers[to_y]["status"] == "open" and b.offers[w_x]["status"] == "open"  # x losing T voids nothing
    with pytest.raises(MarketError) as e:
        await b.sell("x", w_x)  # x no longer holds T
    assert e.value.code == "seller_lacks_token" and b.offers[w_x]["status"] == "open"
    # w can also be served by z, which now holds T: demand reached z with w's new offer
    to_z = (await b.make_offer("w", "T", "z"))["offer_id"]
    await b.sell("z", to_z)
    await b.buy("w", to_z)
    assert b.offers[w_x]["status"] == "open"  # w's offer to x is still standing; w should withdraw it
    assert {o["status"] for o in b.offers.values()} == {"executed", "open"}


async def test_a_sale_to_a_buyer_that_holds_the_token_is_refused_but_the_offer_stays(tmp_path):
    """Only reachable when the seller keeps a copy (token_transfer: copy): the buyer can't hold a token twice."""
    b = broker(tmp_path)
    b.token_transfer = "copy"
    await b.begin()
    first = (await b.make_offer("z", "T", "x"))["offer_id"]
    second = (await b.make_offer("w", "T", "x"))["offer_id"]
    await b.sell("x", first)
    await b.buy("z", first)  # x keeps a copy of T
    late = (await b.make_offer("w", "T", "z"))["offer_id"]
    await b.sell("x", second)
    await b.buy("w", second)  # w now holds T
    with pytest.raises(MarketError) as e:
        await b.sell("z", late)
    assert e.value.code == "buyer_holds_token" and b.offers[late]["status"] == "open"


async def test_one_sale_per_token_at_a_time(market):
    b = market
    oz = (await b.make_offer("z", "T", "x"))["offer_id"]
    ow = (await b.make_offer("w", "T", "x"))["offer_id"]
    await b.sell("x", oz)
    with pytest.raises(MarketError) as e:
        await b.sell("x", ow)
    assert e.value.code == "token_reserved" and b.offers[ow]["status"] == "open"
    with pytest.raises(MarketError) as e:
        await b.sell("x", oz)
    assert e.value.code == "sale_pending"
    with pytest.raises(MarketError) as e:
        await b.buy("w", ow)
    assert e.value.code == "no_pending_sale"


async def test_withdraw_during_a_sale_releases_it(market):
    b = market
    oid = (await b.make_offer("z", "T", "x"))["offer_id"]
    await b.sell("x", oid)
    await b.withdraw_offer("z", oid)
    assert b.offers[oid]["status"] == "withdrawn" and b.reserved == {} and b.available("z") == 9
    assert "no longer reserved" in notices(b, "x", "offer_withdrawn")[0]["body"]
    with pytest.raises(MarketError) as e:
        await b.withdraw_offer("z", oid)
    assert e.value.code == "offer_withdrawn"


async def test_a_sale_not_bought_in_time_lapses_back_to_open(tmp_path):
    b = broker(tmp_path, timeout=0.05)
    await b.begin()
    oid = (await b.make_offer("z", "T", "x"))["offer_id"]
    await b.sell("x", oid)
    await asyncio.sleep(0.1)
    await b.tick()
    assert b.offers[oid]["status"] == "open" and b.reserved == {} and b.available("z") == 9
    assert "Offer " + oid + " is open again" in notices(b, "z", "sale_lapsed")[0]["body"]
    assert notices(b, "x", "sale_lapsed")
    with pytest.raises(MarketError) as e:
        await b.buy("z", oid)
    assert e.value.code == "buy_timeout"  # the buyer didn't confirm in time (#32)
    await b.sell("x", oid)  # the same offer can be sold again
    assert (await b.buy("z", oid))["status"] == "executed"


async def test_offer_validation(market):
    b = market
    cases = [(("z", "T", "w"), "no_edge"), (("z", "T", "nobody"), "unknown_seller"), (("z", "T", "z"), "self_offer"),
             (("z", "NOPE", "x"), "unknown_token"), (("x", "T", "z"), "already_held")]
    for args, code in cases:
        with pytest.raises(MarketError) as e:
            await b.make_offer(*args)
        assert e.value.code == code, args
    await b.make_offer("z", "T", "x")
    with pytest.raises(MarketError) as e:
        await b.make_offer("z", "T", "x")
    assert e.value.code == "duplicate_offer" and b.credits["z"] == 9  # no second tax
    b.credits["w"] = 0
    with pytest.raises(MarketError) as e:
        await b.make_offer("w", "T", "x")
    assert e.value.code == "insufficient_credits"
    with pytest.raises(MarketError) as e:
        await b.sell("y", "off-1")  # not the seller it was made to
    assert e.value.code == "not_your_offer"
    with pytest.raises(MarketError) as e:
        await b.buy("x", "off-1")  # not the buyer
    assert e.value.code == "not_your_offer"
    stages = [f["data"]["stage"] for f in events(b, "offer_failed")]
    assert stages == ["offer"] * 7 + ["sell", "buy"]


async def test_demand_propagates_up_a_chain_and_costs_the_ideal(tmp_path):
    """The spec's worked example: y asks m, m (without the token) asks x, then the token comes back down."""
    g = {"nodes": {"x": {"credits": 50, "holds": ["T"], "needs": []},
                   "m": {"credits": 50, "holds": [], "needs": []},
                   "y": {"credits": 50, "holds": [], "needs": ["T"]}},
         "edges": [{"from": "x", "to": "m", "cost": 3}, {"from": "m", "to": "y", "cost": 2}]}
    b = broker(tmp_path, graph=g)
    await b.begin()
    y_asks_m = (await b.make_offer("y", "T", "m"))["offer_id"]
    m_asks_x = (await b.make_offer("m", "T", "x"))["offer_id"]  # m passes the demand on
    await b.sell("x", m_asks_x)
    await b.buy("m", m_asks_x)
    await b.sell("m", y_asks_m)
    await b.buy("y", y_asks_m)
    await b.submit("y", b.contents["T"])
    res = b.results[0]
    assert res["outcome"] == "pass" and res["credits_spent"] == 3 + 2 + 2 * 1 == b.ideal["ideal_credits"]
    assert res["objective"] == [2, 2, 5]  # (communication, transactions, payments)
    assert res["credits_check"]["consistent"] and res["credits_check"]["burned"] == 2


async def test_round_end_clears_offers(market):
    b = market
    o1 = (await b.make_offer("z", "T", "x"))["offer_id"]
    await b.sell("x", o1)
    o2 = (await b.make_offer("w", "T", "x"))["offer_id"]
    res = await b.end_round("fail", "test")
    assert b.offers[o1]["status"] == b.offers[o2]["status"] == "cleared"
    assert b.reserved == {} and all(not e for e in b.escrow.values())
    assert res["offers"] == {"cleared": 2} and res["credits_check"]["consistent"]
    assert res["credits_spent_on_offers"] == 2 and res["per_agent"]["x"]["offers_received"] == 2


async def test_a_sellable_offer_keeps_an_unaffordable_round_open(tmp_path):
    b = broker(tmp_path, tax=6)
    await b.begin()
    b.credits.update({"x": 0, "y": 0, "w": 0, "z": 11})
    oid = (await b.make_offer("z", "T", "x"))["offer_id"]  # z: 5 left, below the tax: nobody can make an offer
    assert all(b.available(n) < 6 for n in b.agents)
    assert b.phase == "active"  # but x could still sell T to z for 5: not a deadlock
    await b.withdraw_offer("z", oid)  # nothing left that could be sold, and nobody can afford an offer
    assert b.phase != "active" and b.results[0]["reason"] == "no agent can afford any action"


async def test_every_market_event_has_a_unique_increasing_seq(market):
    b = market
    await trade(b, "z", "T", "x")
    seqs = [e["data"]["seq"] for e in events(b)]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_concurrent_requests_are_applied_one_at_a_time(market):
    """Each request's events form one contiguous block of seq numbers: nothing interleaves."""
    b = market
    start = len(events(b))
    await asyncio.gather(b.make_offer("z", "T", "x"), b.make_offer("w", "T", "x"),
                         b.make_offer("z", "U", "y"), b.make_offer("w", "T", "z"))
    evs = events(b)[start:]
    assert len({e["data"]["seq"] for e in evs}) == len(evs)
    for i, e in enumerate(evs):
        if e["kind"] == "offer_made":
            oid = e["data"]["offer_id"]
            # each offer: its tax row before it, then its state snapshot and its notice right after
            assert evs[i - 1]["kind"] == "balances" and evs[i - 1]["data"]["offer_id"] == oid
            assert evs[i + 1]["kind"] == "graph_state" and evs[i + 1]["data"]["offer_id"] == oid
            assert evs[i + 2]["kind"] == "notice" and evs[i + 2]["data"]["offer_id"] == oid
    assert b.credits["z"] == 8 and b.credits["w"] == 8


# ------------------------------------------------- messages: off by default (spec §4.3, §5.2)

async def test_free_text_messages_are_off_by_default(market):
    b = market
    with pytest.raises(MarketError) as e:
        await b.send_message("z", "x", "I need T, who has it?")
    assert e.value.code == "messages_disabled" and "make offers" in e.value.message
    assert b.credits["z"] == 10 and not b.messages  # nothing charged, nothing delivered
    assert b.status("z")["messages_enabled"] is False and b.status("z")["communication_cost"] is None
    assert "communication_cost_per_message" not in b.graph_view("z")


async def test_message_cost_doesnt_count_as_an_action_when_messages_are_off(tmp_path):
    """With messages off, an agent that can't afford an offer has nothing left to do."""
    b = broker(tmp_path, comm=1, tax=3)
    await b.begin()
    b.credits.update({"x": 2, "y": 2, "w": 2, "z": 2})  # enough for a message, not for an offer
    await b._check_progress()
    assert b.phase != "active" and b.results[0]["reason"] == "no agent can afford any action"


def test_agents_only_get_messages_when_enabled():
    from hivemarket.agent.prompts import render_system_prompt
    from hivemarket.agent.tools import tools_for_config
    from hivemarket.common.config import AffordanceConfig, AgentRuntimeConfig, AgentSpec
    from hivemarket.common.paths import repo_root

    template = (repo_root() / "prompts" / "system.md").read_text()
    base = dict(agent_id="a", token="t", run_id="r", market_url="m", collector_url="c", gateway_url="g",
                provider_kind="scripted", spec=AgentSpec(provider="scripted"), affordance=AffordanceConfig(level="none"),
                prompt_template=template)
    status = {"participants": ["a", "b"], "communication_cost": 2, "offer_tax": 1, "sale_timeout_seconds": 90}
    off = AgentRuntimeConfig(**base)
    on = AgentRuntimeConfig(**base, messages=True)
    assert "send_message" not in {t.name for t in tools_for_config(off)}
    assert "send_message" in {t.name for t in tools_for_config(on)}
    p_off, p_on = render_system_prompt(off, status), render_system_prompt(on, status)
    assert "send_message" not in p_off and "no free-text messages" in p_off and "$" not in p_off
    assert "`send_message`" in p_on and "burns 2 credit(s) per recipient" in p_on


def test_experiment_setting_reaches_the_world_and_agents(tmp_path):
    import json
    import yaml
    from hivemarket.orchestrator.run import create_run

    root = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root / "experiments" / "local_smoke.yaml").read_text())
    for messages in (None, True):
        if messages is not None:
            cfg["market"] = {"messages": messages}
        path = tmp_path / f"exp-{messages}.yaml"
        path.write_text(yaml.safe_dump(cfg))
        run = create_run(path, tmp_path / "runs", mode="local", run_id=f"r-{messages}")
        world = json.loads((run / "world.json").read_text())
        agent = json.loads((run / "agents" / "agent-1.json").read_text())
        assert world["rules"]["messages"] is bool(messages) and agent["messages"] is bool(messages)


async def test_status_keeps_the_agents_own_result_for_the_last_round(tmp_path):
    """What the agent spent, by kind, in the last round that ended; still there once the next one starts."""
    b = broker(tmp_path, rounds=2)
    await b.begin()
    assert b.status("z")["last_round_result"] is None
    o = await b.make_offer("z", "T", "x")
    await b.make_offer("z", "U", "y")  # never sold: only its tax
    await b.sell("x", o["offer_id"])
    await b.buy("z", o["offer_id"])
    await b.end_round("fail", "test")
    own = b.status("z")["last_round_result"]
    assert (own["round"], own["spent_on_tokens"], own["spent_on_offers"], own["credits_left"]) == (1, 5, 2, 3)
    assert b.status("x")["last_round_result"]["received"] == 5
    await b._start_round(2)
    assert b.status("z")["last_round_result"]["round"] == 1 and b.status("z")["credits"] == 10


# ------------------------------------------------- declare_stuck: can be switched off

async def test_declare_stuck_can_be_switched_off(tmp_path):
    w = world()
    w["rules"]["declare_stuck"] = False
    b = Broker(w, EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")), run_dir=tmp_path)
    await b.begin()
    with pytest.raises(MarketError) as e:
        await b.declare_stuck("z", "out of ideas")
    assert e.value.code == "declare_stuck_disabled"
    assert b.phase == "active" and not b.stuck  # the round goes on


async def test_declare_stuck_still_ends_the_round_by_default(market):
    assert (await market.declare_stuck("z", "out of credits"))["round_ended"] is True


def test_agents_only_get_declare_stuck_when_enabled():
    from hivemarket.agent.prompts import render_system_prompt
    from hivemarket.agent.tools import tools_for_config
    from hivemarket.common.config import AffordanceConfig, AgentRuntimeConfig, AgentSpec
    from hivemarket.common.paths import repo_root

    template = (repo_root() / "prompts" / "system.md").read_text()
    base = dict(agent_id="a", token="t", run_id="r", market_url="m", collector_url="c", gateway_url="g",
                provider_kind="scripted", spec=AgentSpec(provider="scripted"), affordance=AffordanceConfig(level="none"),
                prompt_template=template)
    status = {"participants": ["a", "b"], "offer_tax": 1, "sale_timeout_seconds": 90}
    on, off = AgentRuntimeConfig(**base), AgentRuntimeConfig(**base, declare_stuck=False)
    assert "declare_stuck" in {t.name for t in tools_for_config(on)}
    assert "declare_stuck" not in {t.name for t in tools_for_config(off)}
    p_on, p_off = render_system_prompt(on, status), render_system_prompt(off, status)
    assert "`declare_stuck`" in p_on
    assert "declare_stuck" not in p_off and "$" not in p_off


def test_the_brokerage_prompt_gets_its_broker_fee():
    from hivemarket.agent.prompts import render_system_prompt
    from hivemarket.common.config import AffordanceConfig, AgentRuntimeConfig, AgentSpec

    cfg = AgentRuntimeConfig(agent_id="a", token="t", run_id="r", market_url="m", collector_url="c", gateway_url="g",
                             provider_kind="scripted", spec=AgentSpec(provider="scripted"),
                             affordance=AffordanceConfig(level="none"),
                             prompt_template="Each offer costs a broker fee of $broker_fee credit(s).")
    assert render_system_prompt(cfg, {"offer_tax": 2}) == "Each offer costs a broker fee of 2 credit(s).\n"


# ------------------------------------------------- deadlock and the ideal: experiment switches

def poor_world(**rounds):
    """Nobody can afford an offer, so the round is deadlocked from the start."""
    w = world()
    for n in w["nodes"].values():
        n["credits"] = 0
    w["rounds"] |= rounds
    return w


async def test_a_deadlock_ends_the_round_by_default(tmp_path):
    b = Broker(poor_world(), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")), run_dir=tmp_path)
    await b.begin()
    await b._check_progress()
    assert b.results and b.results[-1]["reason"] == "no agent can afford any action"


async def test_without_end_on_deadlock_the_round_runs_on_and_logs_the_deadlock_once(tmp_path):
    b = Broker(poor_world(end_on_deadlock=False), EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")),
               run_dir=tmp_path)
    b.log_path = tmp_path / "m.jsonl"
    await b.begin()
    await b._check_progress()
    await b._check_progress()
    assert b.phase == "active" and not b.results
    [d] = events(b, "deadlock")
    assert d["data"]["round"] == 1 and d["data"]["credits"] == {"x": 0, "y": 0, "z": 0, "w": 0}


async def test_the_ideal_can_be_kept_from_agents(tmp_path):
    for announce in (True, False):
        w = world()
        w["rounds"]["announce_ideal"] = announce
        b = Broker(w, EventLog("market", "r", JsonlSink(tmp_path / f"{announce}.jsonl")), run_dir=tmp_path)
        await b.begin()
        result = await b.end_round("fail", "test")
        [notice] = notices(b, "z", "round_end")
        assert ("Ideal cost" in notice["body"]) is announce
        assert result["ideal_credits"] is not None  # observers always have it


def test_the_prompt_mentions_the_ideal_only_when_announced():
    from hivemarket.agent.prompts import render_system_prompt
    from hivemarket.common.config import AffordanceConfig, AgentRuntimeConfig, AgentSpec
    from hivemarket.common.paths import repo_root

    base = dict(agent_id="a", token="t", run_id="r", market_url="m", collector_url="c", gateway_url="g",
                provider_kind="scripted", spec=AgentSpec(provider="scripted"), affordance=AffordanceConfig(level="none"),
                prompt_template=(repo_root() / "prompts" / "system.md").read_text())
    status = {"participants": ["a", "b"], "offer_tax": 1, "sale_timeout_seconds": 90}
    assert "ideal" in render_system_prompt(AgentRuntimeConfig(**base), status)
    hidden = render_system_prompt(AgentRuntimeConfig(**base, announce_ideal=False), status)
    assert "ideal" not in hidden and "$" not in hidden


# ------------------------------------------------------------- scores and reason codes (#32)

async def test_reason_codes_for_closed_offers_and_a_buy_before_any_sale(market):
    b = market
    oid = (await b.make_offer("z", "T", "x"))["offer_id"]
    with pytest.raises(MarketError) as e:
        await b.buy("z", oid)  # nobody has sold yet
    assert e.value.code == "no_pending_sale"
    await b.sell("x", oid)
    await b.buy("z", oid)
    for call, code in [(b.sell("x", oid), "offer_filled"), (b.buy("z", oid), "offer_filled"),
                       (b.withdraw_offer("z", oid), "offer_filled")]:
        with pytest.raises(MarketError) as e:
            await call
        assert e.value.code == code
    other = (await b.make_offer("w", "T", "z"))["offer_id"]
    await b.withdraw_offer("w", other)
    with pytest.raises(MarketError) as e:
        await b.sell("z", other)
    assert e.value.code == "offer_withdrawn"
    fails = [d["data"]["reason"] for d in events(b, "offer_failed")]
    assert fails == ["no_pending_sale", "offer_filled", "offer_filled", "offer_filled", "offer_withdrawn"]


async def test_scores_per_agent_round_and_game_and_the_config_with_them(tmp_path):
    b = broker(tmp_path, rounds=2)
    await b.begin()
    await trade(b, "z", "T", "x")              # z: price 5 + tax 1
    await b.make_offer("w", "T", "z")          # w: tax 1 (never sold)
    await b.end_round("fail", "test")
    await b.tick()                             # pause 0: round 2
    await b.make_offer("z", "U", "y")          # z: tax 1
    await b.end_round("fail", "test")
    ends = [e["data"] for e in events(b, "round_end")]
    r1 = ends[0]
    assert r1["per_agent"]["z"]["credits_spent"] == 6 and r1["per_agent"]["z"]["communication_spent"] == 1
    assert r1["per_agent"]["z"]["credits_remaining"] == 4 == r1["per_agent"]["z"]["credits_left"]
    assert r1["per_agent"]["x"]["credits_spent"] == 0 and r1["per_agent"]["x"]["credits_remaining"] == 25
    assert r1["scores"] == {"credits_spent": 7, "credits_remaining": 58, "communication_spent": 2}
    assert sum(a["credits_spent"] for a in r1["per_agent"].values()) == r1["credits_spent"] == 7
    assert r1["config"]["offer_tax"] == 1 and r1["config"]["messages_enabled"] is False
    assert events(b, "round_start")[0]["data"]["config"] == r1["config"]
    game = events(b, "game_over")[0]["data"]
    assert game["scores"]["credits_spent"] == 7 + 1 and game["scores"]["communication_spent"] == 2 + 1
    assert game["scores"]["credits_remaining"] == 58 + 59  # summed over rounds (credits reset each round)
    assert game["scores"]["per_agent"]["z"] == {"credits_spent": 7, "credits_remaining": 13, "communication_spent": 2}
    assert game["config"] == r1["config"]


async def test_graph_state_says_whether_each_agent_could_still_act(tmp_path):
    poor = {**GRAPH, "nodes": {**GRAPH["nodes"], "w": {"credits": 0, "holds": [], "needs": ["T"]}}}
    b = broker(tmp_path, graph=poor)
    await b.begin()
    s = events(b, "graph_state")[0]["data"]
    assert s["could_act"] == {"x": True, "y": True, "z": True, "w": False}  # w: no credits for an offer, no tokens


async def test_replaying_the_ledger_reproduces_every_graph_state(market):
    """balances (credits) and exchange (holdings) rebuild each snapshot, so either can be trusted (#32)."""
    b = market
    await trade(b, "z", "T", "x")
    oid = (await b.make_offer("w", "T", "z"))["offer_id"]
    await b.sell("z", oid)
    await b.withdraw_offer("w", oid)
    await trade(b, "w", "T", "z")
    credits = {n: s["credits"] for n, s in GRAPH["nodes"].items()}
    holds = {n: set(s["holds"]) for n, s in GRAPH["nodes"].items()}
    checked = 0
    for e in events(b):
        d = e["data"]
        if e["kind"] == "balances":
            credits = dict(d["credits"])
        elif e["kind"] == "exchange":
            holds[d["buyer"]].add(d["token"])
            holds[d["seller"]].discard(d["token"])  # token_transfer: move
        elif e["kind"] == "graph_state":
            assert d["credits"] == credits, (d["cause"], d["credits"], credits)
            assert {n: set(h) for n, h in d["holdings"].items()} == holds, d["cause"]
            checked += 1
    assert checked >= 8


def test_agents_see_a_broker_fee_view_prices_and_no_zero_execution_fee(tmp_path):
    """Agent-facing wording: "broker fee", not "tax"; no graph language in tool names; a fee never charged isn't shown."""
    from hivemarket.agent.tools import MARKET_TOOLS

    b = broker(tmp_path)
    s = b.status("z")
    assert s["broker_fee"] == 1 and "offer_tax" not in s and "execution_fee" not in s
    names = {t.name for t in MARKET_TOOLS}
    assert "view_prices" in names and "view_graph" not in names
    text = " ".join(t.description for t in MARKET_TOOLS).lower()
    assert "tax" not in text and "broker fee" in text
    b.fee = {"buyer": 1, "seller": 0}  # a market that does charge one shows it
    assert b.status("z")["execution_fee"] == {"buyer": 1, "seller": 0}


def test_the_prompt_gets_the_session_length_in_minutes():
    from hivemarket.agent.prompts import render_system_prompt
    from hivemarket.common.config import AffordanceConfig, AgentRuntimeConfig, AgentSpec

    def render(**kw):
        cfg = AgentRuntimeConfig(agent_id="a", token="t", run_id="r", market_url="m", collector_url="c",
                                 gateway_url="g", provider_kind="scripted", spec=AgentSpec(provider="scripted"),
                                 affordance=AffordanceConfig(level="none"),
                                 prompt_template="The session lasts $session_minutes minutes.", **kw)
        return render_system_prompt(cfg, {"seconds_left_in_round": 287.4})

    assert render(round_seconds=900) == "The session lasts 15 minutes.\n"
    assert render(round_seconds=450) == "The session lasts 7.5 minutes.\n"
    assert render() == "The session lasts 4.8 minutes.\n"  # older agent configs: the time left at the start


def test_create_run_passes_the_time_limit_to_every_agent(tmp_path):
    import json

    from hivemarket.orchestrator.run import create_run

    exp = tmp_path / "e.yaml"
    exp.write_text(
        "name: t\nseed: 1\nagents: [{provider: scripted, model: scripted, count: 2}]\n"
        "graph:\n  nodes:\n    agent-1: {credits: 5, holds: [A], needs: []}\n    agent-2: {credits: 5, needs: [A]}\n"
        "  edges: [{from: agent-1, to: agent-2, cost: 1}]\n"
        "rounds: {count: 1, round_seconds: 600}\n")
    run_dir = create_run(exp, tmp_path / "runs", mode="local", local_base_port=19870)
    for f in (run_dir / "agents").glob("*.json"):
        assert json.loads(f.read_text())["round_seconds"] == 600
