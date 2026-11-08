"""Markets from n-body-lab/market_generator as HiveMarket games.

The generator (V0.3, pinned at COMMIT) makes solvable markets with controllable, measured
difficulty and a verified (for small markets, proven optimal) reference solution. Its model
maps onto ours:

  generator                          HiveMarket
  agent i                            agent-{i+1}
  token t                            T{t+1}
  P[buyer][seller], every pair       edge seller -> buyer at that cost (a complete graph)
  initial_owners[t]                  that agent's holds; tokens are unique (token_transfer: move)
  task (unordered set)               needs, in token-id order. Agents still submit it with
                                     submit_answer (our off-channel knowledge check), which costs
                                     nothing, so the benchmark is unchanged
  execution messages (buyer, seller) per sale: the offer tax (market.offer_tax 1-credit messages from
                                     the buyer) plus market.execution_fee = {buyer: buyer messages -
                                     offer_tax, seller: seller messages}, so a sale costs exactly what the
                                     generator charges. An offer tax above the market's buyer messages
                                     raises them to the tax (see played_market) and the ideal is re-solved
  exploratory message (1 credit)     each credit of an offer's tax when the offer never leads to a sale,
                                     or a free-text message per recipient when market.messages is on
                                     (communication_cost: 1)
  initial balances                   credits: the reference budget plus leeway, raised to at least
                                     tasks.market.minimum_initial_credits (the credit floor, applied after
                                     leeway; 0 = off). Without a floor a balance can be 0 (such an agent can
                                     sell, unless sellers pay a fee, but not buy or offer)
  communication_protocol             `offer_buy` (the default): one 1-credit OFFER per sale, BUY and SELL
                                     untaxed, i.e. execution messages (1, 0). `legacy_fixed_messages` for
                                     other counts (e.g. the goal_relevant_fragile family's seller message,
                                     or an offer tax above 1); the protocol is always passed explicitly

Everything except `reconcile` runs on the host when a run is created. The generator needs
Python >= 3.11 and is an optional extra installed from git (`pip install -e ".[generator]"`),
imported lazily. The broker calls `reconcile` at every round end; where the generator isn't
installed (the Docker market image) that reports `unavailable` and `hivemarket report` completes
it on the host from rounds/round-N.json.

The generator's data (difficulty, solutions, proofs) is observer-only: world.json and
generator.json stay in the run directory, which agent containers never mount.
"""

from __future__ import annotations

import dataclasses
import random
from pathlib import Path
from typing import Any

from hivemarket.common.config import ExperimentConfig, GraphEdge, GraphNode, GraphSpec
from hivemarket.game.graph import graph_ideal, graph_to_world

PACKAGE = "hivemarket-generator"
COMMIT = "b69351d8ee371c057514f675e26cad68485e66d7"
SOURCE = "market_generator"
# The generator's realised structural properties, by the groups its docs use (docs/controllability.md).
PROPERTY_GROUPS = {
    "routing": ("indirect_dependency_fraction", "mean_dependency_depth"),
    "interdependence": ("shared_demand_fraction", "ownership_hhi", "shared_intermediaries"),
    "resources": ("limited_slack_fraction",),
    "alternatives": ("competitive_alternative_fraction",),
}
FRAGILITY_KEYS = ("goal_relevant_fragility", "goal_relevant_fragility_status",
                  "full_affordable_action_fragility", "fragility_status")
IDEAL_ASSUMPTIONS = (
    "market_generator's best joint schedule within every agent's budget, minimising (execution messages, "
    "transfers, gross payments) lexicographically: fewest trades first, then lowest prices. credits = gross "
    "payments + execution messages (offer taxes + execution fees), so a schedule with more trades can cost fewer credits (negative excess). "
    "Discovery messages (asking a node to relay) are not counted. `optimal` is proven for that objective; "
    "`feasible` and `limit_reached` are the best schedule found, an upper bound on it.")


