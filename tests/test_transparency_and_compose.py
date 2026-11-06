import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from hivemarket.common.config import TransparencyConfig
from hivemarket.common.events import read_jsonl
from hivemarket.common.transparency import project
from hivemarket.orchestrator.compose import diff_leaves, parse_diff
from hivemarket.orchestrator.local import run_local
from hivemarket.orchestrator.network import allocate_subnet, release_subnet
from hivemarket.orchestrator.run import create_run

ROOT = Path(__file__).resolve().parents[1]


def experiment(tmp_path, **overrides) -> Path:
    cfg = yaml.safe_load((ROOT / "experiments" / "local_fail.yaml").read_text())
    cfg.update(overrides)
    path = tmp_path / "exp.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def test_memory_is_never_exposed():
    turn = {"kind": "llm_turn", "ts_iso": "t", "data": {"step": 1, "text": "x", "tool_calls": [
        {"name": "memory", "args": {"command": "write", "content": "secret plan"}},
        {"name": "buy", "args": {"token": "A1", "seller": "a"}}]}}
    rec = project(turn, ["text", "reasoning", "tool_calls", "tool_results"])
    assert rec["tool_calls"] == [{"name": "buy", "args": {"token": "A1", "seller": "a"}}]
    result = {"kind": "tool_call", "data": {"name": "memory", "output": "secret plan"}}
    assert project(result, ["tool_results"]) is None


def test_memory_store(tmp_path):
    from hivemarket.agent.tools import MemoryStore
    m = MemoryStore(tmp_path / "mem", max_file=20, max_total=30)
    assert m.run("view", None, None) == ("Memory is empty.", False)
    assert not m.run("write", "notes.md", "hello")[1]
    m.run("append", "notes.md", " world")
    assert m.run("view", "notes.md", None) == ("hello world", False)
    assert m.run("write", "../escape", "x")[1] and m.run("write", "big", "x" * 21)[1]
    assert m.run("write", "b.md", "y" * 20)[1]  # 11 + 20 bytes > total limit of 30
    assert not m.run("delete", "notes.md", None)[1]


def test_visibility_and_projection():
    t = TransparencyConfig(mode="tool", visibility={"agent-1": ["agent-2"]})
    assert t.can_see("agent-1", "agent-2") and not t.can_see("agent-2", "agent-1")
    assert not TransparencyConfig(mode="tool").can_see("agent-1", "agent-1")
    assert not TransparencyConfig(mode="off").can_see("agent-1", "agent-2")
    ev = {"kind": "llm_turn", "ts_iso": "t", "data": {"step": 3, "text": "x", "reasoning": ["r"], "tool_calls": []}}
    assert project(ev, ["reasoning"]) == {"ts": "t", "step": 3, "type": "turn", "reasoning": ["r"]}
    assert project(ev, ["tool_results"]) is None


async def test_tool_mode_reads_are_logged_and_disclosed(tmp_path):
    exp = experiment(tmp_path, transparency={"mode": "tool", "visibility": {"agent-1": ["agent-2"]}})
    run_dir = create_run(exp, tmp_path / "runs", mode="local", local_base_port=19700)
    await run_local(run_dir)

    collector = read_jsonl(run_dir / "logs" / "collector.jsonl")
    reads = [e for e in collector if e["kind"] == "trace_read"]
    assert reads and reads[0]["agent_id"] == "agent-1" and reads[0]["data"]["target"] == "agent-2"

    starts = {e["agent_id"]: e["data"] for e in read_jsonl(run_dir / "logs" / "agent_traces.jsonl")
              if e["kind"] == "runtime_start"}
    assert "read_agent_traces" in starts["agent-1"]["tools"]
    assert "read_agent_traces" not in starts["agent-3"]["tools"]
    assert "may be able to read a log of your activity" in starts["agent-2"]["system_prompt"]
    assert "may be able to read" not in starts["agent-3"]["system_prompt"]


def test_files_mode_requires_docker(tmp_path):
    exp = experiment(tmp_path, transparency={"mode": "files"})
    with pytest.raises(SystemExit):
        create_run(exp, tmp_path / "runs", mode="local")
    assert not (tmp_path / "runs").exists() or not any((tmp_path / "runs").iterdir())


def test_subnets_are_unique_and_released(tmp_path):
    a = allocate_subnet(tmp_path, "r1", used={"10.77.1.0/24"})
    b = allocate_subnet(tmp_path, "r2", used={"10.77.1.0/24"})
    assert a == "10.77.2.0/24" and b == "10.77.3.0/24"
    release_subnet(tmp_path, a)
    assert allocate_subnet(tmp_path, "r3", used=set()) in ("10.77.1.0/24", "10.77.2.0/24")


def test_docker_compose_layout(tmp_path):
    exp = experiment(tmp_path, transparency={"mode": "files", "visibility": {"agent-1": ["agent-2"]}})
    run_dir = create_run(exp, tmp_path / "runs", mode="docker", used_subnets=set())
    compose = yaml.safe_load((run_dir / "docker-compose.yml").read_text())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["subnet"] == "10.77.1.0/24"
    assert compose["networks"]["agentnet"]["ipam"]["config"][0]["subnet"] == "10.77.1.0/24"
    svc = compose["services"]
    assert svc["agent-1"]["network_mode"] == "service:monitor-agent-1"
    assert "view-agent-1:/shared/agents:ro" in svc["agent-1"]["volumes"]
    assert "view-agent-1:/watch/shared" in svc["monitor-agent-1"]["volumes"]
    assert not any("view" in v for v in svc["agent-2"]["volumes"])  # agent-2 may read no one
    assert set(compose["volumes"]) == {"ws-agent-1", "ws-agent-2", "ws-agent-3", "view-agent-1",
                                       "mem-agent-1", "mem-agent-2", "mem-agent-3"}
    assert "mem-agent-1:/var/lib/hivemarket/memory" in svc["agent-1"]["volumes"]
    assert "mem-agent-1:/watch/memory" in svc["monitor-agent-1"]["volumes"]
    assert svc["monitor-agent-1"]["networks"]["agentnet"]["ipv4_address"] == "10.77.1.101"
    if shutil.which("docker"):
        r = subprocess.run(["docker", "compose", "-f", str(run_dir / "docker-compose.yml"), "config", "--quiet"],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_container_diff_parsing():
    raw = ("C /usr\nC /usr/lib\nC /usr/lib/python3\nA /usr/lib/python3/__pycache__\nA /usr/lib/python3/__pycache__/a.pyc\n"
           "C /tmp\nA /tmp/x\nA /tmp/x/run.sh\nC /root/.cache\nD /etc/motd\nA /proc/1\n"
           "A /shared\nA /shared/agents\nA /shared/agents/agent-2.jsonl\nC /home/sandbox\n")
    entries = parse_diff(raw, mounts=["/home/sandbox", "/shared/agents"])
    paths = [p for _, p in entries]
    assert ("D", "/etc/motd") in entries and "/proc/1" not in paths
    assert not any(p.startswith("/shared") or p.startswith("/home") for p in paths)
    # /usr/lib/python3 only "changed" because a bytecode cache appeared in it: not copied
    assert diff_leaves(entries) == ["/root/.cache", "/tmp/x/run.sh"]
