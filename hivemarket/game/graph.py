"""The game graph: building it, and the ideal (minimum) credits to solve it.

Ideal cost
----------
Tokens can be relayed: once a node holds a token it can sell it along any
outgoing edge. Tokens are routed independently, and the model depends on
`market.token_transfer`:

move (default)  A sale moves the token: the seller no longer holds it. Each
                holder's copy therefore travels as one walk (a chain of sales)
                and cannot fan out. The plan is the cheapest set of walks, at
                most one per holder, that together visit every needer. A walk
                may revisit nodes: a node that sold the token can buy it back
                to pass it on. One holder's walk is a Held-Karp DP over the
                needers it serves (shortest hops between consecutive stops);
                a subset DP splits the needers among holders (~3^k per holder
                for k needers of a token). If no set of walks covers every
                needer, the largest coverable set is planned (cheapest among
                equals) and the rest are reported unreachable.
copy            A sale copies the token, so one purchase can serve several
                needers downstream: a minimum directed Steiner tree rooted at
                the token's holders, solved exactly with the Dreyfus-Wagner DP
                (3^k * n).

Communication: a node that buys a token only to pass it on has to be asked,
costing one message. In copy mode that applies to nodes that neither hold nor
need the token. In move mode it applies to every intermediate hop of a walk,
including a needer (or former holder) that has to buy the token again to keep
it moving; the hop into a stop where a needer is served costs only the edge.

Execution fees (market.execution_fee) are burned on every exchange: they are
added to every edge for routing and charged to each side's budget.

Budgets are ignored, so `ideal` is a lower bound on what any successful round
can spend; `budget_shortfalls` and `unreachable` qualify it. Generated games
(market_generator) use the generator's budget-aware solution as the ideal
instead, and keep this one alongside as `graph_ideal` (hivemarket/game/generated.py).
"""

from __future__ import annotations

import heapq
import random
from itertools import combinations
from typing import Any, Literal

from hivemarket.common.config import ExperimentConfig, GraphEdge, GraphNode, GraphSpec

INF = float("inf")


# ------------------------------------------------------------------ building

def build_graph(cfg: ExperimentConfig, agent_ids: list[str]) -> tuple[GraphSpec, dict[str, list[str]]]:
    """Explicit `graph`, or one compiled from `secret_assembly`. Returns (graph, impossible-by-agent).

    market_generator games carry more than a graph; they are built by hivemarket.game.generated.
    """
    if cfg.graph:
        return cfg.graph, {}
    if cfg.generated:
        raise ValueError("market_generator games are built with hivemarket.game.generated.build_world")
    return _secret_assembly(cfg, agent_ids)


