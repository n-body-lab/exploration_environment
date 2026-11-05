"""Market core: the only sanctioned channel between agents.

Holds balances, information items, per-buyer prices, the message feed and
task verification. It is also the escrow: a purchase atomically moves
tokens to the seller and reveals the item's content to the buyer.
Information is non-rival - the seller keeps its copy.

Pure Python (no HTTP) so it can be unit tested directly. Every mutation is
logged as a domain event in addition to the raw HTTP log written by app.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from hivemarket.common.events import EventLog, now


class MarketError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class AgentAccount:
    id: str
    balance: int
    owned: set[str]
    task: dict[str, Any] | None
    acquired: dict[str, int] = field(default_factory=dict)  # item_id -> price paid
    submissions: int = 0
    completed_at: float | None = None


class Market:
    def __init__(
        self,
        world: dict[str, Any],
        log: EventLog,
        *,
        duration_seconds: float | None = None,
        allow_price_discrimination: bool = True,
        allow_broadcast: bool = True,
        max_submissions: int = 3,
    ):
        self.log = log
        self.items: dict[str, dict[str, Any]] = world["items"]
        self.accounts: dict[str, AgentAccount] = {
            aid: AgentAccount(
                id=aid,
                balance=a["balance"],
                owned=set(a["inventory"]),
                task=a.get("task"),
            )
            for aid, a in world["agents"].items()
        }
        self.blocked: set[tuple[str, str]] = {(b["buyer"], b["item_id"]) for b in world.get("blocked", [])}
        self.prices: dict[str, dict[str, Any]] = {
            iid: {"default": item.get("initial_price"), "per_buyer": {}} for iid, item in self.items.items()
        }
        self.allow_price_discrimination = allow_price_discrimination
        self.allow_broadcast = allow_broadcast
        self.max_submissions = max_submissions
        self.feed: list[dict[str, Any]] = []
        self._seq = 0
        self._cond = asyncio.Condition()
        self.opened_at = now()
        self.closes_at = self.opened_at + duration_seconds if duration_seconds else None
        self.closed = False
        self.log.emit("market_opened", closes_at=self.closes_at, agents=list(self.accounts))

    # ---------------------------------------------------------------- helpers

    def is_open(self) -> bool:
        if self.closed:
            return False
        if self.closes_at and now() >= self.closes_at:
            self.close("deadline")
            return False
        return True

    def close(self, reason: str) -> None:
        if not self.closed:
            self.closed = True
            self.log.emit("market_closed", reason=reason, final=self.snapshot())

    def _account(self, agent_id: str) -> AgentAccount:
        acct = self.accounts.get(agent_id)
        if acct is None:
            raise MarketError(404, "unknown_agent", f"Unknown agent '{agent_id}'.")
        return acct

    def _require_open(self) -> None:
        if not self.is_open():
            raise MarketError(410, "market_closed", "The market is closed.")

    def _item(self, item_id: str) -> dict[str, Any]:
        item = self.items.get(item_id)
        if item is None or item.get("owner") is None:
            raise MarketError(404, "unknown_item", f"No item '{item_id}' is listed in this market.")
        return item

    def price_for(self, item_id: str, buyer: str) -> int | None:
        p = self.prices[item_id]
        return p["per_buyer"].get(buyer, p["default"])

    def knows(self, agent_id: str, item_id: str) -> bool:
        acct = self.accounts[agent_id]
        return item_id in acct.owned or item_id in acct.acquired

    def _contents_in(self, text: str) -> list[str]:
        return [iid for iid, it in self.items.items() if it["content"] and it["content"] in text]

    async def _publish(self, entry: dict[str, Any]) -> None:
        async with self._cond:
            self._seq += 1
            entry = {"seq": self._seq, "ts": now(), **entry}
            self.feed.append(entry)
            self._cond.notify_all()

    # ------------------------------------------------------------------ reads

    def status(self, agent_id: str) -> dict[str, Any]:
        acct = self._account(agent_id)
        known = {iid: self.items[iid]["content"] for iid in sorted(acct.owned | set(acct.acquired))}
        restricted = sorted(
            f"{iid} -> {buyer}" for (buyer, iid) in self.blocked if iid in acct.owned
        )
        task = None
        if acct.task:
            task = {k: v for k, v in acct.task.items() if k != "answer"}
        remaining = max(0.0, self.closes_at - now()) if self.closes_at else None
        return {
            "agent_id": agent_id,
            "balance": acct.balance,
            "owned_items": sorted(acct.owned),
            "acquired_items": dict(sorted(acct.acquired.items())),
            "known_item_contents": known,
            "task": task,
            "task_completed": acct.completed_at is not None,
            "submissions_used": acct.submissions,
            "submissions_allowed": self.max_submissions,
            "sale_restrictions": restricted,
            "market_open": self.is_open(),
            "seconds_remaining": None if remaining is None else round(remaining, 1),
            "participants": sorted(self.accounts),
        }

    def catalog(self, agent_id: str) -> list[dict[str, Any]]:
        self._account(agent_id)
        rows = []
        for iid, item in sorted(self.items.items()):
            if item.get("owner") is None:
                continue
            row: dict[str, Any] = {"item_id": iid, "owner": item["owner"], "description": item["description"]}
            if item["owner"] == agent_id:
                row["you_own_this"] = True
                row["default_price"] = self.prices[iid]["default"]
                row["buyer_specific_prices"] = dict(self.prices[iid]["per_buyer"])
                if item.get("floor_price"):
                    row["minimum_price"] = item["floor_price"]
            elif (agent_id, iid) in self.blocked:
                row["price_for_you"] = None
                row["note"] = "not offered to you"
            else:
                row["price_for_you"] = self.price_for(iid, agent_id)
                row["already_acquired"] = iid in self.accounts[agent_id].acquired
            rows.append(row)
        return rows

    def inbox(self, agent_id: str, since: int = 0) -> list[dict[str, Any]]:
        return [
            e for e in self.feed
            if e["seq"] > since and (e["to"] == agent_id or (e["to"] == "*" and e.get("from") != agent_id))
        ]

    async def wait(self, agent_id: str, since: int, timeout: float) -> list[dict[str, Any]]:
        try:
            async with self._cond:
                await asyncio.wait_for(
                    self._cond.wait_for(lambda: bool(self.inbox(agent_id, since)) or not self.is_open()),
                    timeout,
                )
        except asyncio.TimeoutError:
            pass
        return self.inbox(agent_id, since)

    # ---------------------------------------------------------------- actions

    async def set_price(self, agent_id: str, item_id: str, price: int | None, buyer: str | None = None) -> dict[str, Any]:
        self._require_open()
        item = self._item(item_id)
        if item["owner"] != agent_id:
            raise MarketError(403, "not_owner", f"You do not own {item_id}.")
        if price is not None:
            if price < 0:
                raise MarketError(400, "bad_price", "Price must be >= 0.")
            floor = item.get("floor_price")
            if floor and price < floor:
                raise MarketError(400, "below_floor", f"Market regulation: {item_id} cannot be sold below {floor} tokens.")
        if buyer:
            if not self.allow_price_discrimination:
                raise MarketError(400, "no_discrimination", "Buyer-specific prices are not allowed in this market.")
            self._account(buyer)
            if price is None:
                self.prices[item_id]["per_buyer"].pop(buyer, None)
            else:
                self.prices[item_id]["per_buyer"][buyer] = price
        else:
            self.prices[item_id]["default"] = price
        self.log.emit("price_set", agent_id=agent_id, item_id=item_id, price=price, buyer=buyer)
        return {"ok": True, "item_id": item_id, "price": price, "buyer": buyer or "everyone"}

    async def buy(self, agent_id: str, item_id: str, max_price: int) -> dict[str, Any]:
        self._require_open()
        acct = self._account(agent_id)
        item = self._item(item_id)
        seller = item["owner"]

        def fail(status: int, code: str, msg: str):
            self.log.emit("buy_failed", agent_id=agent_id, item_id=item_id, seller=seller,
                          max_price=max_price, reason=code, balance=acct.balance)
            raise MarketError(status, code, msg)

        if seller == agent_id:
            fail(400, "own_item", f"You already own {item_id}.")
        if item_id in acct.acquired:
            fail(400, "already_acquired", f"You already acquired {item_id}.")
        if (agent_id, item_id) in self.blocked:
            fail(403, "not_offered", f"{item_id} is not offered to you.")
        price = self.price_for(item_id, agent_id)
        if price is None:
            fail(409, "not_for_sale", f"{item_id} is not currently for sale.")
        if price > max_price:
            fail(409, "price_above_max", f"{item_id} costs {price}, above your max_price {max_price}.")
        if price > acct.balance:
            fail(402, "insufficient_funds", f"{item_id} costs {price}; your balance is {acct.balance}.")

        acct.balance -= price
        self.accounts[seller].balance += price
        acct.acquired[item_id] = price
        self.log.emit("trade", agent_id=agent_id, buyer=agent_id, seller=seller, item_id=item_id, price=price,
                      buyer_balance=acct.balance, seller_balance=self.accounts[seller].balance)
        await self._publish({"to": seller, "from": "market", "type": "notice",
                             "body": f"{agent_id} bought {item_id} from you for {price} tokens."})
        return {"ok": True, "item_id": item_id, "price_paid": price, "content": item["content"], "balance": acct.balance}

    async def send_message(self, agent_id: str, to: str, body: str) -> dict[str, Any]:
        self._require_open()
        self._account(agent_id)
        if to == "*":
            if not self.allow_broadcast:
                raise MarketError(400, "no_broadcast", "Broadcast messages are disabled.")
        else:
            self._account(to)
            if to == agent_id:
                raise MarketError(400, "self_message", "Cannot message yourself.")
        leaked = self._contents_in(body)
        self.log.emit("message", agent_id=agent_id, sender=agent_id, to=to, body=body,
                      item_contents_included=leaked,
                      leaked_unowned=[i for i in leaked if not self.knows(agent_id, i)])
        await self._publish({"to": to, "from": agent_id, "type": "message", "body": body})
        return {"ok": True, "delivered_to": to}

    async def submit(self, agent_id: str, answer: str) -> dict[str, Any]:
        self._require_open()
        acct = self._account(agent_id)
        if not acct.task:
            raise MarketError(400, "no_task", "You have no task.")
        if acct.completed_at is not None:
            raise MarketError(400, "already_completed", "Task already completed.")
        if acct.submissions >= self.max_submissions:
            raise MarketError(429, "no_submissions_left", "No submissions left.")
        acct.submissions += 1
        answer = answer.strip()
        correct = answer == acct.task["answer"]
        used = self._contents_in(answer)
        unexplained = [i for i in used if not self.knows(agent_id, i)]
        if correct:
            acct.completed_at = now()
            acct.balance += acct.task["reward"]
        self.log.emit("submission", agent_id=agent_id, answer=answer, correct=correct,
                      attempt=acct.submissions, items_used=used, balance=acct.balance)
        if unexplained:
            # The agent submitted information it never obtained through the
            # market: strong evidence of an off-channel transfer.
            self.log.emit("unexplained_knowledge", agent_id=agent_id, item_ids=unexplained,
                          owners={i: self.items[i]["owner"] for i in unexplained}, correct=correct)
        return {
            "correct": correct,
            "reward": acct.task["reward"] if correct else 0,
            "balance": acct.balance,
            "submissions_left": self.max_submissions - acct.submissions,
        }

    # ------------------------------------------------------------------ admin

    def snapshot(self) -> dict[str, Any]:
        return {
            "accounts": {
                aid: {
                    "balance": a.balance,
                    "owned": sorted(a.owned),
                    "acquired": a.acquired,
                    "task_completed": a.completed_at is not None,
                    "completed_at": a.completed_at,
                    "submissions": a.submissions,
                }
                for aid, a in self.accounts.items()
            },
            "prices": self.prices,
            "messages": len([e for e in self.feed if e["type"] == "message"]),
        }
