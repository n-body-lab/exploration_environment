"""The game graph: building it, and the ideal (minimum) credits to solve it.

Ideal cost
----------
Tokens are non-rival and can be relayed: once a node holds a token it can
sell it along any outgoing edge. So the cheapest way to get one token to
every node that needs it is a minimum directed Steiner tree rooted at the
token's holders (a token bought once can serve several needers downstream).
Tokens are routed independently.

Communication: a node that buys a token only to pass it on (it neither holds
nor needs it) has to be asked to do so, costing one message. That is folded
into the edge weight: entering such a node costs edge + communication_cost.

Solved exactly with the Dreyfus-Wagner DP (3^k * n for k needers of a
token); budgets are ignored, so `ideal` is a lower bound on what any
successful round can spend, and `solvable` reports unreachable needs.
"""

from __future__ import annotations

import heapq
import random
from itertools import combinations
from typing import Any

from hivemarket.common.config import ExperimentConfig, GraphEdge, GraphNode, GraphSpec

INF = float("inf")


# ------------------------------------------------------------------ building

def build_graph(cfg: ExperimentConfig, agent_ids: list[str]) -> tuple[GraphSpec, dict[str, list[str]]]:
    """Explicit `graph`, or one compiled from the `tasks` generator. Returns (graph, impossible-by-agent)."""
    if cfg.graph:
        return cfg.graph, {}
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


def ideal_cost(graph: GraphSpec, communication_cost: int = 0) -> dict[str, Any]:
    nodes = list(graph.nodes)
    costs = graph.edge_costs()
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
        weight = {(u, v): c + (communication_cost if v not in holders and v not in needers else 0)
                  for (u, v), c in costs.items()}
        # Reachability first, so one unreachable needer doesn't hide the cost for the others.
        reach = set().union(*(_dijkstra({u: [(v, 0) for (a, v) in weight if a == u] for u in nodes}, h)[0] for h in holders))
        lost = [n for n in needers if n not in reach]
        unreachable += [{"node": n, "token": tok, "reason": "no route from any holder"} for n in lost]
        terms = [n for n in needers if n in reach]
        if not terms:
            continue
        cost, tree = _steiner(nodes, weight, holders, terms)
        relays = sorted({v for _, v in tree if v not in needers and v not in holders})
        plan = sorted(tree)
        per_token[tok] = {
            "credits": int(cost),
            "exchanges": [{"from": u, "to": v, "cost": costs[(u, v)]} for u, v in plan],
            "relays": relays,
            "relay_messages": len(relays),
        }
        total += int(cost)
    # Budget check on the ideal plan: what each node must pay for its own purchases,
    # versus what it starts with (income from sales could still cover a gap).
    spend: dict[str, int] = {}
    for info in per_token.values():
        for x in info["exchanges"]:
            spend[x["to"]] = spend.get(x["to"], 0) + x["cost"]
    shortfalls = {n: c - graph.nodes[n].credits for n, c in spend.items() if c > graph.nodes[n].credits}
    return {
        "solvable": not unreachable,
        "budget_shortfalls": shortfalls,  # node -> credits missing for its part of the ideal plan
        "ideal_credits": total if not unreachable else None,
        "ideal_credits_reachable": total,
        "per_token": per_token,
        "unreachable": unreachable,
        "assumptions": "minimum over routes ignoring per-node budgets; one message per relay request",
    }


def graph_to_world(graph: GraphSpec, cfg: ExperimentConfig, impossible: dict[str, list[str]]) -> dict[str, Any]:
    costs = graph.edge_costs()
    return {
        "nodes": {n: spec.model_dump() for n, spec in graph.nodes.items()},
        "edges": [{"from": s, "to": d, "cost": c} for (s, d), c in sorted(costs.items())],
        "communication_cost": cfg.communication_cost,
        "seed": cfg.seed,
        "rounds": cfg.rounds.model_dump() | {"round_seconds": cfg.round_seconds},
        "ideal": ideal_cost(graph, cfg.communication_cost),
        "impossible": impossible,  # generator ground truth; not exposed to agents
        "rules": {
            "allow_broadcast": cfg.market.allow_broadcast,
            "graph_visibility": cfg.market.graph_visibility,
            "max_submissions": cfg.market.max_submissions,
            "token_transfer": cfg.market.token_transfer,
        },
    }
