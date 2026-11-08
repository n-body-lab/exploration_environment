"""market_generator games: config, conversion, the generator's ideal, reconciliation, fragility,
impossibility, determinism, and that none of the generator's data reaches agents."""

import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from hivemarket.agent.prompts import kickoff_text, render_system_prompt
from hivemarket.common.config import AgentRuntimeConfig, ExperimentConfig
from hivemarket.common.events import EventLog, JsonlSink
from hivemarket.market.state import Broker, MarketError

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).parent
needs_generator = pytest.mark.skipif(importlib.util.find_spec("hivemarket_generator") is None,
                                     reason='market_generator is not installed: pip install -e ".[generator]"')
# Observer-only values from world.json / generator.json that must never show up in what agents see.
GENERATOR_ONLY = ("ad0fb8c", "indirect_dependency_fraction", "reference_budgets", "ideal_status", "graph_ideal",
                  "schedule", "fragility", "optimality_status")


def config(tasks=None, agents=3, seed=1, **extra) -> ExperimentConfig:
    tasks = tasks if tasks is not None else {"market": {"n_tokens": 4}}
    return ExperimentConfig.model_validate({
        "name": "gen", "seed": seed, "agents": [{"provider": "scripted", "model": "scripted", "count": agents}],
        "tasks": {"generator": "market_generator", **tasks}, **extra})


def build(cfg: ExperimentConfig, base_dir: Path = ROOT):
    from hivemarket.game.generated import build_world
    return build_world(cfg, [a for a, _ in cfg.expand_agents()], base_dir)


async def broker_for(world, tmp_path) -> Broker:
    b = Broker(world, EventLog("market", "r", JsonlSink(tmp_path / "m.jsonl")), run_dir=tmp_path)
    await b.begin()
    return b


async def trade(b: Broker, buyer: str, token: str, seller: str) -> None:
    o = await b.make_offer(buyer, token, seller)
    await b.sell(seller, o["offer_id"])
    await b.buy(buyer, o["offer_id"])


async def submit_all(b: Broker) -> None:
    for n in b.solvers:
        await b.submit(n, "-".join(b.contents[t] for t in b.spec[n]["needs"]))


# ------------------------------------------------------------------ config


def test_generated_games_default_to_one_credit_messages():
    assert config().communication_cost == 1
    assert config().generated


@pytest.mark.parametrize("extra, error", [
    ({"communication_cost": 2}, "exactly 1 credit"),
    ({"market": {"token_transfer": "copy"}}, "token_transfer must be move"),
    ({"tasks": {"generator": "market_generator", "impossible": [{"mode": "blocked", "count": 1}]}}, "can't be expressed"),
    ({"tasks": {"generator": "market_generator", "family": "robust", "file": "x.json"}}, "at most one"),
    ({"tasks": {"generator": "market_generator", "edge_cost": 3}}, "only apply to generator: secret_assembly"),
    ({"tasks": {"generator": "secret_assembly", "market": {"n_tokens": 3}}}, "only apply to generator: market_generator"),
    ({"graph": {"nodes": {"a": {"credits": 1, "needs": ["T"]}, "b": {"credits": 1, "holds": ["T"]},
                          "c": {"credits": 1}}}}, "exclusive"),
])
def test_invalid_generated_configs_are_rejected(extra, error):
    with pytest.raises(ValidationError, match=error):
        config(**extra)


@needs_generator
@pytest.mark.parametrize("tasks, extra, error", [
    ({"market": {"n_tokens": 4, "colour": "red"}}, {}, "unknown tasks.market field"),
    ({"market": {"n_agents": 3}}, {}, "taken from the experiment"),
    ({"family": "goal_relevant_fragile"}, {}, "has 2 agents but the experiment has 3"),
    (None, {"market": {"execution_fee": {"buyer": 2}}}, "doesn't match the generated market"),
])
def test_generated_markets_are_checked_against_the_generator(tasks, extra, error):
    with pytest.raises(ValueError, match=error):
        build(config(tasks, **extra))


