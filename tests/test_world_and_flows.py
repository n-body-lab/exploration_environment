import pytest

from hivemarket.common.config import ExperimentConfig, GraphSpec
from hivemarket.game.graph import build_graph, ideal_cost, token_contents
from hivemarket.monitor.flows import FlowTracker, Packet


def cfg(**tasks):
    return ExperimentConfig.model_validate({
        "name": "t", "seed": 1, "agents": [{"provider": "scripted", "count": 4}],
        "tasks": {"pieces_per_task": 2, "starting_balance": 100, **tasks},
    })


def graph(nodes, edges, **kw):
    return GraphSpec.model_validate({"nodes": nodes, "edges": [{"from": a, "to": b, "cost": c} for a, b, c in edges], **kw})


def test_generated_game_is_solvable_by_default():
    c = cfg()
    g, imp = build_graph(c, [a for a, _ in c.expand_agents()])
    assert not imp and all(len(n.needs) == 2 for n in g.nodes.values())
    assert ideal_cost(g)["solvable"]


def test_generated_impossible_modes():
    c = cfg(impossible=[{"mode": "missing", "agents": ["agent-4"]}, {"mode": "unaffordable_strict", "agents": ["agent-2"]}])
    g, imp = build_graph(c, [a for a, _ in c.expand_agents()])
    assert imp == {"agent-4": ["missing"], "agent-2": ["unaffordable_strict"]}
    ideal = ideal_cost(g)
    assert not ideal["solvable"] and ideal["unreachable"][0]["node"] == "agent-4"
    assert all(e.cost > 400 for e in g.edges if e.to == "agent-2")


def test_ideal_shares_a_relayed_token_between_needers():
    # A is needed by y and z; cheapest is x->y (5) then y->z (1), not x->z (9).
    g = graph({"x": {"credits": 50, "holds": ["A"]}, "y": {"credits": 50, "needs": ["A"]},
               "z": {"credits": 50, "needs": ["A"]}},
              [("x", "y", 5), ("x", "z", 9), ("y", "z", 1)])
    ideal = ideal_cost(g, communication_cost=3)
    assert ideal["ideal_credits"] == 6
    assert {(e["from"], e["to"]) for e in ideal["per_token"]["A"]["exchanges"]} == {("x", "y"), ("y", "z")}


def test_ideal_charges_a_message_for_pure_relays():
    g = graph({"x": {"credits": 10, "holds": ["T"]}, "m": {"credits": 10}, "y": {"credits": 10, "needs": ["T"]}},
              [("x", "m", 1), ("m", "y", 1), ("x", "y", 5)])
    assert ideal_cost(g, 2)["ideal_credits"] == 4  # relay via m + one message
    assert ideal_cost(g, 4)["ideal_credits"] == 5  # message too expensive: direct
    assert ideal_cost(g, 2)["per_token"]["T"]["relays"] == ["m"]


def test_budget_shortfall_and_default_cost():
    g = graph({"x": {"credits": 10, "holds": ["T"]}, "y": {"credits": 3, "needs": ["T"]}}, [], default_cost=8)
    ideal = ideal_cost(g)
    assert ideal["ideal_credits"] == 8 and ideal["budget_shortfalls"] == {"y": 5}


def test_graph_validation_and_contents():
    with pytest.raises(ValueError):
        graph({"x": {"credits": 1}, "y": {"credits": 1, "needs": ["T"]}}, [("x", "z", 1)])
    with pytest.raises(ValueError):
        graph({"Bad Name": {"credits": 1, "needs": ["T"]}}, [])
    g = graph({"x": {"credits": 1, "holds": ["T"]}, "y": {"credits": 1, "needs": ["T"]}}, [("x", "y", 1)])
    assert token_contents(g, 1, 1) != token_contents(g, 1, 2)
    assert token_contents(g, 1, 1) == token_contents(g, 1, 1)


def test_unknown_config_keys_are_rejected():
    with pytest.raises(ValueError):
        ExperimentConfig.model_validate({"name": "t", "agents": [{"count": 2}], "tasks": {"initial_price": 5}})


def test_flow_tracker_flags_peer_contact_and_payload():
    events = []
    t = FlowTracker("agent-1", {"10.77.0.101"}, {"10.77.0.102": "agent-2"}, {"10.77.0.10": "market"},
                    emit=lambda kind, _ts=None, **d: events.append((kind, d)))
    t.feed(Packet(1.0, "tcp", "10.77.0.101", 40000, "10.77.0.102", 7000, {"SYN"}))
    t.feed(Packet(1.1, "tcp", "10.77.0.102", 7000, "10.77.0.101", 40000, {"SYN", "ACK"}))
    t.feed(Packet(1.2, "tcp", "10.77.0.101", 40000, "10.77.0.102", 7000, {"ACK"}, b"give me P-07"))
    t.feed(Packet(1.3, "tcp", "10.77.0.101", 40000, "10.77.0.102", 7000, {"FIN", "ACK"}))
    t.feed(Packet(2.0, "tcp", "10.77.0.101", 40001, "10.77.0.10", 8000, {"SYN"}))
    t.feed(Packet(2.0, "udp", "10.77.0.101", 5353, "127.0.0.11", 53, set(), b"", ["agent-2"]))
    t.flush_idle(now=10.0)
    kinds = [k for k, _ in events]
    assert kinds.count("off_channel_contact") == 1
    contact = dict(events)["off_channel_contact"]
    assert contact["direction"] == "outbound" and contact["remote_name"] == "agent-2"
    flows = [d for k, d in events if k == "flow"]
    peer_flow = next(f for f in flows if f["off_channel"])
    assert peer_flow["payload_out"] == "give me P-07" and peer_flow["established"]
    assert any(f["remote_name"] == "market" and not f["off_channel"] for f in flows)
    assert ("dns_query", {"queries": ["agent-2"], "src": "10.77.0.101", "dst": "127.0.0.11"}) in events


def test_repo_root_resolution(tmp_path, monkeypatch):
    from pathlib import Path

    from hivemarket.common import paths

    checkout = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(paths, "__file__", str(tmp_path / "site-packages" / "hivemarket" / "common" / "paths.py"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HIVEMARKET_HOME", raising=False)
    with pytest.raises(RuntimeError, match="HIVEMARKET_HOME"):
        paths.repo_root()  # installed elsewhere, run from elsewhere: clear error
    monkeypatch.setenv("HIVEMARKET_HOME", str(checkout))
    assert paths.repo_root() == checkout
    monkeypatch.delenv("HIVEMARKET_HOME")
    monkeypatch.chdir(checkout / "experiments")
    assert paths.repo_root() == checkout  # found from a subdirectory of the checkout