def generator():
    """The market_generator package, or a clear error saying how to install it."""
    try:
        import hivemarket_generator
    except ImportError as e:
        raise RuntimeError(f'market_generator games need the generator package: pip install -e ".[generator]" '
                           f"(Python >= 3.11; {PACKAGE} at {COMMIT[:7]})") from e
    return hivemarket_generator


def token_name(t: int) -> str:
    return f"T{t + 1}"


# ------------------------------------------------------------------ building

def build_world(cfg: ExperimentConfig, agent_ids: list[str], base_dir: Path) -> tuple[dict[str, Any], str]:
    """world.json for a generated game, and the generator's own export (written as generator.json).

    `base_dir` is the experiment file's directory; `tasks.file` is resolved against it.
    """
    g = generator()
    result = _generate(cfg, len(agent_ids), base_dir)
    if cfg.tasks.fragility:
        analysis = g.analyse_fragility(result.market, result.initial_state)
        result = dataclasses.replace(result, difficulty=(result.difficulty or {}) | {"decision_analysis": analysis})
    market, balances = result.market, result.initial_state.balances
    solution, status = _benchmark(result)
    impossible: dict[str, list[str]] = {}
    impossibility = offer_tax = None
    if cfg.tasks.impossible:
        market, balances, impossible = _make_impossible(cfg, agent_ids, market, balances)
    generated_buyer_messages = market.communication.buyer
    market = played_market(market, cfg.market.offer_tax)
    raised = market.communication.buyer != generated_buyer_messages
    if cfg.tasks.impossible or raised:  # the market changed after generation: prove its ideal again
        initial = g.initial_state(market, balances)
        proof = g.solve_exact(market, initial, state_limit=result.config.exact_state_limit or 20000,
                              incumbent=_still_valid(market, initial, solution))
        solution, status = proof.best_solution, _proof_status(proof.conclusion)
        conclusion = {"conclusion": proof.conclusion, "solver_status": proof.status,
                      "proof_scope": proof.proof_scope, "termination_reason": proof.termination_reason}
        if cfg.tasks.impossible:
            impossibility = {"modes": impossible, **conclusion}
        if raised:
            offer_tax = {"offer_tax": cfg.market.offer_tax, "generated_buyer_messages": generated_buyer_messages,
                         **conclusion}

    fee = execution_fee(market, cfg.market.offer_tax)
    if "execution_fee" in cfg.market.model_fields_set and cfg.market.execution_fee.model_dump() != fee:
        raise ValueError(f"market.execution_fee {cfg.market.execution_fee.model_dump()} doesn't match the generated "
                         f"market's {fee}; leave it out to use the market's")
    graph = to_graph(market, balances, agent_ids)
    ideal = generator_ideal(market, solution, status, agent_ids) | {"graph_ideal": graph_ideal(graph, cfg, fee)}
    world = graph_to_world(graph, cfg, impossible, execution_fee=fee, ideal=ideal)
    world["generated"] = _summary(g, cfg, result, market, balances, agent_ids, impossibility)
    # Set when the offer tax raised the market's buyer messages: `market` above is the market as played.
    world["generated"]["offer_tax"] = offer_tax
    return world, g.dumps(result)


def _generate(cfg: ExperimentConfig, n_agents: int, base_dir: Path):
    g = generator()
    t = cfg.tasks
    if t.file:
        result = g.decode_result(g.loads((base_dir / t.file).read_text()))
    elif t.family:
        result = g.economic_family(t.family, seed=cfg.seed)
    else:
        result = g.generate_market(market_config(t.market or {}, n_agents, cfg.seed))
    if result.market.n_agents != n_agents:
        raise ValueError(f"the generated market has {result.market.n_agents} agents but the experiment has "
                         f"{n_agents}; make the agents' counts add up to {result.market.n_agents}")
    return result


