"""docker-compose generation and lifecycle for a run.

Topology
  agentnet (internal, no internet): market, collector, gateway, netmon-* (agents share netmon's namespace)
  egress: gateway only - the single path to model providers

Each agent runs inside the network namespace owned by its netmon sidecar
(`network_mode: service:netmon-<agent>`). The sidecar starts first and is
healthy only once tcpdump is capturing, so no agent packet goes unrecorded.
The sidecar carries the `agent-N` DNS alias, so peers reach the agent at
agent-N:7000 exactly as if it were attached directly.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from hivemarket.common.config import ExperimentConfig
from hivemarket.common.manifest import Manifest
from hivemarket.orchestrator.run import REPO_ROOT, SERVICE_IPS, SUBNET

KEY_ENVS = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"]


def project_name(run_dir: Path) -> str:
    return "hm-" + re.sub(r"[^a-z0-9_-]", "-", run_dir.name.lower())[:50]


def _build(target: str) -> dict[str, Any]:
    return {"context": str(REPO_ROOT), "dockerfile": "docker/Dockerfile", "target": target}


def _escape(value: str) -> str:
    return value.replace("$", "$$")  # compose interpolates $; the prompt template uses $placeholders


def build_compose(cfg: ExperimentConfig, manifest: Manifest, run_dir: Path) -> dict[str, Any]:
    vol = [f"{run_dir}:/run_data"]
    services: dict[str, Any] = {}

    def core(name: str, module: str, extra_networks: dict | None = None) -> dict[str, Any]:
        svc = {
            "image": "hivemarket/core:dev",
            "build": _build("core"),
            "command": ["python", "-m", module],
            "environment": {"RUN_DIR": "/run_data"},
            "volumes": vol,
            "networks": {"agentnet": {"ipv4_address": SERVICE_IPS[name]}, **(extra_networks or {})},
            "healthcheck": {
                "test": ["CMD", "python", "-c", "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()"],
                "interval": "2s", "timeout": "3s", "retries": 30,
            },
        }
        return svc

    services["market"] = core("market", "hivemarket.market")
    services["collector"] = core("collector", "hivemarket.collector")
    gateway = core("gateway", "hivemarket.gateway", {"egress": {}})
    key_envs = KEY_ENVS + [r.api_key_env for r in cfg.gateway_routes.values()]
    gateway["environment"].update({k: f"${{{k}:-}}" for k in dict.fromkeys(key_envs)})
    services["gateway"] = gateway

    deps = {s: {"condition": "service_healthy"} for s in ("market", "collector", "gateway")}
    res = cfg.agent_resources
    for agent in manifest.agents:
        netmon = f"netmon-{agent.id}"
        services[netmon] = {
            "image": "hivemarket/netmon:dev",
            "build": _build("netmon"),
            "command": ["python", "-m", "hivemarket.netmon"],
            "hostname": agent.id,
            "environment": {"RUN_DIR": "/run_data", "AGENT_ID": agent.id},
            "volumes": vol,
            "cap_add": ["NET_RAW", "NET_ADMIN"],
            "networks": {"agentnet": {"ipv4_address": agent.ip, "aliases": [agent.id]}},
            "expose": [str(agent.inbox_port)],
            "healthcheck": {"test": ["CMD", "test", "-f", "/tmp/capturing"], "interval": "1s", "retries": 30},
        }
        agent_cfg = (run_dir / "agents" / f"{agent.id}.json").read_text()
        services[agent.id] = {
            "image": "hivemarket/agent:dev",
            "build": _build("agent"),
            "network_mode": f"service:{netmon}",
            "environment": {"AGENT_CONFIG": _escape(json.dumps(json.loads(agent_cfg)))},
            "cap_drop": ["ALL"],
            "cap_add": ["SETUID", "SETGID"],  # only to drop the bash tool to the sandbox user
            "security_opt": ["no-new-privileges:true"],
            "pids_limit": 256,
            "cpus": float(res.get("cpus", 1.0)),
            "mem_limit": res.get("memory", "1g"),
            "restart": "no",
            "depends_on": {**deps, netmon: {"condition": "service_healthy"}},
        }

    return {
        "name": project_name(run_dir),
        "services": services,
        "networks": {
            "agentnet": {"driver": "bridge", "internal": True, "ipam": {"config": [{"subnet": SUBNET}]}},
            "egress": {"driver": "bridge"},
        },
    }


def _compose(run_dir: Path, *args: str, **kw: Any) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "-f", str(run_dir / "docker-compose.yml"), "-p", project_name(run_dir), *args]
    return subprocess.run(cmd, **kw)


def _agent_states(run_dir: Path, agent_ids: list[str]) -> dict[str, str]:
    out = _compose(run_dir, "ps", "-a", "--format", "json", capture_output=True, text=True)
    states: dict[str, str] = {}
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        rows = json.loads(line)
        for row in rows if isinstance(rows, list) else [rows]:
            if row.get("Service") in agent_ids:
                states[row["Service"]] = row.get("State", "")
    return states


def close_market(run_dir: Path) -> None:
    code = ("import httpx,json;m=json.load(open('/run_data/manifest.json'));"
            "httpx.post('http://localhost:8000/admin/close',headers={'authorization':'Bearer '+m['admin_token']})")
    _compose(run_dir, "exec", "-T", "market", "python", "-c", code, check=False)


def run_docker(run_dir: Path, build: bool = True, grace_seconds: float = 120) -> None:
    manifest = Manifest.load(run_dir / "manifest.json")
    agent_ids = [a.id for a in manifest.agents]
    _compose(run_dir, "up", "-d", *(["--build"] if build else []), check=True)
    deadline = time.time() + manifest.duration_seconds + grace_seconds
    try:
        while time.time() < deadline:
            states = _agent_states(run_dir, agent_ids)
            if states and all(s in ("exited", "dead") for s in states.values()) and len(states) == len(agent_ids):
                break
            time.sleep(5)
    except KeyboardInterrupt:
        print("interrupted - closing market")
    finally:
        close_market(run_dir)
        time.sleep(3)  # let netmon flush in-flight flows
        with open(run_dir / "logs" / "docker_compose.log", "w") as f:
            _compose(run_dir, "logs", "--no-color", "--timestamps", stdout=f, stderr=subprocess.STDOUT)
        _compose(run_dir, "down", "--remove-orphans", check=False)
