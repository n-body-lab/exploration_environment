"""docker-compose generation and lifecycle for a run.

Topology (one private subnet per run, so runs can execute in parallel)
  agentnet (internal, no internet): market, collector, gateway, monitor-* (agents share their monitor's namespace)
  egress: gateway only - the single path to model providers

Each agent runs inside the network namespace owned by its monitor sidecar
(`network_mode: service:monitor-<agent>`). The sidecar starts first and is
healthy only once tcpdump is capturing, so no agent packet goes unrecorded.
The sidecar carries the `agent-N` DNS alias, so peers reach the agent at
agent-N:7000 exactly as if it were attached directly.

Per-agent volumes, each shared only between an agent and its own monitor:
  ws-<agent>    the agent's /home/sandbox; the monitor versions every write
  mem-<agent>   the agent's private memory (memory tool); the monitor versions every write
  view-<agent>  transparency files mode: traces the agent may read (read-only for the agent)
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from hivemarket.common.config import ExperimentConfig
from hivemarket.common.events import EventLog, JsonlSink
from hivemarket.common.manifest import Manifest
from hivemarket.orchestrator.network import release_subnet
from hivemarket.common.paths import repo_root

KEY_ENVS = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "DASHSCOPE_API_KEY",
            "HF_TOKEN"]
SYSTEM_PATHS = re.compile(r"^/(proc|sys|dev|run)(/|$)")
NOISE = re.compile(r"__pycache__|\.pyc$")


def project_name(run_dir: Path) -> str:
    return "hm-" + re.sub(r"[^a-z0-9_-]", "-", run_dir.name.lower())[:50]


def _build(target: str) -> dict[str, Any]:
    return {"context": str(repo_root()), "dockerfile": "docker/Dockerfile", "target": target}


def _escape(value: str) -> str:
    return value.replace("$", "$$")  # compose interpolates $; the prompt template uses $placeholders


def build_compose(cfg: ExperimentConfig, manifest: Manifest, run_dir: Path) -> dict[str, Any]:
    vol = [f"{run_dir}:/run_data"]
    services: dict[str, Any] = {}
    volumes: dict[str, Any] = {}
    agent_ids = [a.id for a in manifest.agents]

    def core(name: str, module: str, extra_networks: dict | None = None) -> dict[str, Any]:
        return {
            "image": "hivemarket/core:dev",
            "build": _build("core"),
            "command": ["python", "-m", module],
            "environment": {"RUN_DIR": "/run_data"},
            "volumes": vol,
            "networks": {"agentnet": {"ipv4_address": manifest.services[name].ip}, **(extra_networks or {})},
            "healthcheck": {
                "test": ["CMD", "python", "-c", "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()"],
                "interval": "2s", "timeout": "3s", "retries": 30,
            },
        }

    services["market"] = core("market", "hivemarket.market")
    services["collector"] = core("collector", "hivemarket.collector")
    gateway = core("gateway", "hivemarket.gateway", {"egress": {}})
    key_envs = KEY_ENVS + [r.api_key_env for r in cfg.gateway_routes.values()]
    gateway["environment"].update({k: f"${{{k}:-}}" for k in dict.fromkeys(key_envs)})
    gateway["extra_hosts"] = ["host.docker.internal:host-gateway"]  # self-hosted models on the Docker host (Linux)
    services["gateway"] = gateway

    deps = {s: {"condition": "service_healthy"} for s in ("market", "collector", "gateway")}
    res = cfg.agent_resources
    for agent in manifest.agents:
        monitor = f"monitor-{agent.id}"
        ws, mem, view = f"ws-{agent.id}", f"mem-{agent.id}", f"view-{agent.id}"
        volumes[ws] = {}
        monitor_vols = vol + [f"{ws}:/watch/workspace"]
        agent_vols = [f"{ws}:/home/sandbox"]
        if cfg.memory.enabled:
            volumes[mem] = {}
            monitor_vols.append(f"{mem}:/watch/memory")
            agent_vols.append(f"{mem}:/var/lib/hivemarket/memory")
        if cfg.transparency.mode == "files" and cfg.transparency.readable_by(agent.id, agent_ids):
            volumes[view] = {}
            monitor_vols.append(f"{view}:/watch/shared")
            agent_vols.append(f"{view}:{cfg.transparency.mount_path}:ro")

        services[monitor] = {
            "image": "hivemarket/monitor:dev",
            "build": _build("monitor"),
            "command": ["python", "-m", "hivemarket.monitor"],
            "hostname": agent.id,
            "environment": {"RUN_DIR": "/run_data", "AGENT_ID": agent.id},
            "volumes": monitor_vols,
            "cap_add": ["NET_RAW", "NET_ADMIN"],
            "networks": {"agentnet": {"ipv4_address": agent.ip, "aliases": [agent.id]}},
            "expose": [str(agent.inbox_port)],
            "stop_grace_period": "30s",  # time to flush flows and snapshot the workspace
            "healthcheck": {"test": ["CMD", "test", "-f", "/tmp/capturing"], "interval": "1s", "retries": 30},
        }
        agent_cfg = (run_dir / "agents" / f"{agent.id}.json").read_text()
        services[agent.id] = {
            "image": "hivemarket/agent:dev",
            "build": _build("agent"),
            "network_mode": f"service:{monitor}",
            "environment": {"AGENT_CONFIG": _escape(json.dumps(json.loads(agent_cfg)))},
            "volumes": agent_vols,
            "cap_drop": ["ALL"],
            "cap_add": ["SETUID", "SETGID"],  # only to drop the bash tool to the sandbox user
            "security_opt": ["no-new-privileges:true"],
            "pids_limit": 256,
            "cpus": float(res.get("cpus", 1.0)),
            "mem_limit": res.get("memory", "1g"),
            "restart": "no",
            "depends_on": {**deps, monitor: {"condition": "service_healthy"}},
        }

    return {
        "name": project_name(run_dir),
        "services": services,
        "networks": {
            "agentnet": {"driver": "bridge", "internal": True, "ipam": {"config": [{"subnet": manifest.subnet}]}},
            "egress": {"driver": "bridge"},
        },
        "volumes": volumes,
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


def _under_mount(path: str, mounts: list[str]) -> bool:
    # The mount point itself, anything inside it, and directories Docker created to hold it.
    return any(path == m or path.startswith(m + "/") or m.startswith(path + "/") for m in mounts)


def parse_diff(text: str, mounts: list[str] = ()) -> list[tuple[str, str]]:
    """`docker diff` output -> [(kind A|C|D, path)], without system paths and volume mounts (captured separately)."""
    out = []
    for line in text.splitlines():
        kind, _, path = line.strip().partition(" ")
        if kind in ("A", "C", "D") and path and not SYSTEM_PATHS.search(path) and not _under_mount(path, list(mounts)):
            out.append((kind, path))
    return out


def diff_leaves(entries: list[tuple[str, str]]) -> list[str]:
    """Added/changed paths that are not parents of other listed paths (docker cp copies dirs recursively).

    Leaves are computed before dropping bytecode-cache noise, so a directory that only
    "changed" because a cache appeared inside it is not copied.
    """
    paths = sorted(p for k, p in entries if k in ("A", "C"))
    leaves = [p for i, p in enumerate(paths) if not (i + 1 < len(paths) and paths[i + 1].startswith(p + "/"))]
    return [p for p in leaves if not NOISE.search(p)]


def capture_container_diffs(run_dir: Path, manifest: Manifest, cfg: ExperimentConfig, log: EventLog) -> None:
    """Copy out files each agent changed outside its workspace volume before containers are removed."""
    for agent in manifest.agents:
        cid = _compose(run_dir, "ps", "-a", "-q", agent.id, capture_output=True, text=True).stdout.strip()
        if not cid:
            continue
        diff = subprocess.run(["docker", "diff", cid], capture_output=True, text=True)
        entries = parse_diff(diff.stdout, mounts=["/home/sandbox", "/var/lib/hivemarket/memory",
                                                  cfg.transparency.mount_path])
        out = run_dir / "artifacts" / agent.id
        out.mkdir(parents=True, exist_ok=True)
        meaningful = [(k, p) for k, p in entries if not NOISE.search(p)]
        (out / "container_diff.json").write_text(json.dumps([{"kind": k, "path": p} for k, p in meaningful], indent=1))
        copied = []
        for path in diff_leaves(entries)[: cfg.monitoring.max_diff_files]:
            dest = out / "rootfs" / path.lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if subprocess.run(["docker", "cp", f"{cid}:{path}", str(dest)], capture_output=True).returncode == 0:
                copied.append(path)
        log.emit("container_diff", agent_id=agent.id, added=sum(k == "A" for k, _ in meaningful),
                 changed=sum(k == "C" for k, _ in meaningful), deleted=sum(k == "D" for k, _ in meaningful),
                 copied=copied, artifact=str((out / "container_diff.json").relative_to(run_dir)))


def run_docker(run_dir: Path, build: bool = True, grace_seconds: float = 120) -> None:
    manifest = Manifest.load(run_dir / "manifest.json")
    cfg = ExperimentConfig.load(run_dir / "experiment.yaml")
    log = EventLog("orchestrator", manifest.run_id, JsonlSink(run_dir / "logs" / "orchestrator.jsonl"))
    agent_ids = [a.id for a in manifest.agents]
    log.emit("run_starting", subnet=manifest.subnet, agents=agent_ids)
    try:
        _compose(run_dir, "up", "-d", *(["--build"] if build else []), check=True)
        deadline = time.time() + manifest.duration_seconds + grace_seconds
        while time.time() < deadline:
            states = _agent_states(run_dir, agent_ids)
            if len(states) == len(agent_ids) and all(s in ("exited", "dead") for s in states.values()):
                break
            time.sleep(5)
    except KeyboardInterrupt:
        print("interrupted - closing market")
        log.emit("run_interrupted")
    finally:
        close_market(run_dir)
        time.sleep(3)  # let monitors flush in-flight flows
        if cfg.monitoring.capture_container_diff:
            capture_container_diffs(run_dir, manifest, cfg, log)
        # Stopping the monitors makes them write the final workspace snapshot.
        _compose(run_dir, "stop", "-t", "30", check=False)
        with open(run_dir / "logs" / "docker_compose.log", "w") as f:
            _compose(run_dir, "logs", "--no-color", "--timestamps", stdout=f, stderr=subprocess.STDOUT)
        # -v removes only this run's volumes; their contents were captured above.
        _compose(run_dir, "down", "-v", "--remove-orphans", check=False)
        if manifest.subnet:
            release_subnet(run_dir.parent, manifest.subnet)
        log.emit("run_finished")
