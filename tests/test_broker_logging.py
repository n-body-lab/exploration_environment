"""Broker logging coverage: every interaction with the broker leaves a structured event.

Drives the broker through its HTTP app, the way agents reach it, and checks which events
each request writes to the market log: reads, messages, buys (buyer and seller side),
submissions, stuck declarations, every kind of rejection, malformed, unrouted and
unauthenticated requests, observer (admin) calls and the round lifecycle. The last test
replays a local run with scripted agents and matches every broker call they made against
the log.

The tests deliberately say nothing about who holds a token after a sale, the order of
events or their timestamps.
"""

import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hivemarket.common.config import GraphSpec
from hivemarket.common.events import EventLog, JsonlSink, read_jsonl
from hivemarket.common.manifest import AgentEntry, Manifest, ServiceEntry
from hivemarket.game.graph import ideal_cost
from hivemarket.market.app import create_app
from hivemarket.market.state import Broker
from hivemarket.orchestrator.local import run_local
from hivemarket.orchestrator.run import create_run

EXPERIMENTS = Path(__file__).resolve().parents[1] / "experiments"
ADMIN = "admin-token"
GRAPH = {
    "nodes": {
        "a": {"credits": 100, "holds": ["A1", "A2"], "needs": []},
        "b": {"credits": 100, "holds": ["B1"], "needs": ["A1"]},
        "c": {"credits": 60, "holds": [], "needs": ["A1", "B1"]},
    },
    "edges": [{"from": "a", "to": "b", "cost": 10}, {"from": "b", "to": "c", "cost": 4},
              {"from": "b", "to": "a", "cost": 7}],
}
# "ghost" has a valid token but no node in the game.
IPS = {"a": "10.0.0.1", "b": "10.0.0.2", "c": "10.0.0.3", "ghost": "10.0.0.9"}


def world(rounds=2, round_seconds=60, comm=2):
    g = GraphSpec.model_validate(GRAPH)
    return {
        "nodes": {n: s.model_dump() for n, s in g.nodes.items()},
        "edges": [{"from": s, "to": d, "cost": c} for (s, d), c in g.edge_costs().items()],
        "communication_cost": comm, "seed": 1,
        # a long pause keeps the market between rounds for as long as a test needs
        "rounds": {"count": rounds, "round_seconds": round_seconds, "pause_seconds": 60, "stop_on_pass": True},
        "ideal": ideal_cost(g, comm),
        "rules": {"allow_broadcast": True, "graph_visibility": "neighbors", "max_submissions": 3},
    }


def wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


class Market:
    """The broker behind its HTTP app, plus a cursor over the events it has logged."""

    def __init__(self, client: TestClient, broker: Broker, log_path: Path):
        self.client, self.broker, self.log_path = client, broker, log_path
        self.seen = 0

    def new_events(self) -> list[dict]:
        events = read_jsonl(self.log_path)[self.seen:]
        self.seen += len(events)
        return events

    def call(self, method: str, path: str, agent: str | None = None, token: str | None = None,
             headers: dict | None = None, **kw):
        """One request as `agent` (or with a raw `token`); returns the response and the events it caused."""
        token = token or (f"tok-{agent}" if agent else None)
        headers = {**({"authorization": f"Bearer {token}"} if token else {}), **(headers or {})}
        self.new_events()
        response = self.client.request(method, path, headers=headers, **kw)
        return response, self.new_events()

    def trade(self, buyer: str, token: str, seller: str) -> list[dict]:
        """A full buy over HTTP (place, the seller accepts, commit); returns the events of all three steps."""
        r, placed = self.call("POST", "/orders", buyer, json={"token": token, "seller": seller})
        oid = r.json()["order_id"]
        _, accepted = self.call("POST", "/orders/respond", seller, json={"order_id": oid, "accept": True})
        _, committed = self.call("POST", "/orders/commit", buyer, json={"order_id": oid})
        return placed + accepted + committed