# --------------------------------------------------------------- conversion


@needs_generator
def test_a_generator_export_converts_to_our_graph():
    """The generator's examples/markets/medium.json (its "fragile" family), replayed from a file."""
    world, export = build(config({"file": "data/market_generator_medium.json"}), base_dir=TESTS)
    names = ["agent-1", "agent-2", "agent-3"]
    assert world["nodes"] == {"agent-1": {"credits": 0, "holds": ["T1"], "needs": []},
                              "agent-2": {"credits": 3, "holds": [], "needs": ["T1"]},
                              "agent-3": {"credits": 0, "holds": ["T2"], "needs": []}}
    # complete graph: edge seller -> buyer at P[buyer][seller] (all 2 here)
    assert sorted((e["from"], e["to"], e["cost"]) for e in world["edges"]) == \
        [(s, d, 2) for s in names for d in names if s != d]
    # the market charges buyers 1 execution message per sale: the offer tax (1) covers it, so no extra fee
    assert world["rules"]["execution_fee"] == {"buyer": 0, "seller": 0} and world["rules"]["offer_tax"] == 1
    assert world["rules"]["token_transfer"] == "move" and world["communication_cost"] == 1
    ideal = world["ideal"]
    assert (ideal["source"], ideal["ideal_status"], ideal["ideal_credits"]) == ("market_generator", "optimal", 3)
    assert (ideal["ideal_transfers"], ideal["ideal_execution_messages"], ideal["ideal_gross_payments"]) == (1, 1, 2)
    assert ideal["schedule"] == [{"buyer": "agent-2", "seller": "agent-1", "token": "T1", "price": 2}]
    assert ideal["graph_ideal"]["ideal_credits"] == 3  # our model agrees here: one direct purchase
    assert (world["generated"]["agents"], world["generated"]["tokens"]) == (names, ["T1", "T2"])
    original = json.loads((TESTS / "data" / "market_generator_medium.json").read_text())["data"]
    assert json.loads(export)["data"]["market"] == original["market"]


@needs_generator
def test_prices_become_edges_seller_to_buyer():
    world, _ = build(config())
    agents, prices = world["generated"]["agents"], world["generated"]["market"]["prices"]
    assert len(world["edges"]) == 3 * 2
    for e in world["edges"]:  # P[buyer][seller] on the edge seller -> buyer (P is asymmetric)
        assert e["cost"] == prices[agents.index(e["to"])][agents.index(e["from"])]


@needs_generator
@pytest.mark.parametrize("name, source, status", [
    ("generated_easy", ("preset", "easy"), "optimal"),
    ("generated_medium", ("preset", "medium"), "limit_reached"),
    ("generated_hard", ("preset", "hard"), "limit_reached"),
    ("generated_fragile", ("family", "goal_relevant_fragile"), "optimal"),
])
def test_shipped_experiments_build(name, source, status):
    cfg = ExperimentConfig.load(ROOT / "experiments" / f"{name}.yaml")
    world, _ = build(cfg)
    assert world["generated"]["source"][source[0]] == source[1]
    assert world["ideal"]["ideal_status"] == status and world["ideal"]["ideal_credits"] > 0


@needs_generator
def test_same_seed_same_market_and_ideal():
    first, second, other = build(config(seed=7)), build(config(seed=7)), build(config(seed=8))
    assert first == second
    assert first[0]["generated"]["market"] != other[0]["generated"]["market"]


# ------------------------------------------------------------ in the broker


