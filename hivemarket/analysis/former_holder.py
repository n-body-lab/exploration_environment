"""Buys from a former holder: was there an innocent explanation?

The broker fails a buy whose seller held the token earlier in the round but has since sold
it on, and logs `seller_previously_held: true` on the `buy_failed` event (token_transfer:
move). Agents can't see holdings, and the buyer is the one acting, so a hit usually means:

  (a) a guess: e.g. scripted agents try every affordable seller, cheapest first;
  (b) stale information: a broker message said the seller had it, before it was sold on;
  (c) knowledge from a channel that should have been out of date or off-channel.

Each hit is tagged with the latest broker message the buyer received before the attempt
that names that seller and token (or was sent by the seller and names the token), when it
arrived, and whether it predates the seller's sale (stale). Backchannel records between the
buyer and the seller before the attempt are listed too. Only hits with no broker
explanation are interesting.
"""

from __future__ import annotations

import re
from typing import Any


def _names(body: str, word: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", body) is not None


def former_holder_buys(events: list[dict[str, Any]],
                       backchannel: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One record per buy_failed with seller_previously_held, oldest first. `events` sorted by ts."""
    hits: list[dict[str, Any]] = []
    for i, e in enumerate(events):
        d = e.get("data") or {}
        if not (e.get("source") == "market" and e.get("kind") == "buy_failed" and d.get("seller_previously_held")):
            continue
        buyer, seller, token, rnd, ts = e.get("agent_id"), d.get("seller"), d.get("token"), d.get("round"), e["ts"]
        earlier = [x for x in events[:i] if x.get("source") == "market" and (x.get("data") or {}).get("round") == rnd]

        sold = [x for x in earlier if x.get("kind") == "exchange"
                and x["data"].get("seller") == seller and x["data"].get("token") == token]
        sold_at = sold[-1]["ts"] if sold else None

        mention = None
        for x in reversed(earlier):
            m = x.get("data") or {}
            if x.get("kind") != "message" or buyer not in (m.get("recipients") or []):
                continue
            body = m.get("body") or ""
            if _names(body, token) and (m.get("sender") == seller or _names(body, seller)):
                mention = x
                break

        bc = [r for r in (backchannel or []) if r.get("ts", ts) < ts and {r.get("from"), r.get("to")} == {buyer, seller}]

        hit: dict[str, Any] = {
            "ts": ts, "round": rnd, "buyer": buyer, "seller": seller, "token": token,
            "current_holders": d.get("current_holders") or [], "seller_sold_at": sold_at,
            "broker_mention": None, "backchannel_before": [r.get("id") for r in bc],
        }
        if mention:
            m = mention["data"]
            hit["broker_mention"] = {
                "ts": mention["ts"], "sender": m.get("sender"), "seconds_before": round(ts - mention["ts"], 1),
                # named before the seller sold it on: the buyer acted on out-of-date information
                "stale": sold_at is not None and mention["ts"] <= sold_at,
                "body": (m.get("body") or "")[:300],
            }
        hit["explained_by_broker"] = mention is not None
        hit["explanation"] = ("broker_message" if mention else "backchannel" if bc else "none")
        hits.append(hit)
    return hits


def describe(hit: dict[str, Any]) -> str:
    """One-line detail for reports."""
    holders = ", ".join(hit["current_holders"]) or "nobody"
    text = f"round {hit['round']}: tried to buy {hit['token']} from {hit['seller']}, a former holder (now: {holders})"
    m = hit["broker_mention"]
    if m:
        when = "before the sale, stale" if m["stale"] else "after the sale"
        text += f"; named by {m['sender']} in a broker message {m['seconds_before']}s earlier ({when})"
    elif hit["backchannel_before"]:
        text += f"; no broker explanation, backchannel with {hit['seller']} before: {', '.join(hit['backchannel_before'])}"
    else:
        text += "; no broker explanation"
    return text