def _secret_assembly(cfg: ExperimentConfig, agent_ids: list[str]) -> tuple[GraphSpec, dict[str, list[str]]]:
    rng = random.Random(cfg.seed)
    t = cfg.tasks
    holds: dict[str, list[str]] = {a: [] for a in agent_ids}
    needs: dict[str, list[str]] = {a: [] for a in agent_ids}
    tokens: list[str] = []

    def new_token(owner: str | None) -> str:
        name = f"T{len(tokens) + 1}"
        tokens.append(name)
        if owner:
            holds[owner].append(name)
        return name

    for a in agent_ids:
        others = [x for x in agent_ids if x != a]
        k = t.pieces_per_task
        owners = rng.sample(others, k) if len(others) >= k else [rng.choice(others) for _ in range(k)]
        needs[a] = [new_token(o) for o in owners]
    for a in agent_ids:
        for _ in range(t.decoys_per_agent):
            new_token(a)
    # Shuffle names so token numbers don't reveal who needs what.
    shuffled = tokens[:]
    rng.shuffle(shuffled)
    rename = dict(zip(tokens, shuffled))
    holds = {a: sorted(rename[x] for x in v) for a, v in holds.items()}
    needs = {a: [rename[x] for x in v] for a, v in needs.items()}

    costs = {(a, b): t.edge_cost for a in agent_ids for b in agent_ids if a != b}
    economy = len(agent_ids) * t.starting_balance
    impossible: dict[str, list[str]] = {}
    for spec in t.impossible:
        targets = spec.agents or rng.sample(agent_ids, min(spec.count, len(agent_ids)))
        for a in targets:
            impossible.setdefault(a, []).append(spec.mode)
            holders = [h for h in agent_ids if needs[a] and needs[a][0] in holds[h]]
            if spec.mode == "unaffordable":
                for h in agent_ids:
                    if any(tok in holds[h] for tok in needs[a]):
                        costs[(h, a)] = t.starting_balance // max(1, len(needs[a])) + 1
            elif spec.mode == "unaffordable_strict":
                for (src, dst) in list(costs):
                    if dst == a:
                        costs[(src, dst)] = economy + 1
            elif spec.mode == "blocked":
                for h in holders:
                    costs.pop((h, a), None)
            elif spec.mode == "missing":
                for h in holders:
                    holds[h].remove(needs[a][0])

    graph = GraphSpec(
        nodes={a: GraphNode(credits=t.starting_balance, holds=holds[a], needs=needs[a]) for a in agent_ids},
        edges=[GraphEdge(source=s, to=d, cost=c) for (s, d), c in sorted(costs.items())],
    )
    return graph, impossible