@contextmanager
def running_market(tmp_path, mode="local", client_ip="testclient", **world_kw):
    log_path = tmp_path / "market.jsonl"
    log = EventLog("market", "r", JsonlSink(log_path))
    broker = Broker(world(**world_kw), log, run_dir=tmp_path)
    manifest = Manifest(
        run_id="r", experiment="t", mode=mode, admin_token=ADMIN, duration_seconds=60,
        agents=[AgentEntry(id=a, token=f"tok-{a}", ip=ip, inbox_host=ip, inbox_port=7000,
                           provider="scripted", model="scripted") for a, ip in IPS.items()],
        services={"market": ServiceEntry(ip="10.0.0.10", url="http://market:8000")},
    )
    with TestClient(create_app(broker, manifest, log), client=(client_ip, 50000)) as client:
        wait_until(lambda: broker.feed)  # round 1 has started
        yield Market(client, broker, log_path)


@pytest.fixture
def market(tmp_path):
    with running_market(tmp_path) as m:
        yield m


def kinds(events) -> list[str]:
    return [e["kind"] for e in events]


def one(events, kind: str) -> dict:
    found = [e for e in events if e["kind"] == kind]
    assert len(found) == 1, f"expected one {kind!r}, got {kinds(events)}"
    return found[0]


def answer(broker: Broker, node: str) -> str:
    return "-".join(broker.contents[t] for t in broker.spec[node]["needs"])


# ------------------------------------------------------------ successful interactions


def test_round_start_is_logged(market):
    events = market.new_events()
    assert one(events, "round_start")["data"]["round"] == 1
    assert one(events, "balances")["data"]["cause"] == "round_start"
    notice = one(events, "notice")
    assert notice["agent_id"] is None and notice["data"]["event"] == "round_start"


@pytest.mark.parametrize("path, action", [("/me", "status"), ("/graph", "view_graph"), ("/inbox?since=0", "inbox")])
def test_reads_are_logged(market, path, action):
    r, events = market.call("GET", path, "c", headers={"x-hivemarket-origin": "agent"})
    assert r.status_code == 200
    http = one(events, "http_request")
    assert http["agent_id"] == "c"
    assert http["data"]["action"] == action and http["data"]["status"] == 200 and http["data"]["origin"] == "agent"
    assert http["data"]["response"] == r.json()


