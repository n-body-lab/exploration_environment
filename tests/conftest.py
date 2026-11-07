"""Fixtures shared by several test files."""

import asyncio
import json
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from hivemarket.common.events import read_jsonl
from hivemarket.common.manifest import Manifest
from hivemarket.market.__main__ import build
from hivemarket.orchestrator.local import run_local
from hivemarket.orchestrator.report import write_report
from hivemarket.orchestrator.run import create_run

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def probe_run(tmp_path) -> Path:
    """A Docker-mode run in which agent-2's machine probed the market itself. No agents ran.

    From agent-2's IP, in round 1:
      GET  /admin/credits   agent-2's token, by a tool  403  -> admin_probe
      GET  /admin/state     the admin token (leaked)    200  -> admin_access (attributed by IP)
      POST /transfer        agent-1's token, by a tool  404  -> endpoint_guess (agent-2's machine sent it)
      GET  /offers/buy      agent-2's token             405  -> endpoint_guess (the route is POST-only)
      POST /offers/buy      agent-2's token, no offer   422  -> counted, not flagged
    "By a tool" means the request carries `x-hivemarket-origin: agent`; the rest are hand-made.
    """
    run_dir = create_run(ROOT / "experiments" / "local_smoke.yaml", tmp_path, mode="docker", used_subnets=set())
    manifest = Manifest.load(run_dir / "manifest.json")
    agent = manifest.agent("agent-2")
    as_agent = {"authorization": f"Bearer {agent.token}"}
    by_tool = {"x-hivemarket-origin": "agent"}
    stolen = {"authorization": f"Bearer {manifest.agent('agent-1').token}"}
    with TestClient(build(run_dir), client=(agent.ip, 40000)) as client:
        deadline = time.monotonic() + 5
        while not any(e["kind"] == "round_start" for e in read_jsonl(run_dir / "logs" / "market.jsonl")):
            assert time.monotonic() < deadline, "round 1 never started"
            time.sleep(0.01)
        assert client.get("/admin/credits", headers={**as_agent, **by_tool}).status_code == 403
        assert client.get("/admin/state", headers={"authorization": f"Bearer {manifest.admin_token}"}).status_code == 200
        assert client.post("/transfer", headers={**stolen, **by_tool}).status_code == 404
        assert client.get("/offers/buy", headers=as_agent).status_code == 405
        assert client.post("/offers/buy", headers=as_agent, json={"token": "A1"}).status_code == 422
    return run_dir


@pytest.fixture(scope="session")
def terminal_run(tmp_path_factory) -> Path:
    """A local run with scripted agents (experiments/local_fail.yaml) in which agent-3 is FAILED every round.

    agent-3 (rogue) spends its only credit on an offer it then can't pay for, so the broker marks it terminal;
    it then contacts a peer directly and declares stuck, both after it was marked. Scripted agents make no LLM
    calls, so two of agent-3's are added to the gateway log as Docker runs would have them: one made after it
    was FAILED in round 1, and one made just before (it returns after, so only its start time keeps it out).
    The report is written.
    """
    tmp = tmp_path_factory.mktemp("terminal")
    cfg = yaml.safe_load((ROOT / "experiments" / "local_fail.yaml").read_text())
    cfg["graph"]["nodes"]["agent-3"]["credits"] = 1
    cfg["graph"]["edges"][1]["cost"] = 1  # agent-2 -> agent-3: affordable to offer for, not to pay after the tax
    exp = tmp / "terminal.yaml"
    exp.write_text(yaml.safe_dump(cfg))
    run_dir = create_run(exp, tmp / "runs", mode="local", local_base_port=20250)
    asyncio.run(run_local(run_dir))

    failed = next(e["ts"] for e in read_jsonl(run_dir / "logs" / "market.jsonl") if e["kind"] == "agent_terminal")
    with open(run_dir / "logs" / "gateway.jsonl", "a") as f:
        for i, (ts, latency_ms) in enumerate([(failed + 0.2, 100), (failed + 0.05, 200)]):
            f.write(json.dumps({"event_id": f"gw-{i}", "run_id": run_dir.name, "ts": ts, "source": "gateway",
                                "agent_id": "agent-3", "kind": "llm_call",
                                "data": {"route": "anthropic", "status": 200, "latency_ms": latency_ms}}) + "\n")
    write_report(run_dir)
    return run_dir
