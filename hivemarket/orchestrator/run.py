"""Creates a run directory: manifest, world, per-agent runtime configs, compose file.

runs/<run_id>/
  experiment.yaml      copy of the config used
  manifest.json        identities, tokens, IPs (trusted services only)
  world.json           the game graph, rules, ideal cost and generator ground truth
  rounds/round-N.json  per-round ledger written by the broker
  agents/<id>.json     runtime config handed to each agent
  docker-compose.yml   (docker mode)
  logs/                every component's JSONL events
  pcap/                per-agent packet captures (docker mode)
  artifacts/<agent>/   workspace file versions, final workspace snapshot, container diff
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import yaml

from hivemarket.common.config import AgentRuntimeConfig, ExperimentConfig, provider_kind
from hivemarket.common.manifest import AgentEntry, Manifest, ServiceEntry
from hivemarket.common.paths import repo_root
from hivemarket.orchestrator.network import allocate_subnet, host
from hivemarket.game.graph import build_graph, graph_to_world
from hivemarket.orchestrator.world import new_token

SERVICE_OCTETS = {"market": 10, "gateway": 11, "collector": 12}
FIRST_AGENT_OCTET = 101
INBOX_PORT = 7000


def _max_tokens_param(provider: str, cfg: ExperimentConfig) -> str:
    from hivemarket.gateway.app import DEFAULT_UPSTREAMS

    if provider in cfg.gateway_routes:
        return cfg.gateway_routes[provider].max_tokens_param
    return DEFAULT_UPSTREAMS.get(provider, {}).get("max_tokens_param", "max_tokens")


def create_run(experiment_path: str | Path, runs_dir: str | Path = "runs", mode: str = "docker",
               run_id: str | None = None, local_base_port: int = 18100,
               used_subnets: set[str] | None = None) -> Path:
    experiment_path = Path(experiment_path).resolve()
    cfg = ExperimentConfig.load(experiment_path)
    if cfg.transparency.mode == "files" and mode != "docker":
        raise SystemExit("transparency mode 'files' needs Docker (the monitor sidecar publishes and audits the files); "
                         "use mode 'tool' for local runs.")
    run_id = run_id or f"{cfg.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir = Path(runs_dir).resolve() / run_id
    (run_dir / "logs").mkdir(parents=True, exist_ok=False)
    (run_dir / "agents").mkdir()
    shutil.copy(experiment_path, run_dir / "experiment.yaml")

    prompt_path = (experiment_path.parent / cfg.prompt_file) if cfg.prompt_file else repo_root() / "prompts" / "system.md"
    prompt_template = prompt_path.read_text()
    shutil.copy(prompt_path, run_dir / "system_prompt_template.md")

    agents = cfg.expand_agents()
    agent_ids = [a for a, _ in agents]
    graph, impossible = build_graph(cfg, agent_ids)
    world = graph_to_world(graph, cfg, impossible)
    (run_dir / "world.json").write_text(json.dumps(world, indent=2))

    subnet = None
    if mode == "docker":
        subnet = allocate_subnet(Path(runs_dir).resolve(), run_id, used_subnets)
        services = {name: ServiceEntry(ip=host(subnet, o), url=f"http://{name}:8000") for name, o in SERVICE_OCTETS.items()}
        entries = [
            AgentEntry(id=aid, token=new_token(), ip=host(subnet, FIRST_AGENT_OCTET + i), inbox_host=aid,
                       inbox_port=INBOX_PORT,
                       provider=spec.provider, model=spec.model, spec=spec.model_dump())
            for i, (aid, spec) in enumerate(agents)
        ]
    else:
        ports = {"market": local_base_port, "gateway": local_base_port + 1, "collector": local_base_port + 2}
        services = {n: ServiceEntry(ip="127.0.0.1", url=f"http://127.0.0.1:{p}") for n, p in ports.items()}
        entries = [
            AgentEntry(id=aid, token=new_token(), ip="127.0.0.1", inbox_host="127.0.0.1",
                       inbox_port=local_base_port + 10 + i, provider=spec.provider, model=spec.model,
                       spec=spec.model_dump())
            for i, (aid, spec) in enumerate(agents)
        ]

    manifest = Manifest(
        run_id=run_id, experiment=cfg.name, mode=mode, admin_token=new_token(),
        duration_seconds=cfg.total_seconds, agents=entries, services=services,
        enforce_source_ip=cfg.market.enforce_source_ip, subnet=subnet,
    )
    manifest.save(run_dir / "manifest.json")
    if cfg.gateway_routes:
        (run_dir / "gateway_routes.json").write_text(
            json.dumps({k: v.model_dump() for k, v in cfg.gateway_routes.items()}, indent=2))

    disclose_peers = cfg.affordance.level in ("shell_peers", "direct_tool")
    for entry in entries:
        rc = AgentRuntimeConfig(
            agent_id=entry.id,
            token=entry.token,
            run_id=run_id,
            market_url=services["market"].url,
            collector_url=services["collector"].url,
            gateway_url=services["gateway"].url,
            provider_kind=provider_kind(entry.provider, cfg.gateway_routes),
            inbox_port=entry.inbox_port,
            spec=entry.spec,
            affordance=cfg.affordance,
            transparency=cfg.transparency,
            readable_agents=cfg.transparency.readable_by(entry.id, agent_ids),
            watched=cfg.transparency.watched_by_someone(entry.id, agent_ids),
            peers={a.id: f"{a.inbox_host}:{a.inbox_port}" for a in entries if a.id != entry.id} if disclose_peers else {},
            prompt_template=prompt_template,
            sandbox_user="sandbox" if mode == "docker" else None,
            memory=cfg.memory,
            context=cfg.context,
            max_tokens_param=_max_tokens_param(entry.provider, cfg),
            memory_dir="/var/lib/hivemarket/memory" if mode == "docker" else str(run_dir / "artifacts" / entry.id / "memory"),
            max_steps_per_round=cfg.rounds.max_steps_per_round,
        )
        (run_dir / "agents" / f"{entry.id}.json").write_text(rc.model_dump_json(indent=2))

    if mode == "docker":
        from hivemarket.orchestrator.compose import build_compose
        (run_dir / "pcap").mkdir()
        compose = build_compose(cfg, manifest, run_dir)
        (run_dir / "docker-compose.yml").write_text(yaml.safe_dump(compose, sort_keys=False, width=200))
    return run_dir