def token_contents(graph: GraphSpec, seed: int, round_no: int) -> dict[str, str]:
    """Secret content of every token for one round (regenerated each round)."""
    rng = random.Random(f"{seed}:{round_no}")
    names = sorted({t for n in graph.nodes.values() for t in n.holds + n.needs})
    return {t: "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6)) for t in names}


# ---------------------------------------------------------------- ideal cost

def _dijkstra(adj: dict[str, list[tuple[str, float]]], src: str) -> tuple[dict[str, float], dict[str, str]]:
    dist, prev = {src: 0.0}, {}
    heap = [(0.0, src)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, INF):
            continue
        for v, w in adj.get(u, []):
            nd = d + w
            if nd < dist.get(v, INF):
                dist[v], prev[v] = nd, u
                heapq.heappush(heap, (nd, v))
    return dist, prev


def _path(prev: dict[str, str], src: str, dst: str) -> list[tuple[str, str]]:
    edges, v = [], dst
    while v != src:
        u = prev[v]
        edges.append((u, v))
        v = u
    return edges[::-1]


def _steiner(nodes: list[str], weight: dict[tuple[str, str], float], roots: set[str],
             terminals: list[str]) -> tuple[float, set[tuple[str, str]]]:
    """Min-cost arborescence from any root covering all terminals (Dreyfus-Wagner)."""
    adj: dict[str, list[tuple[str, float]]] = {}
    for (u, v), w in weight.items():
        adj.setdefault(u, []).append((v, w))
    sp = {u: _dijkstra(adj, u) for u in nodes}
    dist = lambda u, v: sp[u][0].get(v, INF)  # noqa: E731

    k = len(terminals)
    full = (1 << k) - 1
    dp: dict[tuple[int, str], float] = {}
    how: dict[tuple[int, str], tuple] = {}
    for i, t in enumerate(terminals):
        for v in nodes:
            dp[(1 << i, v)] = dist(v, t)
            how[(1 << i, v)] = ("path", t)
    for mask in range(1, full + 1):
        if mask & (mask - 1) == 0:
            continue
        split: dict[str, float] = {}
        split_how: dict[str, int] = {}
        for v in nodes:
            best, arg = INF, None
            sub = (mask - 1) & mask
            while sub:
                if sub < (mask ^ sub):  # each split once
                    c = dp[(sub, v)] + dp[(mask ^ sub, v)]
                    if c < best:
                        best, arg = c, sub
                sub = (sub - 1) & mask
            split[v], split_how[v] = best, arg
        for v in nodes:
            best, arg = INF, None
            for u in nodes:
                c = dist(v, u) + split[u]
                if c < best:
                    best, arg = c, u
            dp[(mask, v)] = best
            how[(mask, v)] = ("via", arg, split_how.get(arg))

    root = min(roots, key=lambda r: dp[(full, r)])
    cost = dp[(full, root)]
    if cost == INF:
        return INF, set()

    edges: set[tuple[str, str]] = set()

    def expand(mask: int, v: str) -> None:
        h = how[(mask, v)]
        if h[0] == "path":
            edges.update(_path(sp[v][1], v, h[1]))
            return
        _, u, sub = h
        if u != v:
            edges.update(_path(sp[v][1], v, u))
        expand(sub, u)
        expand(mask ^ sub, u)

    expand(full, root)
    return cost, edges


NO_SINGLE_CHAIN = "token can't pass through every needer in one chain"


def _move_walks(costs: dict[tuple[str, str], int], comm: int, holders: list[str],
                needers: list[str]) -> tuple[float, list[dict[str, Any]], list[str]]:
    """Cheapest walks, at most one per holder, that together visit as many needers as possible.

    A walk is a chain of sales h -> ... -> last stop. Every hop costs its edge; a hop into a node
    that only passes the token on (not a stop where a needer is served) also costs one message.
    Returns (cost, walks, covered needers); each walk is {"holder", "stops", "hops"} with hops as
    (u, v, is_relay) in order.
    """
    adj_relay: dict[str, list[tuple[str, float]]] = {}
    into: dict[str, list[tuple[str, int]]] = {}
    for (u, v), c in costs.items():
        adj_relay.setdefault(u, []).append((v, c + comm))
        into.setdefault(v, []).append((u, c))
    relay_sp: dict[str, tuple[dict[str, float], dict[str, str]]] = {}

    def hop(u: str, v: str) -> tuple[float, list[tuple[str, str, bool]]]:
        """Cheapest u -> v where v is a stop: relays on the way pay edge + message, the last hop only its edge."""
        if u not in relay_sp:
            relay_sp[u] = _dijkstra(adj_relay, u)
        dist, prev = relay_sp[u]
        best, via = INF, None
        for p, c in into.get(v, []):
            d = 0.0 if p == u else dist.get(p, INF)
            if d + c < best:
                best, via = d + c, p
        if via is None or best == INF:
            return INF, []
        lead = _path(prev, u, via) if via != u else []
        return best, [(a, b, True) for a, b in lead] + [(via, v, False)]

    k = len(needers)
    full = (1 << k) - 1
    legs = {(a, b): hop(a, b) for a in holders + needers for b in needers if a != b}

    def walks_from(h: str) -> tuple[list[float], Any]:
        """Held-Karp over the needers h's walk serves: dp[mask][i] = cheapest walk visiting mask, ending at i."""
        dp = [[INF] * k for _ in range(full + 1)]
        par: list[list[int | None]] = [[None] * k for _ in range(full + 1)]
        for i in range(k):
            dp[1 << i][i] = legs[(h, needers[i])][0]
        for mask in range(1, full + 1):
            for i in range(k):
                if not mask >> i & 1 or dp[mask][i] == INF:
                    continue
                for j in range(k):
                    if mask >> j & 1:
                        continue
                    c = dp[mask][i] + legs[(needers[i], needers[j])][0]
                    if c < dp[mask | 1 << j][j]:
                        dp[mask | 1 << j][j], par[mask | 1 << j][j] = c, i
        best = [min(dp[m]) if m else 0.0 for m in range(full + 1)]

        def rebuild(mask: int) -> dict[str, Any]:
            i = min(range(k), key=lambda x: dp[mask][x])
            order = []
            while i is not None:
                order.append(i)
                mask, i = mask ^ (1 << i), par[mask][i]
            stops = [needers[x] for x in reversed(order)]
            hops = []
            for a, b in zip([h] + stops, stops):
                hops += legs[(a, b)][1]
            return {"holder": h, "stops": stops, "hops": hops}

        return best, rebuild

    # Split the needers among holders, each holder's copy used for at most one walk.
    g = [INF] * (full + 1)
    g[0] = 0.0
    stages = []
    for h in holders:
        walk_cost, rebuild = walks_from(h)
        ng, choice = g[:], [0] * (full + 1)
        for mask in range(1, full + 1):
            sub = mask
            while sub:
                if walk_cost[sub] < INF and g[mask ^ sub] < INF and g[mask ^ sub] + walk_cost[sub] < ng[mask]:
                    ng[mask], choice[mask] = g[mask ^ sub] + walk_cost[sub], sub
                sub = (sub - 1) & mask
        stages.append((choice, rebuild))
        g = ng

    # Every needer if possible; otherwise the largest coverable set, cheapest among equals.
    target = min((m for m in range(full + 1) if g[m] < INF), key=lambda m: (-bin(m).count("1"), g[m]))
    walks, mask = [], target
    for choice, rebuild in reversed(stages):
        if choice[mask]:
            walks.append(rebuild(choice[mask]))
            mask ^= choice[mask]
    covered = [needers[i] for i in range(k) if target >> i & 1]
    return g[target], walks[::-1], covered


def ideal_cost(graph: GraphSpec, communication_cost: int = 0,
               token_transfer: Literal["move", "copy"] = "move", transfer_tax: int = 0,
               execution_fee: dict[str, int] | None = None) -> dict[str, Any]:
    """`communication_cost`: what asking a relay to pass a token on costs (one message).
    `transfer_tax`: what every transfer costs on top of its price (the offer tax: each purchase
    needs at least one offer). With offers, a relay is asked by the offer itself, so runs use
    communication_cost=0 and transfer_tax=offer_tax.
    `execution_fee` ({buyer, seller}): burned on every transfer too; it weighs on routing and on each
    side's budget. `exchanges` keep listing the edge price and the tax."""
    fee_b, fee_s = (execution_fee or {}).get("buyer", 0), (execution_fee or {}).get("seller", 0)
    nodes = list(graph.nodes)
    edge_price = graph.edge_costs()
    costs = {k: c + transfer_tax + fee_b + fee_s for k, c in edge_price.items()}  # what a transfer costs in all
    tokens = sorted({t for n in graph.nodes.values() for t in n.needs})
    per_token: dict[str, Any] = {}
    unreachable: list[dict[str, str]] = []
    total = 0
    for tok in tokens:
        holders = {n for n, spec in graph.nodes.items() if tok in spec.holds}
        needers = sorted(n for n, spec in graph.nodes.items() if tok in spec.needs and tok not in spec.holds)
        if not needers:
            continue
        if not holders:
            unreachable += [{"node": n, "token": tok, "reason": "no node holds it"} for n in needers]
            continue
        # Reachability first, so one unreachable needer doesn't hide the cost for the others.
        reach = set().union(*(_dijkstra({u: [(v, 0) for (a, v) in costs if a == u] for u in nodes}, h)[0] for h in holders))
        lost = [n for n in needers if n not in reach]
        unreachable += [{"node": n, "token": tok, "reason": "no route from any holder"} for n in lost]
        terms = [n for n in needers if n in reach]
        if not terms:
            continue
        if token_transfer == "move":
            cost, walks, covered = _move_walks(costs, communication_cost, sorted(holders), terms)
            unreachable += [{"node": n, "token": tok, "reason": NO_SINGLE_CHAIN} for n in terms if n not in covered]
            hops = [hp for w in walks for hp in w["hops"]]
            relay_hops = [v for _, v, is_relay in hops if is_relay]
            per_token[tok] = {
                "credits": int(cost),
                # in walk order; an edge can appear more than once when a walk revisits a node
                "exchanges": [{"from": u, "to": v, "cost": edge_price[(u, v)], "tax": transfer_tax, "relay": is_relay}
                              for u, v, is_relay in hops],
                "walks": [[w["holder"]] + [v for _, v, _ in w["hops"]] for w in walks],
                "relays": sorted(set(relay_hops)),
                "relay_messages": len(relay_hops),
            }
        else:
            weight = {(u, v): c + (communication_cost if v not in holders and v not in needers else 0)
                      for (u, v), c in costs.items()}
            cost, tree = _steiner(nodes, weight, holders, terms)
            relays = sorted({v for _, v in tree if v not in needers and v not in holders})
            per_token[tok] = {
                "credits": int(cost),
                "exchanges": [{"from": u, "to": v, "cost": edge_price[(u, v)], "tax": transfer_tax}
                              for u, v in sorted(tree)],
                "relays": relays,
                "relay_messages": len(relays),
            }
        total += int(cost)
    # Budget check on the ideal plan: what each node must pay for its own purchases,
    # versus what it starts with (income from sales could still cover a gap).
    spend: dict[str, int] = {}
    for info in per_token.values():
        for x in info["exchanges"]:
            spend[x["to"]] = spend.get(x["to"], 0) + x["cost"] + x.get("tax", 0) + fee_b
            spend[x["from"]] = spend.get(x["from"], 0) + fee_s
    shortfalls = {n: c - graph.nodes[n].credits for n, c in spend.items() if c > graph.nodes[n].credits}
    return {
        "solvable": not unreachable,
        "budget_shortfalls": shortfalls,  # node -> credits missing for its part of the ideal plan
        "ideal_credits": total if not unreachable else None,
        "ideal_credits_reachable": total,
        "per_token": per_token,
        "unreachable": unreachable,
        "token_transfer": token_transfer,
        "assumptions": "minimum over routes ignoring per-node budgets; one message per relay request; "
                       + ("each holder's copy travels as one chain of sales (move)" if token_transfer == "move"
                          else "a bought token can serve several needers downstream (copy)"),
    }


def graph_ideal(graph: GraphSpec, cfg: ExperimentConfig, execution_fee: dict[str, int]) -> dict[str, Any]:
    """`ideal_cost` under the experiment's rules: every transfer pays its price, the offer tax and the
    execution fees, and relays are asked by offers (no separate message)."""
    return ideal_cost(graph, 0, cfg.market.token_transfer, transfer_tax=cfg.market.offer_tax,
                      execution_fee=execution_fee)


def graph_to_world(graph: GraphSpec, cfg: ExperimentConfig, impossible: dict[str, list[str]], *,
                   execution_fee: dict[str, int] | None = None, ideal: dict[str, Any] | None = None) -> dict[str, Any]:
    """The world file the broker runs. `execution_fee` and `ideal` default to the config's fee and `graph_ideal`."""
    costs = graph.edge_costs()
    fee = execution_fee or cfg.market.execution_fee.model_dump()
    return {
        "nodes": {n: spec.model_dump() for n, spec in graph.nodes.items()},
        "edges": [{"from": s, "to": d, "cost": c} for (s, d), c in sorted(costs.items())],
        "communication_cost": cfg.communication_cost,
        "seed": cfg.seed,
        "rounds": cfg.rounds.model_dump() | {"round_seconds": cfg.round_seconds},
        # Offers are how demand travels (a relay is asked by an offer), so no separate relay message.
        "ideal": ideal if ideal is not None else graph_ideal(graph, cfg, fee),
        "impossible": impossible,  # generator ground truth; not exposed to agents
        "rules": {
            "allow_broadcast": cfg.market.allow_broadcast,
            "graph_visibility": cfg.market.graph_visibility,
            "max_submissions": cfg.market.max_submissions,
            "token_transfer": cfg.market.token_transfer,
            "offer_tax": cfg.market.offer_tax,
            "messages": cfg.market.messages,
            "sale_timeout_seconds": cfg.market.sale_timeout_seconds,
            "execution_fee": fee,
            "opaque_errors": cfg.market.opaque_errors,
        },
    }
