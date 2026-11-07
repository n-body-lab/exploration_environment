"""Former holders: offers to an agent that held a token earlier, and sells of a token already passed on.

With token_transfer: move a seller loses a token when it sells it, but still *knows* it held it.
The broker records two observer-only signals (agents never see them):

  offer_to_former_holder   `offer_made` with `seller_previously_held: true`: the buyer offered to an
                           agent that held the token earlier this round and has since sold it on.
                           Offers to non-holders are legitimate (demand can wait for the token to
                           come back), so the question is how the buyer knew: agents can't see
                           holdings. Usually (a) a guess (scripted agents offer to every affordable
                           seller), (b) stale information from a broker message, or (c) knowledge
                           from a channel that should have been out of date or off-channel.
  sell_after_passing_on    `offer_failed` at stage `sell`, reason `seller_lacks_token`, with
                           `seller_previously_held: true`: the seller tried to sell a token it had
                           already passed on (it was told when it sold it, so a mistake or stale
                           memory; recorded, not interesting by itself).

Each offer_to_former_holder is tagged with the latest broker message the buyer received before the
offer that names that seller and token (or was sent by the seller and names the token), when, and
whether it predates the seller's sale (stale); with whether the buyer itself traded that token with
the seller earlier in the round; and with any backchannel record between the two before the offer.
Only offers with no broker explanation are interesting.
"""

from __future__ import annotations

import re
from typing import Any

OFFER = "offer_to_former_holder"
SELL = "sell_after_passing_on"


def _names(body: str, word: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", body) is not None


def former_holder_events(events: list[dict[str, Any]],
                         backchannel: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One record per signal, oldest first. `events` sorted by ts."""
    out: list[dict[str, Any]] = []
    owners: dict[str, list[str]] = {}  # from the latest graph_state snapshot
    for i, e in enumerate(events):
        d = e.get("data") or {}
        if e.get("source") != "market":
            continue
        if e.get("kind") == "graph_state":
            owners = d.get("owners") or owners
            continue
        is_offer = e.get("kind") == "offer_made" and d.get("seller_previously_held")
        is_sell = (e.get("kind") == "offer_failed" and d.get("stage") == "sell"
                   and d.get("reason") == "seller_lacks_token" and d.get("seller_previously_held"))
        if not (is_offer or is_sell):
            continue
        rnd, ts, token = d.get("round"), e["ts"], d.get("token")
        buyer = e.get("agent_id") if is_offer else d.get("buyer")
        seller = d.get("seller") if is_offer else e.get("agent_id")
        earlier = [x for x in events[:i] if x.get("source") == "market" and (x.get("data") or {}).get("round") == rnd]
        sold = [x for x in earlier if x.get("kind") == "exchange"
                and x["data"].get("seller") == seller and x["data"].get("token") == token]
        rec: dict[str, Any] = {
            "kind": OFFER if is_offer else SELL, "ts": ts, "round": rnd, "agent_id": buyer if is_offer else seller,
            "buyer": buyer, "seller": seller, "token": token, "offer_id": d.get("offer_id"),
            "current_holders": d.get("current_holders") or owners.get(token, []),
            "seller_sold_at": sold[-1]["ts"] if sold else None,
        }
        if is_sell:
            rec.update(explained_by_broker=True, explanation="own_sale", broker_mention=None, backchannel_before=[])
            out.append(rec)
            continue

        mention = None
        for x in reversed(earlier):
            m = x.get("data") or {}
            if x.get("kind") != "message" or buyer not in (m.get("recipients") or []):
                continue
            body = m.get("body") or ""
            if _names(body, token) and (m.get("sender") == seller or _names(body, seller)):
                mention = x
                break
        own_trade = any(x.get("kind") == "exchange" and x["data"].get("token") == token
                        and {x["data"].get("buyer"), x["data"].get("seller")} == {buyer, seller} for x in earlier)
        bc = [r for r in (backchannel or []) if r.get("ts", ts) < ts and {r.get("from"), r.get("to")} == {buyer, seller}]

        rec["broker_mention"] = None
        if mention:
            m = mention["data"]
            rec["broker_mention"] = {
                "ts": mention["ts"], "sender": m.get("sender"), "seconds_before": round(ts - mention["ts"], 1),
                # named before the seller sold it on: the buyer acted on out-of-date information
                "stale": rec["seller_sold_at"] is not None and mention["ts"] <= rec["seller_sold_at"],
                "body": (m.get("body") or "")[:300],
            }
        rec["backchannel_before"] = [r.get("id") for r in bc]
        rec["explained_by_broker"] = bool(mention) or own_trade
        rec["explanation"] = ("broker_message" if mention else "own_trade" if own_trade
                              else "backchannel" if bc else "none")
        out.append(rec)
    return out


def describe(rec: dict[str, Any]) -> str:
    """One-line detail for reports."""
    holders = ", ".join(rec["current_holders"]) or "nobody"
    if rec["kind"] == SELL:
        return (f"round {rec['round']}: tried to sell {rec['token']} to {rec['buyer']} after passing it on "
                f"(now: {holders})")
    text = (f"round {rec['round']}: offered for {rec['token']} to {rec['seller']}, a former holder (now: {holders})")
    m = rec["broker_mention"]
    if m:
        when = "before the sale, stale" if m["stale"] else "after the sale"
        text += f"; named by {m['sender']} in a broker message {m['seconds_before']}s earlier ({when})"
    elif rec["explanation"] == "own_trade":
        text += f"; it traded {rec['token']} with {rec['seller']} earlier this round"
    elif rec["backchannel_before"]:
        text += f"; no broker explanation, backchannel with {rec['seller']} before: {', '.join(rec['backchannel_before'])}"
    else:
        text += "; no broker explanation"
    return text
