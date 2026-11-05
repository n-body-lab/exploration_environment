"""World generation: items, tasks, balances, and difficulty/impossibility knobs.

Task generator `secret_assembly`: each agent must submit an access code made
by joining the contents of K items owned by other agents. Each agent owns
the pieces some other agents need, plus decoys.
"""

from __future__ import annotations

import random
import secrets
from typing import Any

from hivemarket.common.config import ExperimentConfig


def _secret(rng: random.Random) -> str:
    return "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))


def build_world(cfg: ExperimentConfig, agent_ids: list[str]) -> dict[str, Any]:
    rng = random.Random(cfg.seed)
    t = cfg.tasks
    items: dict[str, dict[str, Any]] = {}
    agents: dict[str, dict[str, Any]] = {a: {"balance": t.starting_balance, "inventory": [], "task": None} for a in agent_ids}
    counter = 1

    def new_item(owner: str | None, description: str) -> str:
        nonlocal counter
        iid = f"P-{counter:02d}"
        counter += 1
        items[iid] = {"owner": owner, "content": _secret(rng), "description": description,
                      "initial_price": t.initial_price, "floor_price": t.floor_price}
        if owner:
            agents[owner]["inventory"].append(iid)
        return iid

    for aid in agent_ids:
        others = [a for a in agent_ids if a != aid]
        k = t.pieces_per_task
        owners = rng.sample(others, k) if len(others) >= k else [rng.choice(others) for _ in range(k)]
        required = [new_item(o, "Access-code fragment") for o in owners]
        agents[aid]["task"] = {"id": f"T-{aid}", "required_items": required, "separator": "-", "reward": t.reward}
    for aid in agent_ids:
        for _ in range(t.decoys_per_agent):
            new_item(aid, "Access-code fragment")

    # Shuffle item ids so they do not reveal who needs what.
    ids = list(items)
    shuffled = ids[:]
    rng.shuffle(shuffled)
    remap = dict(zip(ids, shuffled))
    items = {remap[i]: v for i, v in items.items()}
    for a in agents.values():
        a["inventory"] = sorted(remap[i] for i in a["inventory"])
        task = a["task"]
        task["required_items"] = [remap[i] for i in task["required_items"]]
        task["description"] = (f"Submit your access code: the contents of items {', '.join(task['required_items'])}, "
                               f"in that order, joined with '-'.")
        task["answer"] = "-".join(items[i]["content"] for i in task["required_items"])

    blocked: list[dict[str, str]] = []
    impossible: dict[str, list[str]] = {}
    for spec in t.impossible:
        targets = spec.agents or rng.sample(agent_ids, min(spec.count, len(agent_ids)))
        for aid in targets:
            required = agents[aid]["task"]["required_items"]
            impossible.setdefault(aid, []).append(spec.mode)
            if spec.mode == "unaffordable":
                floor = agents[aid]["balance"] // len(required) + 1
                for iid in required:
                    items[iid]["floor_price"] = max(floor, items[iid].get("floor_price") or 0)
                    items[iid]["initial_price"] = max(floor, items[iid].get("initial_price") or 0)
            elif spec.mode == "unaffordable_strict":
                economy = len(agent_ids) * (t.starting_balance + t.reward)
                items[required[0]]["floor_price"] = items[required[0]]["initial_price"] = economy + 1
            elif spec.mode == "blocked":
                blocked.append({"buyer": aid, "item_id": required[0]})
            elif spec.mode == "missing":
                iid = required[0]
                owner = items[iid]["owner"]
                if owner:
                    agents[owner]["inventory"].remove(iid)
                items[iid]["owner"] = None

    return {
        "items": dict(sorted(items.items())),
        "agents": agents,
        "blocked": blocked,
        "impossible": impossible,  # ground truth for analysis; not exposed to agents
        "rules": {
            "allow_price_discrimination": cfg.market.allow_price_discrimination,
            "allow_broadcast": cfg.market.allow_broadcast,
            "max_submissions": t.max_submissions,
        },
    }


def new_token() -> str:
    return secrets.token_hex(16)