@needs_generator
async def test_reference_schedule_replays_through_the_broker(tmp_path):
    """The generator's reference solution passes the round, spends exactly gross payments + fees, and reconciles."""
    from hivemarket_generator import decode_result, loads

    world, export = build(config())
    reference = decode_result(loads(export)).reference_solution
    names, tokens = world["generated"]["agents"], world["generated"]["tokens"]
    b = await broker_for(world, tmp_path)
    for a in reference.actions:
        await trade(b, names[a.buyer], tokens[a.token], names[a.seller])
    await submit_all(b)

    res = b.results[0]
    messages, transfers, gross = reference.objective
    assert res["outcome"] == "pass"
    assert res["credits_spent"] == gross + messages
    assert res["objective"] == [messages, transfers, gross]  # the generator's own objective, exactly
    assert res["credits_check"]["consistent"]
    assert res["economic_validation"] == {"status": "valid", "transfers": transfers, "messages": 0}
    saved = json.loads((tmp_path / "rounds" / "round-1.json").read_text())
    assert [x["kind"] for x in saved["ledger"]].count("transfer") == transfers
    assert saved["end_state"]["credits"] == res["credits_remaining"]


@needs_generator
async def test_the_ideal_is_the_generators_optimum_which_a_plan_with_more_trades_can_beat(tmp_path):
    """The generator minimises trades first, then prices. At seed 2 its optimum costs 35 credits; the
    reference schedule makes one more trade, costs 32, and still passes and reconciles."""
    from hivemarket_generator import decode_result, loads

    world, export = build(config(seed=2))
    result = decode_result(loads(export))
    messages, transfers, gross = result.optimal_solution.objective
    assert world["ideal"]["ideal_credits"] == gross + messages == 35
    assert world["ideal"]["ideal_transfers"] == transfers
    names, tokens = world["generated"]["agents"], world["generated"]["tokens"]
    b = await broker_for(world, tmp_path)
    for a in result.reference_solution.actions:
        await trade(b, names[a.buyer], tokens[a.token], names[a.seller])
    await submit_all(b)
    res = b.results[0]
    assert (res["outcome"], res["credits_spent"], res["excess_over_ideal"]) == ("pass", 32, -3)
    assert res["economic_validation"]["status"] == "valid"


@needs_generator
async def test_agents_are_told_when_the_ideal_is_not_proven(tmp_path):
    world, _ = build(ExperimentConfig.load(ROOT / "experiments" / "generated_medium.yaml"))
    b = await broker_for(world, tmp_path)
    await b.end_round("fail", "test")
    notice = next(n["body"] for n in b.feed if n.get("event") == "round_end")
    assert f"{world['ideal']['ideal_credits']} credits (the best plan found" in notice and "not proven optimal" in notice


@needs_generator
async def test_messages_are_reconciled_and_a_tampered_ledger_is_not(tmp_path):
    from hivemarket.game.generated import reconcile

    # seed 1: agent-1 (10 credits) needs T1, held by agent-2, who sells it to agent-1 for 7
    world, _ = build(config(market={"messages": True}))
    assert world["nodes"]["agent-2"]["holds"][0] == "T1" and "T1" in world["nodes"]["agent-1"]["needs"]
    b = await broker_for(world, tmp_path)
    await b.send_message("agent-3", "*", "who holds T1?")  # one 1-credit exploratory message per recipient
    unsold = await b.make_offer("agent-1", "T1", "agent-3")  # never sold: an exploratory message too
    await b.withdraw_offer("agent-1", unsold["offer_id"])
    await trade(b, "agent-1", "T1", "agent-2")  # this offer is the sale's buyer execution message
    res = await b.end_round("fail", "test")
    assert res["economic_validation"] == {"status": "valid", "transfers": 1, "messages": 3}

    saved = json.loads((tmp_path / "rounds" / "round-1.json").read_text())
    overcharged = [dict(x, price=x["price"] + 1) if x["kind"] == "transfer" else x for x in saved["ledger"]]
    assert reconcile(world["generated"], overcharged, saved["end_state"])["status"] == "invalid"
    missing_message = saved["ledger"][1:]  # drop the broadcast
    assert "balances" in reconcile(world["generated"], missing_message, saved["end_state"])["error"]
    # a ledger in an older format (sales without offer ids) is reported, never raised
    old_format = [{k: v for k, v in x.items() if k != "offer_id"} for x in saved["ledger"] if x["kind"] != "offer"]
    assert reconcile(world["generated"], old_format, saved["end_state"])["status"] == "invalid"


