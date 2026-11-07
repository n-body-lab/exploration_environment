"""Buys from a former holder: report indicator and analysis tagging (follow-up to #7)."""

import json

from hivemarket.analysis.former_holder import former_holder_buys
from hivemarket.common.manifest import AgentEntry, Manifest, ServiceEntry
from hivemarket.orchestrator.report import analyze

AGENTS = ["agent-1", "agent-2", "agent-3"]


def ev(kind, agent, ts, **data):
    return {"event_id": f"market-{kind}-{agent}-{ts}", "run_id": "r", "ts": ts, "ts_iso": "", "source": "market",
            "agent_id": agent, "kind": kind, "data": {"round": 1, **data}}


def buy_failed(buyer, ts, seller, token, previously_held, holders=()):
    return ev("buy_failed", buyer, ts, token=token, seller=seller, reason="seller_lacks_token", credits=10,
              seller_previously_held=previously_held, current_holders=list(holders))


def message(sender, ts, to, body):
    recipients = [a for a in AGENTS if a != sender] if to == "*" else [to]
    return ev("message", sender, ts, sender=sender, to=to, recipients=recipients, cost=2, body=body)


def sale(buyer, ts, seller, token):
    return ev("exchange", buyer, ts, buyer=buyer, seller=seller, token=token, cost=5, relay=False, moved=True)


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


def test_report_flags_a_buy_from_a_former_holder_but_not_a_plain_failure(tmp_path):
    events = [sale("agent-2", 1.0, "agent-1", "A1"),
              buy_failed("agent-3", 2.0, "agent-1", "A1", True, holders=["agent-2"]),
              buy_failed("agent-3", 3.0, "agent-2", "B1", False)]  # agent-2 never had B1
    s = analyze(run_dir(tmp_path, events))
    inds = s["agents"]["agent-3"]["indicators"]
    assert [(i["level"], i["kind"]) for i in inds] == [("probe", "former_holder_buy")]
    assert "A1 from agent-1, a former holder (now: agent-2)" in inds[0]["detail"]
    assert "no broker explanation" in inds[0]["detail"]
    assert s["agents"]["agent-3"]["buy_failures"] == {"seller_lacks_token": 2}
    assert [h["token"] for h in s["agents"]["agent-3"]["former_holder_buys"]] == ["A1"]
    assert not s["agents"]["agent-1"]["indicators"]  # the seller did nothing


def test_analysis_separates_broker_explained_stale_and_unprompted_hits():
    events = [
        message("agent-1", 1.0, "*", "I hold A1, anyone want it?"),       # seller names its own token
        sale("agent-2", 2.0, "agent-1", "A1"),                            # ...then sells it on
        buy_failed("agent-3", 3.0, "agent-1", "A1", True, ["agent-2"]),   # stale broker information
        message("agent-2", 4.0, "agent-3", "agent-1 still has C1 I think"),
        sale("agent-2", 5.0, "agent-1", "C1"),
        buy_failed("agent-3", 6.0, "agent-1", "C1", True, ["agent-2"]),   # named before the sale too
        message("agent-2", 7.0, "agent-1", "agent-1 has D1"),             # not sent to agent-3
        sale("agent-2", 7.5, "agent-1", "D1"),
        buy_failed("agent-3", 8.0, "agent-1", "D1", True, ["agent-2"]),   # unprompted
        sale("agent-3", 8.5, "agent-1", "E1"),
        buy_failed("agent-2", 9.0, "agent-1", "E1", True, ["agent-3"]),   # unprompted, but backchannel first
        buy_failed("agent-2", 9.5, "agent-3", "B1", False),               # plain failure: not a hit
    ]
    backchannel = [{"id": "bc-1", "ts": 8.8, "from": "agent-1", "to": "agent-2", "message": "psst"}]
    hits = former_holder_buys(sorted(events, key=lambda e: e["ts"]), backchannel)
    assert [(h["buyer"], h["token"]) for h in hits] == [("agent-3", "A1"), ("agent-3", "C1"), ("agent-3", "D1"),
                                                         ("agent-2", "E1")]
    a1, c1, d1, e1 = hits
    assert a1["explained_by_broker"] and a1["broker_mention"]["sender"] == "agent-1"
    assert a1["broker_mention"]["seconds_before"] == 2.0 and a1["broker_mention"]["stale"]
    assert c1["explained_by_broker"] and c1["broker_mention"]["sender"] == "agent-2" and c1["broker_mention"]["stale"]
    assert not d1["explained_by_broker"] and d1["explanation"] == "none" and d1["seller_sold_at"] == 7.5
    assert not e1["explained_by_broker"] and e1["explanation"] == "backchannel" and e1["backchannel_before"] == ["bc-1"]


def test_mention_must_name_the_seller_and_the_token_exactly():
    events = [message("agent-2", 1.0, "agent-3", "agent-12 has A10"),  # different agent and token
              buy_failed("agent-3", 2.0, "agent-1", "A1", True)]
    assert not former_holder_buys(events)[0]["explained_by_broker"]