def market_config(spec: dict[str, Any], n_agents: int, seed: int):
    """The generator's MarketConfig from `tasks.market`; unknown fields are rejected, as everywhere else."""
    g = generator()
    spec = dict(spec)
    preset = spec.pop("preset", None)
    if fixed := sorted({"n_agents", "seed"} & set(spec)):
        raise ValueError(f"tasks.market.{', '.join(fixed)}: taken from the experiment (agents, seed); remove it")
    known = {f.name for f in dataclasses.fields(g.MarketConfig)}
    if unknown := sorted(set(spec) - known):
        raise ValueError(f"unknown tasks.market field(s) {', '.join(unknown)}; "
                         f"MarketConfig has preset, {', '.join(sorted(known - {'n_agents', 'seed'}))}")
    kwargs = {k: _tuples(v) for k, v in spec.items()} | {"n_agents": n_agents, "seed": seed}
    return g.MarketConfig.preset(preset, **kwargs) if preset else g.MarketConfig(**kwargs)


def _tuples(value: Any) -> Any:
    """YAML gives lists; the generator's ranges and constraint intervals are tuples."""
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, dict):
        return {k: _tuples(v) for k, v in value.items()}
    return value


def _benchmark(result) -> tuple[Any, str]:
    """The generator's ideal schedule and how sure it is: optimal (proven), limit_reached or feasible."""
    if result.optimal_solution is not None:
        return result.optimal_solution, "optimal"
    status = "limit_reached" if result.optimality_status == "limit_reached" else "feasible"
    return result.best_solution or result.reference_solution, status


def _proof_status(conclusion: str) -> str:
    return {"optimal": "optimal", "infeasible": "infeasible", "feasible": "limit_reached"}.get(conclusion, "unknown")


def _still_valid(market, initial, solution):
    """The generated market's schedule, if it still works once impossibility is applied (a start for the proof)."""
    from hivemarket_generator.scheduling import verify_solution
    try:
        verify_solution(market, initial, solution)
    except ValueError:
        return None
    return solution


def _make_impossible(cfg: ExperimentConfig, agent_ids: list[str], market, balances: tuple[int, ...]):
    """Apply `tasks.impossible` after generation (the generator itself only makes solvable markets).

    unaffordable         the agent's budget is cut to 0 (soft: it can still earn by selling, unless
                         sellers pay an execution fee)
    unaffordable_strict  every purchase by the agent costs more than all credits in the economy
    Random targets (`count`) are drawn from agents that have to buy something: an agent whose task
    is covered by its own tokens isn't affected by either.
    """
    g = generator()
    rng = random.Random(cfg.seed)
    n = market.n_agents
    prices = [list(row) for row in market.prices]
    credits = list(balances)
    economy = sum(balances)
    buyers = [a for i, a in enumerate(agent_ids) if any(market.initial_owners[t] != i for t in market.tasks[i])]
    modes: dict[str, list[str]] = {}
    for spec in cfg.tasks.impossible:
        targets = spec.agents or rng.sample(buyers, min(spec.count, len(buyers)))
        for name in targets:
            if name not in agent_ids:
                raise ValueError(f"tasks.impossible: unknown agent {name!r}")
            i = agent_ids.index(name)
            if spec.mode == "unaffordable":
                credits[i] = 0
            else:
                prices[i] = [0 if j == i else economy + 1 for j in range(n)]
            modes.setdefault(name, []).append(spec.mode)
    changed = g.Market(tuple(map(tuple, prices)), market.tasks, market.initial_owners, market.communication,
                       communication_protocol=market.communication_protocol)
    return changed, tuple(credits), modes


