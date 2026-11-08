"""Chained sessions: one team plays several markets in a row, each agent's memory carried over.

`hivemarket run b.yaml --memory-from runs/<a>` (or `hivemarket chain a.yaml b.yaml ...`) starts each
agent's memory with what it held at the end of run a:

  memory files      from artifacts/<agent>/memory_final.tar.gz (docker runs) or artifacts/<agent>/memory/
                    (local runs)
  previous_session.md  with memory.carry_results: the agent's own result in run a (completed or not,
                    credits spent, broker fees), from its last round. Never the ideal or other agents' results.

The files go to runs/<b>/memory_seed/<agent>/ and memory_seed.json records where they came from. They
reach the agent before it starts: copied into its memory directory here in local mode, and by its
monitor (which mounts the run directory; the agent doesn't) in docker mode.
"""

from __future__ import annotations

import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

from hivemarket.common.config import ExperimentConfig
from hivemarket.common.manifest import Manifest

SEED_DIR = "memory_seed"
RESULTS_FILE = "previous_session.md"
MEMORY_ARCHIVE_ROOT = "var/lib/hivemarket/memory"  # arcname of the monitor's memory_final.tar.gz


def _plain_name(name: str) -> bool:
    """The memory tool's file names: flat, no hidden files (see agent/tools.py MemoryStore)."""
    return bool(name) and "/" not in name and "\\" not in name and not name.startswith(".")


def _memory_files(source: Path, agent_id: str) -> dict[str, bytes]:
    """An agent's memory files at the end of a run, by name."""
    artifacts = source / "artifacts" / agent_id
    archive = artifacts / "memory_final.tar.gz"
    if archive.exists():
        files = {}
        with tarfile.open(archive) as tar:
            for m in tar.getmembers():
                parent, _, name = m.name.rpartition("/")
                if m.isfile() and parent == MEMORY_ARCHIVE_ROOT and _plain_name(name):
                    files[name] = tar.extractfile(m).read()
        return files
    folder = artifacts / "memory"
    if folder.is_dir():
        return {p.name: p.read_bytes() for p in sorted(folder.iterdir()) if p.is_file() and _plain_name(p.name)}
    return {}


def _last_round(source: Path) -> dict[str, Any] | None:
    rounds = sorted((source / "rounds").glob("round-*.json"), key=lambda p: int(p.stem.split("-")[1]))
    return json.loads(rounds[-1].read_text()) if rounds else None


def results_text(agent_id: str, result: dict[str, Any], start_credits: int | None) -> str:
    """The agent's own result in its previous session, from the broker's round_end per_agent entry."""
    offers, fees = result.get("spent_on_offers", 0), result.get("spent_on_fees", 0)
    messages, tokens = result.get("spent_on_messages", 0), result.get("spent_on_tokens", 0)
    spent = tokens + offers + fees + messages
    lines = ["# Your previous session", "",
             f"Your own result as {agent_id} in your previous session, recorded by the broker.", "",
             f"- Task completed: {'yes' if result.get('completed') else 'no'}"]
    if start_credits is not None:
        lines.append(f"- Credits at the start: {start_credits}")
    lines += [f"- Credits spent: {spent} (tokens {tokens}, broker fees {offers + fees}"
              + (f", messages {messages}" if messages else "") + ")",
              f"- Credits received from sales: {result.get('received', 0)}",
              f"- Credits left: {result.get('credits_left')}"]
    return "\n".join(lines) + "\n"


def prepare_seed(source: str | Path, run_dir: Path, cfg: ExperimentConfig, manifest: Manifest) -> dict[str, Any]:
    """Write runs/<new>/memory_seed/<agent>/ from the source run; returns what memory_seed.json records."""
    source = Path(source).resolve()
    if not (source / "manifest.json").exists():
        raise SystemExit(f"--memory-from {source}: not a run directory (no manifest.json)")
    if not cfg.memory.enabled:
        raise SystemExit("--memory-from carries memory, but this experiment has no memory "
                         "(session: fresh, or memory.enabled: false)")
    before = Manifest.load(source / "manifest.json")
    team = lambda m: {a.id: (a.provider, a.model) for a in m.agents}
    if team(before) != team(manifest):
        raise SystemExit(f"--memory-from {source}: a chained session needs the same team (agent ids, providers "
                         f"and models); it had {team(before)}, this run has {team(manifest)}")
    last = _last_round(source) if cfg.memory.carry_results else None
    if cfg.memory.carry_results and last is None:
        raise SystemExit(f"--memory-from {source}: memory.carry_results needs its rounds/round-N.json; "
                         "the run has none (did it finish?)")
    world = json.loads((source / "world.json").read_text())
    seeded: dict[str, list[str]] = {}
    for agent in manifest.agents:
        files = _memory_files(source, agent.id)
        if last is not None:
            start = world.get("nodes", {}).get(agent.id, {}).get("credits")
            files[RESULTS_FILE] = results_text(agent.id, last["per_agent"][agent.id], start).encode()
        out = run_dir / SEED_DIR / agent.id
        out.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (out / name).write_bytes(data)
        seeded[agent.id] = sorted(files)
    record = {"from_run": before.run_id, "from_path": str(source), "carry_results": cfg.memory.carry_results,
              "results_round": last.get("round") if last else None, "files": seeded}
    (run_dir / "memory_seed.json").write_text(json.dumps(record, indent=2))
    return record


def seed_memory(seed: Path, memory_root: Path) -> list[str]:
    """Copy an agent's seed files into its (empty) memory directory; returns their names."""
    if not seed.is_dir():
        return []
    memory_root.mkdir(parents=True, exist_ok=True)
    names = []
    for p in sorted(seed.iterdir()):
        if p.is_file() and _plain_name(p.name):
            shutil.copyfile(p, memory_root / p.name)
            names.append(p.name)
    return names