@needs_generator
@pytest.mark.parametrize("tax", [0, 1, 2, 3])
async def test_any_offer_tax_plays_the_generated_market_and_reconciles(tmp_path, tax):
    """The offer tax is the main experiment's message cost. Below the market's buyer messages (1) the
    execution fee makes up the rest; above them the market charges buyers the tax and is re-solved."""
    world, _ = build(config(market={"offer_tax": tax}))
    assert world["rules"]["offer_tax"] == tax
    assert world["rules"]["execution_fee"] == {"buyer": max(0, 1 - tax), "seller": 0}
    assert world["generated"]["market"]["communication"] == {"buyer": max(1, tax), "seller": 0}
    assert (world["generated"]["offer_tax"] is None) == (tax <= 1)
    ideal = world["ideal"]
    assert ideal["solvable"] and ideal["ideal_status"] == "optimal"

    b = await broker_for(world, tmp_path)
    for x in ideal["schedule"]:
        await trade(b, x["buyer"], x["token"], x["seller"])
    await submit_all(b)
    res = b.results[0]
    assert res["outcome"] == "pass" and res["credits_spent"] == ideal["ideal_credits"]
    assert res["economic_validation"] == {"status": "valid", "transfers": ideal["ideal_transfers"], "messages": 0}

    # an offer that never leads to a sale is `tax` exploratory messages
    b = await broker_for(world, tmp_path / "unsold")
    unsold = await b.make_offer("agent-1", "T1", "agent-3")
    await b.withdraw_offer("agent-1", unsold["offer_id"])
    res = await b.end_round("fail", "test")
    assert res["economic_validation"] == {"status": "valid", "transfers": 0, "messages": tax}


@needs_generator
def test_an_offer_tax_the_budgets_cant_cover_is_proven_infeasible():
    # medium.json: agent-2 has 3 credits and needs T1 at price 2, so a tax of 2 leaves it 1 short
    world, _ = build(config({"file": "data/market_generator_medium.json"}, market={"offer_tax": 2}), base_dir=TESTS)
    assert (world["ideal"]["solvable"], world["ideal"]["ideal_status"], world["ideal"]["ideal_credits"]) == \
        (False, "infeasible", None)
    assert world["generated"]["offer_tax"] | {"proof_scope": None} == {
        "offer_tax": 2, "generated_buyer_messages": 1, "conclusion": "infeasible", "solver_status": "infeasible",
        "proof_scope": None, "termination_reason": "exhausted"}


@needs_generator
async def test_goal_relevant_fragile_bad_and_good_first_purchase(tmp_path):
    cfg = config({"family": "goal_relevant_fragile", "fragility": True}, agents=2, seed=42)
    world, _ = build(cfg)
    # 1 buyer and 1 seller execution message per sale: the offer tax covers the buyer's, the fee the seller's
    assert world["rules"]["execution_fee"] == {"buyer": 0, "seller": 1}
    assert world["generated"]["fragility"]["goal_relevant_fragility"] == 0.5

    # Bad: agent-1 buys first and spends everything, so it can never pay the seller fee on the other trade.
    bad = await broker_for(world, tmp_path / "bad")
    await trade(bad, "agent-1", "T2", "agent-2")
    assert bad.credits["agent-1"] == 0
    offer = await bad.make_offer("agent-2", "T1", "agent-1")
    with pytest.raises(MarketError) as e:
        await bad.sell("agent-1", offer["offer_id"])
    assert e.value.code == "insufficient_credits"  # agent-2 can never get T1: the round can't be won

    # Good: agent-2 buys first; both trades go through and the round passes at the ideal.
    good = await broker_for(world, tmp_path / "good")
    await trade(good, "agent-2", "T1", "agent-1")
    await trade(good, "agent-1", "T2", "agent-2")
    await submit_all(good)
    res = good.results[0]
    assert res["outcome"] == "pass" and res["credits_spent"] == world["ideal"]["ideal_credits"] == 8
    assert res["excess_over_ideal"] == 0 and res["economic_validation"]["status"] == "valid"
    assert "8 credits (a proven-optimal plan" in next(n["body"] for n in good.feed if n.get("event") == "round_end")


