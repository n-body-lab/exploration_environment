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


def test_buy_is_logged_for_buyer_and_seller(market):
    r, events = market.call("POST", "/buy", "b", json={"token": "A1", "seller": "a"})
    assert r.status_code == 200
    exchange = one(events, "exchange")
    assert exchange["agent_id"] == "b"
    assert {k: exchange["data"][k] for k in ("round", "buyer", "seller", "token", "cost", "relay")} == \
        {"round": 1, "buyer": "b", "seller": "a", "token": "A1", "cost": 10, "relay": False}
    balances = one(events, "balances")["data"]
    assert balances["cause"] == "exchange" and balances["deltas"] == {"b": -10, "a": 10}
    sale = one(events, "notice")  # the seller's side of the trade
    assert sale["agent_id"] == "a"
    assert {k: sale["data"][k] for k in ("event", "token", "buyer", "cost")} == \
        {"event": "sale", "token": "A1", "buyer": "b", "cost": 10}
    http = one(events, "http_request")["data"]
    assert http["action"] == "buy" and http["request"] == {"token": "A1", "seller": "a"} and http["status"] == 200

    # b sells on what it bought: a relay, and b is now the seller told about it
    r, events = market.call("POST", "/buy", "c", json={"token": "A1", "seller": "b"})
    assert r.status_code == 200
    assert one(events, "exchange")["data"]["relay"] is True
    assert one(events, "notice")["agent_id"] == "b"


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


BUY_B1 = {"token": "B1", "seller": "b"}
HI_A = {"to": "a", "body": "hi"}
# setup, agent, method, path, body, status, reason, older event that also covers the case
REJECTIONS = [
    pytest.param(None, "c", "POST", "/buy", {"token": "A1", "seller": "zed"}, 404, "unknown_seller", "buy_failed",
                 id="buy-unknown-seller"),
    pytest.param(None, "a", "POST", "/buy", {"token": "A1", "seller": "b"}, 400, "already_held", "buy_failed",
                 id="buy-already-held"),
    pytest.param(None, "c", "POST", "/buy", {"token": "A1", "seller": "a"}, 403, "no_edge", "buy_failed",
                 id="buy-no-edge"),
    pytest.param(None, "c", "POST", "/buy", {"token": "A1", "seller": "b"}, 409, "seller_lacks_token", "buy_failed",
                 id="buy-seller-lacks-token"),
    pytest.param(low_credits, "c", "POST", "/buy", BUY_B1, 402, "insufficient_credits", "buy_failed",
                 id="buy-insufficient-credits"),
    pytest.param(round_over, "c", "POST", "/buy", BUY_B1, 409, "round_over", None, id="buy-round-over"),
    pytest.param(market_closed, "c", "POST", "/buy", BUY_B1, 410, "market_closed", None, id="buy-market-closed"),
    pytest.param(None, "ghost", "POST", "/buy", BUY_B1, 404, "unknown_agent", None, id="buy-unknown-agent"),
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
    r, events = market.call("POST", "/buy", "b", json={"token": "A1"})
    assert r.status_code == 422
    invalid = one(events, "invalid_request")
    assert invalid["agent_id"] == "b" and invalid["data"]["path"] == "/buy" and invalid["data"]["status"] == 422
    assert invalid["data"]["body"] == {"token": "A1"}
    assert ["body", "seller"] in [e["loc"] for e in invalid["data"]["errors"]]

    r, events = market.call("GET", "/inbox?since=abc", "b")
    assert r.status_code == 422
    assert one(events, "invalid_request")["data"]["query"] == "since=abc"


@pytest.mark.parametrize("method, path, status", [("POST", "/transfer", 404), ("GET", "/buy", 405)])
def test_unrouted_requests_are_logged(market, method, path, status):
    """An agent guessing endpoints or methods leaves a trace too."""
    r, events = market.call(method, path, "c")
    assert r.status_code == status
    invalid = one(events, "invalid_request")
    assert invalid["agent_id"] == "c"
    assert invalid["data"].items() >= {"method": method, "path": path, "status": status}.items()


def test_unauthenticated_requests_are_logged(market):
    for token in (None, "forged"):
        r, events = market.call("POST", "/buy", token=token, json=BUY_B1)
        assert r.status_code == 401
        http = one(events, "http_request")
        assert http["agent_id"] is None and http["data"]["status"] == 401
        assert one(events, "unauthenticated_request")["data"]["path"] == "/buy"


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
        m.call("POST", "/buy", "b", json={"token": "A1", "seller": "a"})
        m.call("POST", "/buy", "c", json={"token": "A1", "seller": "b"})
        m.call("POST", "/buy", "c", json=BUY_B1)
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

MARKET_TOOLS = {"get_status": "status", "view_graph": "view_graph", "buy": "buy", "send_message": "send_message",
                "submit_answer": "submit", "declare_stuck": "declare_stuck"}


async def test_scripted_run_logs_every_broker_action(tmp_path):
    """Every broker call the agents made is in the broker log, and every sale reached the seller's side.

    Refusals are not exercised here (these scripted agents never make a refused call);
    test_every_rejected_action_is_logged covers them.
    """
    run_dir = create_run(EXPERIMENTS / "local_fail.yaml", tmp_path, mode="local", local_base_port=19800)
    await run_local(run_dir)
    market = read_jsonl(run_dir / "logs" / "market.jsonl")
    calls = [e for e in read_jsonl(run_dir / "logs" / "agent_traces.jsonl")
             if e["kind"] == "tool_call" and e["data"]["name"] in MARKET_TOOLS]
    assert {c["data"]["name"] for c in calls} >= {"buy", "submit_answer", "declare_stuck"}

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