def to_graph(market, balances: tuple[int, ...], names: list[str]) -> GraphSpec:
    """The complete graph: an edge seller -> buyer at P[buyer][seller] for every pair."""
    n = market.n_agents
    nodes = {name: GraphNode(credits=balances[i],
                             holds=[token_name(t) for t, owner in enumerate(market.initial_owners) if owner == i],
                             needs=[token_name(t) for t in sorted(market.tasks[i])])
             for i, name in enumerate(names)}
    edges = [GraphEdge(source=names[s], to=names[b], cost=market.prices[b][s])
             for b in range(n) for s in range(n) if s != b]
    return GraphSpec(nodes=nodes, edges=edges)


def played_market(market, offer_tax: int):
    """The market as HiveMarket plays it at `offer_tax`.

    Every sale needs an offer, whose tax is `offer_tax` 1-credit buyer messages, so a sale can't cost the
    buyer fewer messages than that. A tax above the market's buyer execution messages raises them to the
    tax; a lower one is made up by the buyer's execution fee. The market is otherwise unchanged.
    """
    if market.communication.buyer >= offer_tax:
        return market
    g = generator()
    from hivemarket_generator.models import ExecutionMessages
    return g.Market(market.prices, market.tasks, market.initial_owners,
                    ExecutionMessages(offer_tax, market.communication.seller),
                    communication_protocol="legacy_fixed_messages")  # offer_buy is exactly (1, 0)


def execution_fee(market, offer_tax: int) -> dict[str, int]:
    """Our execution fee for a market as played (`played_market`): the generator's per-sale messages minus
    the offer tax, which already charges the buyer's first ones (every sale needs an offer)."""
    return {"buyer": max(0, market.communication.buyer - offer_tax), "seller": market.communication.seller}


def generator_ideal(market, solution, status: str, names: list[str]) -> dict[str, Any]:
    """The round's ideal for a generated game, from the generator's (optimal or best) schedule."""
    base = {"source": SOURCE, "ideal_status": status, "budget_shortfalls": {}, "unreachable": [],
            "assumptions": IDEAL_ASSUMPTIONS}
    if solution is None:  # proven infeasible, or nothing found within the search limit
        return base | {"solvable": False if status == "infeasible" else None, "ideal_credits": None}
    messages, transfers, gross = solution.objective
    schedule = [{"buyer": names[a.buyer], "seller": names[a.seller], "token": token_name(a.token),
                 "price": market.prices[a.buyer][a.seller]} for a in solution.actions]
    return base | {"solvable": True, "ideal_credits": gross + messages,  # every execution message is 1 credit
                   "ideal_transfers": transfers, "ideal_execution_messages": messages,
                   "ideal_gross_payments": gross, "schedule": schedule}


def _summary(g, cfg: ExperimentConfig, result, market, balances: tuple[int, ...], names: list[str],
             impossibility: dict[str, Any] | None) -> dict[str, Any]:
    """world["generated"]: what the broker needs to reconcile rounds, plus difficulty for analysis."""
    meta = result.metadata or {}
    realised = meta.get("realised_properties") or {}
    analysis = (result.difficulty or {}).get("decision_analysis")
    t = cfg.tasks
    return {
        "package": PACKAGE, "commit": COMMIT, "version": meta.get("package_version"),
        "source": {"preset": (t.market or {}).get("preset"), "family": t.family, "file": t.file},
        "seed": result.seed, "n_agents": market.n_agents, "n_tokens": market.n_tokens,
        "agents": names, "tokens": [token_name(i) for i in range(market.n_tokens)],
        "market": g.loads(g.dumps(market)),
        "initial_balances": list(balances),
        "reference_budgets": list(result.reference_budgets),
        "status": result.status, "optimality_status": result.optimality_status,
        "generation_attempts": meta.get("generation_attempts"),
        "constraints_satisfied": meta.get("constraints_satisfied"),
        "properties": {group: {k: realised[k] for k in keys if k in realised}
                       for group, keys in PROPERTY_GROUPS.items()},
        "fragility": {k: analysis.get(k) for k in FRAGILITY_KEYS} if analysis else None,
        "impossibility": impossibility,
        "communication_protocol": market.communication_protocol,
        # reference budgets, credits before and after the floor, agents the floor raised, zero-credit agents
        "credit_allocation": meta.get("credit_allocation"),
    }