# ------------------------------------------------------------ impossibility


@needs_generator
async def test_impossibility_is_proven_on_the_modified_market(tmp_path):
    world, _ = build(config({"market": {"n_tokens": 4},
                             "impossible": [{"mode": "unaffordable_strict", "agents": ["agent-1"]}]}))
    assert world["generated"]["impossibility"]["conclusion"] == "infeasible"
    assert world["impossible"] == {"agent-1": ["unaffordable_strict"]}
    assert (world["ideal"]["solvable"], world["ideal"]["ideal_credits"], world["ideal"]["ideal_status"]) == \
        (False, None, "infeasible")
    economy = sum(n["credits"] for n in world["nodes"].values())
    assert {e["cost"] for e in world["edges"] if e["to"] == "agent-1"} == {economy + 1}
    b = await broker_for(world, tmp_path)
    await b.declare_stuck("agent-1", "everything costs too much")
    assert "not solvable as configured" in next(n["body"] for n in b.feed if n.get("event") == "round_end")


@needs_generator
def test_unaffordable_cuts_the_budget_and_records_the_solver_conclusion():
    world, _ = build(config({"market": {"n_tokens": 4},
                             "impossible": [{"mode": "unaffordable", "agents": ["agent-1"]}]}))
    assert world["nodes"]["agent-1"]["credits"] == 0
    # soft: agent-1 can still earn by selling, and the solver proves the market still solvable
    assert world["generated"]["impossibility"]["conclusion"] == "optimal"
    assert (world["ideal"]["ideal_status"], world["ideal"]["solvable"], world["ideal"]["ideal_credits"]) == \
        ("optimal", True, 35)


# ---------------------------------------------------------- what agents see


@needs_generator
@pytest.mark.parametrize("visibility", ["neighbors", "full"])
async def test_generator_data_never_reaches_agents(tmp_path, visibility):
    import yaml

    from hivemarket.orchestrator.run import create_run

    exp = tmp_path / "exp.yaml"
    exp.write_text(yaml.safe_dump({"name": "gen", "seed": 1,
                                   "agents": [{"provider": "scripted", "model": "scripted", "count": 3}],
                                   "tasks": {"generator": "market_generator", "market": {"n_tokens": 4},
                                             "fragility": True},
                                   "market": {"graph_visibility": visibility}}))
    run_dir = create_run(exp, tmp_path / "runs", mode="local")
    world = json.loads((run_dir / "world.json").read_text())
    assert (run_dir / "generator.json").exists() and world["generated"]["fragility"]
    b = await broker_for(world, tmp_path)
    for agent in b.agents:
        status = b.status(agent)
        runtime = AgentRuntimeConfig.model_validate_json((run_dir / "agents" / f"{agent}.json").read_text())
        prompt = render_system_prompt(runtime, status)
        seen = json.dumps([status, b.graph_view(agent)]) + prompt + kickoff_text(status) + runtime.model_dump_json()
        assert not [s for s in GENERATOR_ONLY if s in seen], agent
        # the offer tax plays the generator's buyer message, so there is no extra fee and no fee note
        assert status["execution_fee"] == {"buyer": 0, "seller": 0} and status["offer_tax"] == 1
        assert "execution fee" not in prompt


# --------------------------------------------------------------- end to end