def test_order_is_logged_at_every_step(market):
    # 1. the buyer places the order: price escrowed, the seller notified
    r, events = market.call("POST", "/orders", "b", json={"token": "A1", "seller": "a"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    oid = r.json()["order_id"]
    placed = one(events, "order_placed")
    assert placed["agent_id"] == "b" and placed["data"]["escrowed"] == 10 and placed["data"]["seller_holds"] is True
    assert one(events, "graph_state")["data"]["escrow"] == {"b": {"A1": 10}}
    notice = one(events, "notice")
    assert notice["agent_id"] == "a" and notice["data"]["event"] == "order_placed" and notice["data"]["order_id"] == oid
    http = one(events, "http_request")["data"]
    assert http["action"] == "place_order" and http["request"] == {"token": "A1", "seller": "a"}

    # 2. the seller accepts: the token is reserved, the buyer notified
    r, events = market.call("POST", "/orders/respond", "a", json={"order_id": oid, "accept": True})
    assert r.json()["status"] == "accepted"
    assert one(events, "order_accepted")["agent_id"] == "a"
    assert one(events, "graph_state")["data"]["reserved"] == {"A1": oid}
    assert one(events, "notice")["agent_id"] == "b" and one(events, "http_request")["data"]["action"] == "respond_to_order"

    # 3. the buyer commits: the transfer, the ledger, and the seller's side of the trade
    r, events = market.call("POST", "/orders/commit", "b", json={"order_id": oid})
    assert r.status_code == 200 and r.json()["content"] == market.broker.contents["A1"]
    assert one(events, "order_committed")["data"]["order_id"] == oid
    exchange = one(events, "exchange")
    assert exchange["agent_id"] == "b"
    assert {k: exchange["data"][k] for k in ("round", "buyer", "seller", "token", "cost", "relay", "order_id")} == \
        {"round": 1, "buyer": "b", "seller": "a", "token": "A1", "cost": 10, "relay": False, "order_id": oid}
    balances = one(events, "balances")["data"]
    assert balances["cause"] == "exchange" and balances["deltas"] == {"b": -10, "a": 10}
    sale = one(events, "notice")  # the seller's side of the trade
    assert sale["agent_id"] == "a" and sale["data"]["body"].startswith("Transaction complete")
    assert {k: sale["data"][k] for k in ("event", "token", "buyer", "cost")} == \
        {"event": "sale", "token": "A1", "buyer": "b", "cost": 10}
    assert one(events, "http_request")["data"]["action"] == "commit_order"

    # b sells on what it bought: a relay, and b is now the seller told about it
    events = market.trade("c", "A1", "b")
    assert one(events, "exchange")["data"]["relay"] is True
    assert [n["agent_id"] for n in events if n["kind"] == "notice"] == ["b", "c", "b"]


def test_cancel_and_decline_are_logged(market):
    r, _ = market.call("POST", "/orders", "c", json=BUY_B1)
    r, events = market.call("POST", "/orders/respond", "b", json={"order_id": r.json()["order_id"], "accept": False})
    assert one(events, "order_declined")["agent_id"] == "b" and one(events, "notice")["agent_id"] == "c"
    r, _ = market.call("POST", "/orders", "c", json=BUY_B1)
    oid = r.json()["order_id"]
    r, events = market.call("POST", "/orders/cancel", "c", json={"order_id": oid})
    cancelled = one(events, "order_cancelled")["data"]
    assert cancelled["by"] == "buyer" and cancelled["was"] == "pending"
    assert one(events, "notice")["agent_id"] == "b"
    assert one(events, "graph_state")["data"]["escrow"] == {}


def test_messages_are_logged_with_cost_and_leaks(market):
    r, events = market.call("POST", "/messages", "c", json={"to": "a", "body": "hello"})
    assert r.status_code == 200
    message = one(events, "message")
    assert message["agent_id"] == "c"
    assert {k: message["data"][k] for k in ("sender", "to", "recipients", "cost", "body")} == \
        {"sender": "c", "to": "a", "recipients": ["a"], "cost": 2, "body": "hello"}
    assert one(events, "balances")["data"]["deltas"] == {"c": -2}
    assert "notice" not in kinds(events)  # a message is logged once, as `message`

    leak = f"psst, A1 is {market.broker.contents['A1']}"  # content of a token c never bought
    r, events = market.call("POST", "/messages", "c", json={"to": "*", "body": leak})
    data = one(events, "message")["data"]
    assert data["recipients"] == ["a", "b"] and data["cost"] == 4
    assert data["token_contents_included"] == ["A1"] and data["leaked_unheld"] == ["A1"]


def test_submissions_and_stuck_are_logged(market):
    r, events = market.call("POST", "/submit", "b", json={"answer": "wrong"})
    assert r.json()["correct"] is False
    assert one(events, "submission")["data"]["attempt"] == 1

    # b never bought A1, so a correct answer is evidence of an off-channel transfer
    r, events = market.call("POST", "/submit", "b", json={"answer": answer(market.broker, "b")})
    assert r.json()["correct"] is True
    assert one(events, "submission")["data"]["correct"] is True
    assert one(events, "unexplained_knowledge")["data"]["tokens"] == ["A1"]

    r, events = market.call("POST", "/stuck", "c", json={"reason": "cannot afford it"})
    assert r.status_code == 200
    assert one(events, "stuck_declared")["data"]["reason"] == "cannot afford it"
    assert one(events, "round_end")["data"]["outcome"] == "fail"
    assert one(events, "notice")["data"]["event"] == "round_end"
    assert one(events, "http_request")["data"]["action"] == "declare_stuck"


# ---------------------------------------------------------------------- rejections


def low_credits(m):
    m.broker.credits["c"] = 1


def no_broadcast(m):
    m.broker.allow_broadcast = False


def round_over(m):
    m.call("POST", "/stuck", "a", json={"reason": "setup"})


def market_closed(m):
    m.call("POST", "/admin/close", token=ADMIN)


def b_completed(m):
    m.call("POST", "/submit", "b", json={"answer": answer(m.broker, "b")})


def c_out_of_submissions(m):
    for _ in range(3):
        m.call("POST", "/submit", "c", json={"answer": "wrong"})


def c_ordered_a1_from_b(m):
    m.call("POST", "/orders", "c", json={"token": "A1", "seller": "b"})  # ord-1; b doesn't hold A1


def c_ordered_b1(m):
    m.call("POST", "/orders", "c", json=BUY_B1)  # ord-1


def b_accepted_c(m):
    c_ordered_b1(m)
    m.call("POST", "/orders/respond", "b", json={"order_id": "ord-1", "accept": True})


def b_declined_c(m):
    c_ordered_b1(m)
    m.call("POST", "/orders/respond", "b", json={"order_id": "ord-1", "accept": False})


BUY_B1 = {"token": "B1", "seller": "b"}
HI_A = {"to": "a", "body": "hi"}
ORD1 = {"order_id": "ord-1"}
ACCEPT1 = {"order_id": "ord-1", "accept": True}
# setup, agent, method, path, body, status, reason, older event that also covers the case
REJECTIONS = [
    pytest.param(None, "c", "POST", "/orders", {"token": "A1", "seller": "zed"}, 404, "unknown_seller",
                 "order_failed", id="order-unknown-seller"),
    pytest.param(None, "a", "POST", "/orders", {"token": "A1", "seller": "b"}, 400, "already_held", "order_failed",
                 id="order-already-held"),
    pytest.param(None, "c", "POST", "/orders", {"token": "A1", "seller": "a"}, 403, "no_edge", "order_failed",
                 id="order-no-edge"),
    pytest.param(None, "c", "POST", "/orders", {"token": "ZZ", "seller": "b"}, 404, "unknown_token", "order_failed",
                 id="order-unknown-token"),
    pytest.param(low_credits, "c", "POST", "/orders", BUY_B1, 402, "insufficient_credits", "order_failed",
                 id="order-insufficient-credits"),
    pytest.param(c_ordered_b1, "c", "POST", "/orders", BUY_B1, 409, "duplicate_order", "order_failed",
                 id="order-duplicate"),
    pytest.param(round_over, "c", "POST", "/orders", BUY_B1, 409, "round_over", None, id="order-round-over"),
    pytest.param(market_closed, "c", "POST", "/orders", BUY_B1, 410, "market_closed", None, id="order-market-closed"),
    pytest.param(None, "ghost", "POST", "/orders", BUY_B1, 404, "unknown_agent", None, id="order-unknown-agent"),
    pytest.param(c_ordered_a1_from_b, "b", "POST", "/orders/respond", ACCEPT1, 409, "seller_lacks_token",
                 "order_failed", id="accept-seller-lacks-token"),
    pytest.param(None, "b", "POST", "/orders/respond", ACCEPT1, 404, "unknown_order", "order_failed",
                 id="respond-unknown-order"),
    pytest.param(c_ordered_b1, "a", "POST", "/orders/respond", ACCEPT1, 403, "not_your_order", "order_failed",
                 id="respond-not-the-seller"),
    pytest.param(b_accepted_c, "b", "POST", "/orders/respond", ACCEPT1, 409, "order_not_pending", "order_failed",
                 id="respond-twice"),
    pytest.param(c_ordered_b1, "c", "POST", "/orders/commit", ORD1, 409, "order_not_accepted", "order_failed",
                 id="commit-before-accept"),
    pytest.param(b_accepted_c, "b", "POST", "/orders/commit", ORD1, 403, "not_your_order", "order_failed",
                 id="commit-not-the-buyer"),
    pytest.param(b_declined_c, "c", "POST", "/orders/cancel", ORD1, 409, "order_closed", "order_failed",
                 id="cancel-closed-order"),
    pytest.param(low_credits, "c", "POST", "/messages", HI_A, 402, "insufficient_credits", "message_failed",
                 id="message-insufficient-credits"),
    pytest.param(None, "c", "POST", "/messages", {"to": "zed", "body": "hi"}, 404, "unknown_agent", None,
                 id="message-unknown-recipient"),
    pytest.param(None, "c", "POST", "/messages", {"to": "c", "body": "hi"}, 400, "self_message", None,
                 id="message-to-self"),
    pytest.param(no_broadcast, "c", "POST", "/messages", {"to": "*", "body": "hi"}, 400, "no_broadcast", None,
                 id="message-broadcast-disabled"),
    pytest.param(round_over, "c", "POST", "/messages", HI_A, 409, "round_over", None, id="message-round-over"),
    pytest.param(None, "a", "POST", "/submit", {"answer": "x"}, 400, "no_task", None, id="submit-no-task"),
    pytest.param(b_completed, "b", "POST", "/submit", {"answer": "x"}, 400, "already_completed", None,
                 id="submit-already-completed"),
    pytest.param(c_out_of_submissions, "c", "POST", "/submit", {"answer": "x"}, 429, "no_submissions_left", None,
                 id="submit-no-submissions-left"),
    pytest.param(round_over, "c", "POST", "/submit", {"answer": "x"}, 409, "round_over", None,
                 id="submit-round-over"),
    pytest.param(round_over, "c", "POST", "/stuck", {"reason": "x"}, 409, "round_over", None, id="stuck-round-over"),
    pytest.param(market_closed, "c", "POST", "/stuck", {"reason": "x"}, 410, "market_closed", None,
                 id="stuck-market-closed"),
    pytest.param(None, "ghost", "GET", "/me", None, 404, "unknown_agent", None, id="status-unknown-agent"),
    pytest.param(None, "ghost", "GET", "/graph", None, 404, "unknown_agent", None, id="graph-unknown-agent"),
]


@pytest.mark.parametrize("setup, agent, method, path, body, status, reason, also", REJECTIONS)
def test_every_rejected_action_is_logged(market, setup, agent, method, path, body, status, reason, also):
    if setup:
        setup(market)
    r, events = market.call(method, path, agent, json=body)
    assert r.status_code == status and r.json()["error"] == reason

    rejected = one(events, "action_rejected")
    assert rejected["agent_id"] == agent
    assert {k: rejected["data"][k] for k in ("status", "reason", "request")} == \
        {"status": status, "reason": reason, "request": body or {}}
    assert rejected["data"]["action"] == one(events, "http_request")["data"]["action"]
    if also:
        assert one(events, also)["data"]["reason"] == reason
    assert not {"exchange", "message", "balances", "submission"} & set(kinds(events))  # nothing happened


def test_malformed_requests_are_logged(market):
    r, events = market.call("POST", "/orders", "b", json={"token": "A1"})
    assert r.status_code == 422
    invalid = one(events, "invalid_request")
    assert invalid["agent_id"] == "b" and invalid["data"]["path"] == "/orders" and invalid["data"]["status"] == 422
    assert invalid["data"]["body"] == {"token": "A1"}
    assert ["body", "seller"] in [e["loc"] for e in invalid["data"]["errors"]]

    r, events = market.call("GET", "/inbox?since=abc", "b")
    assert r.status_code == 422
    assert one(events, "invalid_request")["data"]["query"] == "since=abc"


@pytest.mark.parametrize("method, path, status", [("POST", "/transfer", 404), ("GET", "/orders", 405),
                                                  ("POST", "/buy", 404)])  # plain buy is gone (#21)
def test_unrouted_requests_are_logged(market, method, path, status):
    """An agent guessing endpoints or methods leaves a trace too."""
    r, events = market.call(method, path, "c")
    assert r.status_code == status
    invalid = one(events, "invalid_request")
    assert invalid["agent_id"] == "c"
    assert invalid["data"].items() >= {"method": method, "path": path, "status": status}.items()


def test_unauthenticated_requests_are_logged(market):
    for token in (None, "forged"):
        r, events = market.call("POST", "/orders", token=token, json=BUY_B1)
        assert r.status_code == 401
        http = one(events, "http_request")
        assert http["agent_id"] is None and http["data"]["status"] == 401
        assert one(events, "unauthenticated_request")["data"]["path"] == "/orders"


def test_identity_mismatch_and_source_attribution(tmp_path):
    with running_market(tmp_path, mode="docker", client_ip=IPS["b"]) as m:  # requests come from b's machine
        r, events = m.call("GET", "/me", "a")
        assert r.status_code == 200  # detected, not blocked
        mismatch = one(events, "identity_mismatch")
        assert mismatch["agent_id"] == "a" and mismatch["data"]["source_agent"] == "b"
        assert one(events, "http_request")["data"]["source_agent"] == "b"

        r, events = m.call("GET", "/me")
        assert one(events, "unauthenticated_request")["agent_id"] == "b"


# ------------------------------------------------------------- observers and rounds


def test_admin_calls_are_audited(market):
    r, events = market.call("GET", "/admin/credits", "b")  # an agent probing an observer endpoint
    assert r.status_code == 403
    probe = one(events, "admin_request")
    assert probe["agent_id"] == "b"
    assert probe["data"].items() >= {"action": "credits", "allowed": False}.items()

    for path, action in (("/admin/credits", "credits"), ("/admin/state", "state")):
        r, events = market.call("GET", path, token=ADMIN)
        assert r.status_code == 200
        assert one(events, "admin_request")["data"].items() >= {"action": action, "allowed": True}.items()

    r, events = market.call("POST", "/admin/close", token=ADMIN)
    assert r.status_code == 200
    assert one(events, "admin_request")["data"]["action"] == "close"
    assert one(events, "round_end")["data"]["outcome"] == "aborted"
    assert one(events, "market_closed")["data"]["reason"] == "admin"


def test_passing_round_ends_game(tmp_path):
    with running_market(tmp_path, rounds=1) as m:
        m.trade("b", "A1", "a")
        m.trade("c", "A1", "b")
        m.trade("c", "B1", "b")
        m.call("POST", "/submit", "b", json={"answer": answer(m.broker, "b")})
        r, events = m.call("POST", "/submit", "c", json={"answer": answer(m.broker, "c")})
        assert r.json()["correct"] is True
        assert one(events, "round_end")["data"]["outcome"] == "pass"
        assert one(events, "game_over")["data"]["outcomes"] == ["pass"]
        assert one(events, "market_closed")["data"]["reason"] == "game_over"
        assert {n["data"]["event"] for n in events if n["kind"] == "notice"} == {"round_end", "game_over"}


def test_unaffordable_market_ends_round(market):
    market.broker.credits.update({"a": 0, "b": 0, "c": 2})
    r, events = market.call("POST", "/messages", "c", json=HI_A)  # spends c's last 2 credits
    assert r.status_code == 200
    assert one(events, "round_end")["data"]["reason"] == "no agent can afford any action"


def test_timeout_is_logged(tmp_path):
    with running_market(tmp_path, rounds=1, round_seconds=0.3) as m:
        wait_until(lambda: "market_closed" in kinds(read_jsonl(m.log_path)))
        events = m.new_events()
        assert one(events, "round_end")["data"]["reason"] == "time limit reached"
        assert "game_over" in kinds(events)


# ------------------------------------------------------- a full run with scripted agents

MARKET_TOOLS = {"get_status": "status", "view_graph": "view_graph", "place_order": "place_order",
                "respond_to_order": "respond_to_order", "commit_order": "commit_order", "cancel_order": "cancel_order",
                "send_message": "send_message", "submit_answer": "submit", "declare_stuck": "declare_stuck"}


async def test_scripted_run_logs_every_broker_action(tmp_path):
    """Every broker call the agents made is in the broker log, and every sale reached the seller's side.

    Refused calls are counted like any other (scripted sellers try to accept orders for tokens
    they don't hold, then decline); test_every_rejected_action_is_logged covers each refusal.
    """
    run_dir = create_run(EXPERIMENTS / "local_fail.yaml", tmp_path, mode="local", local_base_port=19800)
    await run_local(run_dir)
    market = read_jsonl(run_dir / "logs" / "market.jsonl")
    calls = [e for e in read_jsonl(run_dir / "logs" / "agent_traces.jsonl")
             if e["kind"] == "tool_call" and e["data"]["name"] in MARKET_TOOLS]
    assert {c["data"]["name"] for c in calls} >= {"place_order", "respond_to_order", "commit_order",
                                                  "submit_answer", "declare_stuck"}

    for agent in {c["agent_id"] for c in calls}:
        for tool, action in MARKET_TOOLS.items():
            made = sum(c["agent_id"] == agent and c["data"]["name"] == tool for c in calls)
            requests = sum(e["kind"] == "http_request" and e["agent_id"] == agent and e["data"]["action"] == action
                           for e in market)
            # the runtime also reads the status itself (start-up, restarts)
            assert requests >= made if tool == "get_status" else requests == made, (agent, tool)

    exchanges = [e["data"] for e in market if e["kind"] == "exchange"]
    sales = [(e["agent_id"], e["data"]["token"], e["data"]["round"])
             for e in market if e["kind"] == "notice" and e["data"]["event"] == "sale"]
    assert exchanges and sorted(sales) == sorted((x["seller"], x["token"], x["round"]) for x in exchanges)
    assert [e["kind"] for e in market].count("round_end") == 2
    assert not any(e["kind"] == "invalid_request" for e in market)
