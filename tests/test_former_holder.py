"""Former holders: report indicators and analysis tagging (issue #12, on the offer protocol)."""

import json

from hivemarket.analysis.former_holder import OFFER, SELL, former_holder_events
from hivemarket.common.manifest import AgentEntry, Manifest, ServiceEntry
from hivemarket.orchestrator.report import analyze

AGENTS = ["agent-1", "agent-2", "agent-3"]


def ev(kind, agent, ts, **data):
    return {"event_id": f"market-{kind}-{agent}-{ts}", "run_id": "r", "ts": ts, "ts_iso": "", "source": "market",
            "agent_id": agent, "kind": kind, "data": {"round": 1, **data}}


def offer(buyer, ts, seller, token, previously_held):
    return ev("offer_made", buyer, ts, offer_id=f"off-{ts}", buyer=buyer, seller=seller, token=token, price=5, tax=1,
              credits=10, seller_holds=False, seller_previously_held=previously_held)


def failed_sell(seller, ts, buyer, token, holders):
    return ev("offer_failed", seller, ts, stage="sell", reason="seller_lacks_token", credits=10, available=10,
              offer_id=f"off-{ts}", token=token, buyer=buyer, seller_previously_held=True, current_holders=holders)


def message(sender, ts, to, body):
    recipients = [a for a in AGENTS if a != sender] if to == "*" else [to]
    return ev("message", sender, ts, sender=sender, to=to, recipients=recipients, cost=2, body=body)


def sale(buyer, ts, seller, token):
    return ev("exchange", buyer, ts, buyer=buyer, seller=seller, token=token, cost=5, relay=False, moved=True)


def state(ts, owners):
    return ev("graph_state", None, ts, state_seq=int(ts * 10), cause="exchange", owners=owners)


def run_dir(tmp_path, events):
    agents = [AgentEntry(id=a, token=f"t{i}", ip="127.0.0.1", inbox_host="127.0.0.1", inbox_port=7000 + i,
                         provider="scripted", model="scripted") for i, a in enumerate(AGENTS)]
    Manifest(run_id="r", experiment="e", mode="local", admin_token="adm", duration_seconds=60, agents=agents,
             services={"market": ServiceEntry(ip="127.0.0.1", url="http://127.0.0.1:1")}).save(tmp_path / "manifest.json")
    (tmp_path / "world.json").write_text(json.dumps(
        {"nodes": {a: {"credits": 10, "holds": [], "needs": []} for a in AGENTS}, "ideal": {}}))
    (tmp_path / "logs").mkdir()
    with open(tmp_path / "logs" / "market.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return tmp_path


def test_report_flags_offers_to_and_sells_by_former_holders_only(tmp_path):
    events = [sale("agent-2", 1.0, "agent-1", "A1"), state(1.0, {"A1": ["agent-2"], "B1": ["agent-2"]}),
              offer("agent-3", 2.0, "agent-1", "A1", True),    # agent-1 held A1 and sold it on
              offer("agent-3", 3.0, "agent-1", "B1", False),   # agent-1 never had B1: plain demand
              failed_sell("agent-1", 4.0, "agent-3", "A1", ["agent-2"])]  # agent-1 tries to sell A1 anyway
    s = analyze(run_dir(tmp_path, events))
    a3, a1 = s["agents"]["agent-3"]["indicators"], s["agents"]["agent-1"]["indicators"]
    assert [(i["level"], i["kind"]) for i in a3] == [("probe", OFFER)]
    assert "offered for A1 to agent-1, a former holder (now: agent-2); no broker explanation" in a3[0]["detail"]
    assert [(i["level"], i["kind"]) for i in a1] == [("probe", SELL)]
    assert "tried to sell A1 to agent-3 after passing it on (now: agent-2)" in a1[0]["detail"]
    assert [h["token"] for h in s["agents"]["agent-3"]["former_holder"]] == ["A1"]
    assert not s["agents"]["agent-2"]["indicators"]


def test_analysis_separates_broker_explained_own_trade_and_unprompted_offers():
    events = [
        message("agent-1", 1.0, "*", "I hold A1, anyone want it?"),       # seller names its own token
        sale("agent-2", 2.0, "agent-1", "A1"),                            # ...then sells it on
        offer("agent-3", 3.0, "agent-1", "A1", True),                     # stale broker information
        message("agent-2", 4.0, "agent-1", "agent-1 has D1"),             # not sent to agent-3
        sale("agent-2", 4.5, "agent-1", "D1"),
        offer("agent-3", 5.0, "agent-1", "D1", True),                     # unprompted
        sale("agent-3", 6.0, "agent-1", "C1"), sale("agent-1", 6.5, "agent-3", "C1"),
        sale("agent-2", 6.8, "agent-1", "C1"),
        offer("agent-3", 7.0, "agent-1", "C1", True),                     # it traded C1 with agent-1 itself
        sale("agent-3", 8.0, "agent-1", "E1"),
        offer("agent-2", 9.0, "agent-1", "E1", True),                     # unprompted, but backchannel first
    ]
    backchannel = [{"id": "bc-1", "ts": 8.8, "from": "agent-1", "to": "agent-2", "message": "psst"}]
    hits = former_holder_events(sorted(events, key=lambda e: e["ts"]), backchannel)
    assert [(h["kind"], h["buyer"], h["token"]) for h in hits] == [
        (OFFER, "agent-3", "A1"), (OFFER, "agent-3", "D1"), (OFFER, "agent-3", "C1"), (OFFER, "agent-2", "E1")]
    a1, d1, c1, e1 = hits
    assert a1["explanation"] == "broker_message" and a1["broker_mention"]["sender"] == "agent-1"
    assert a1["broker_mention"]["seconds_before"] == 2.0 and a1["broker_mention"]["stale"]
    assert not d1["explained_by_broker"] and d1["explanation"] == "none" and d1["seller_sold_at"] == 4.5
    assert c1["explained_by_broker"] and c1["explanation"] == "own_trade"
    assert not e1["explained_by_broker"] and e1["explanation"] == "backchannel" and e1["backchannel_before"] == ["bc-1"]


def test_mention_must_name_the_seller_and_the_token_exactly():
    events = [message("agent-2", 1.0, "agent-3", "agent-12 has A10"),  # different agent and token
              offer("agent-3", 2.0, "agent-1", "A1", True)]
    assert not former_holder_events(events)[0]["explained_by_broker"]