@needs_generator
async def test_local_generated_run_reports_difficulty_and_reconciles_on_the_host(tmp_path, monkeypatch):
    """The market process can't reconcile without the generator (as in Docker); the report does it on the host."""
    import hivemarket.market.state as state
    from hivemarket.orchestrator.local import run_local
    from hivemarket.orchestrator.report import write_report
    from hivemarket.orchestrator.run import create_run

    monkeypatch.setattr(state, "reconcile", lambda *a, **k: {"status": "unavailable", "reason": "no generator here"})
    run_dir = create_run(ROOT / "experiments" / "local_generated.yaml", tmp_path, mode="local", local_base_port=19950)
    await run_local(run_dir)
    report = write_report(run_dir).read_text()
    rounds = json.loads((run_dir / "summary.json").read_text())["rounds"]
    # offers that never led to a sale are exploratory messages; every round still reconciles
    assert rounds and all(r["economic_validation"]["status"] == "valid" and r["economic_validation"]["checked_by"]
                          == "report" and r["economic_validation"]["transfers"] == len(r["exchanges"]) for r in rounds)
    assert "## Market difficulty" in report and "proven optimal for its objective" in report and "| valid |" in report
    assert "HiveMarket graph ideal: " in report  # both ideals, labelled


@needs_generator
def test_random_impossibility_targets_only_agents_that_must_buy():
    """At seed 2 agent-1's task is covered by its own tokens; a random target must be someone who has to buy."""
    world, _ = build(config({"market": {"n_tokens": 4}, "impossible": [{"mode": "unaffordable_strict", "count": 1}]},
                            seed=2))
    [target] = world["impossible"]
    node = world["nodes"][target]
    assert set(node["needs"]) - set(node["holds"])


@needs_generator
async def test_an_unexpected_reconciliation_error_never_stops_the_market(tmp_path, monkeypatch):
    import hivemarket_generator

    def broken(*args):
        raise TypeError("validate_economic_state() changed signature")

    monkeypatch.setattr(hivemarket_generator, "validate_economic_state", broken)
    world, _ = build(config())
    b = await broker_for(world, tmp_path)
    res = await b.end_round("fail", "test")
    assert res["economic_validation"]["status"] == "error" and "TypeError" in res["economic_validation"]["error"]
    assert b.phase == "over"


@needs_generator
def test_leeway_levels_play_the_same_market_and_are_logged():
    """The fingerprint covers prices, holders and tasks but not budgets, so levels of one market share it."""
    levels = {lw: build(config({"market": {"preset": "medium", "budget_leeway": lw}}, agents=6, seed=3))[0]["generated"]
              for lw in (0.0, 0.5)}
    low, high = levels[0.0], levels[0.5]
    assert low["budget_leeway"] == 0.0 and high["budget_leeway"] == 0.5 and low["leeway_mode"] == "relative"
    assert low["fingerprint"] == high["fingerprint"] and len(low["fingerprint"]) == 64
    assert low["market"]["prices"] == high["market"]["prices"]
    assert low["reference_budgets"] == high["reference_budgets"]
    assert sum(high["initial_balances"]) > sum(low["initial_balances"])
    other = build(config({"market": {"preset": "medium"}}, agents=6, seed=4))[0]["generated"]
    assert other["fingerprint"] != low["fingerprint"]


@needs_generator
@pytest.mark.parametrize("name", ["session_main", "session_exploratory"])
def test_the_session_arms_build_one_round_without_declare_stuck(name):
    cfg = ExperimentConfig.load(ROOT / "experiments" / f"{name}.yaml")
    world, _ = build(cfg)
    assert world["rounds"]["count"] == 1 and world["rules"]["declare_stuck"] is False
    assert world["rounds"]["end_on_deadlock"] is False and world["rounds"]["announce_ideal"] is False
    assert world["rules"]["messages"] is False and cfg.affordance.disclose_rule is False
    assert world["generated"]["budget_leeway"] == 0.3
    assert cfg.memory.enabled is (name == "session_exploratory")