# ------------------------------------------------------------- reconciling

def reconcile(generated: dict[str, Any], ledger: list[dict[str, Any]], end_state: dict[str, Any]) -> dict[str, Any]:
    """Check one round against the generator's economics ("Economic validation", V0.3).

    `ledger` is the broker's ordered record of the round: offers (tax paid), sales (price and fees
    charged) and messages. In the generator's terms an offer that led to a sale is that sale's buyer
    execution message (its tax is charged with the sale); any other offer, and each recipient of a
    message, is a 1-credit exploratory message. validate_economic_state replays it from the trusted
    initial state and requires the broker's final balances, owners and progress (`end_state`:
    credits, holdings, known) to match exactly. Charging a sale's offer tax at the sale rather than
    when the offer was made only ever leaves the replay with more credits at earlier steps, so
    anything the broker allowed stays affordable in the replay.
    """
    try:
        g = generator()
    except RuntimeError as e:
        return {"status": "unavailable", "reason": str(e)}

    agents, tokens = generated["agents"], generated["tokens"]
    agent, token = {n: i for i, n in enumerate(agents)}, {n: t for t, n in enumerate(tokens)}
    counts: dict[str, int] = {}
    try:
        from hivemarket_generator.models import CommunicationEvent, Event, State, Transfer

        sold = {x["offer_id"] for x in ledger if x["kind"] == "transfer"}
        tax = {x["offer_id"]: x["tax"] for x in ledger if x["kind"] == "offer"}
        counts = {"transfers": len(sold),
                  "messages": sum(x["tax"] for x in ledger if x["kind"] == "offer" and x["offer_id"] not in sold)
                  + sum(len(x["recipients"]) for x in ledger if x["kind"] == "message")}

        market = g.decode_market(generated["market"])
        initial = g.initial_state(market, tuple(generated["initial_balances"]))
        records: list[Any] = []
        transfers: list[Any] = []
        histories = [list(h) for h in initial.histories]
        for x in ledger:
            if x["kind"] == "message":
                records += [CommunicationEvent(agent[x["sender"]], agent[r]) for r in x["recipients"]]
                continue
            if x["kind"] == "offer":  # charged with its sale, or else an exploratory message per credit of tax
                if x["offer_id"] not in sold:
                    records += [CommunicationEvent(agent[x["buyer"]], agent[x["seller"]])] * x["tax"]
                continue
            t, buyer = token[x["token"]], agent[x["buyer"]]
            event = Event(len(transfers), Transfer(t, agent[x["seller"]], buyer), x["price"],
                          tax[x["offer_id"]] + x["buyer_fee"], x["seller_fee"])
            transfers.append(event)
            records.append(event)
            histories[t].append(buyer)
        holders = [[agent[n] for n in agents if name in end_state["holdings"][n]] for name in tokens]
        final = State(
            owners=tuple(h[0] if len(h) == 1 else -1 for h in holders),
            balances=tuple(end_state["credits"][n] for n in agents),
            progress=tuple(frozenset(token[k] for k in end_state["known"][n] if token[k] in market.tasks[i])
                           for i, n in enumerate(agents)),
            histories=tuple(map(tuple, histories)),
            events=tuple(transfers),
            execution_messages=sum(e.buyer_messages + e.seller_messages for e in transfers),
            exploratory_messages=counts["messages"],
        )
        g.validate_economic_state(market, initial, final, records)
    except (ValueError, KeyError) as e:  # the generator's verdict: the round doesn't reconcile
        return {"status": "invalid", "error": str(e), **counts}
    except Exception as e:  # e.g. a generator installed at another commit; a bookkeeping check never stops the market
        return {"status": "error", "error": f"{type(e).__name__}: {e}", **counts}
    return {"status": "valid", **counts}
