"""Chained sessions (--memory-from): each agent starts with the memory it ended its previous run with,
plus, with memory.carry_results, its own result there; never the ideal or other agents' results."""

import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml

from hivemarket.orchestrator.carry import RESULTS_FILE, results_text
from hivemarket.orchestrator.run import create_run

ROOT = Path(__file__).resolve().parents[1]
SMOKE = yaml.safe_load((ROOT / "experiments" / "local_smoke.yaml").read_text())


def experiment(tmp_path: Path, name: str, **changes) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(SMOKE | {"name": name} | changes))
    return path


def finished_run(tmp_path: Path) -> Path:
    """A run as it looks after playing: agent-1 kept memory as files (local), agent-2 in the monitor's tarball."""
    run = create_run(experiment(tmp_path, "first"), tmp_path / "runs", mode="local", run_id="first")
    memory = run / "artifacts" / "agent-1" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "notes.md").write_text("agent-2 relays A1 cheaply")
    (run / "artifacts" / "agent-2").mkdir(parents=True, exist_ok=True)
    with tarfile.open(run / "artifacts" / "agent-2" / "memory_final.tar.gz", "w:gz") as tar:
        for name, text in (("plan.md", "ask agent-1 first"), (".hidden", "x")):
            info = tarfile.TarInfo(f"var/lib/hivemarket/memory/{name}")
            info.size = len(text)
            tar.addfile(info, io.BytesIO(text.encode()))
    per_agent = {a: {"spent_on_tokens": 4, "spent_on_offers": 2, "spent_on_fees": 1, "spent_on_messages": 0,
                     "received": 10, "credits_left": 93, "completed": a == "agent-3"}
                 for a in ("agent-1", "agent-2", "agent-3")}
    (run / "rounds").mkdir(exist_ok=True)
    (run / "rounds" / "round-1.json").write_text(json.dumps(
        {"round": 1, "outcome": "fail", "ideal_credits": 17, "per_agent": per_agent}))
    return run


def test_memory_carries_into_the_next_run(tmp_path):
    first = finished_run(tmp_path)
    run = create_run(experiment(tmp_path, "second"), tmp_path / "runs", mode="local", run_id="second",
                     memory_from=first)
    assert (run / "artifacts" / "agent-1" / "memory" / "notes.md").read_text() == "agent-2 relays A1 cheaply"
    assert (run / "artifacts" / "agent-2" / "memory" / "plan.md").read_text() == "ask agent-1 first"
    assert not (run / "artifacts" / "agent-2" / "memory" / ".hidden").exists()  # not a memory tool file
    record = json.loads((run / "memory_seed.json").read_text())
    assert record["from_run"] == "first" and record["carry_results"] is False
    assert record["files"] == {"agent-1": ["notes.md"], "agent-2": ["plan.md"], "agent-3": []}
    assert not any((run / "memory_seed").rglob(RESULTS_FILE))  # only with memory.carry_results


def test_carry_results_writes_only_the_agents_own_result(tmp_path):
    first = finished_run(tmp_path)
    exp = experiment(tmp_path, "second", memory={"carry_results": True})
    run = create_run(exp, tmp_path / "runs", mode="local", run_id="second", memory_from=first)
    text = (run / "artifacts" / "agent-3" / "memory" / RESULTS_FILE).read_text()
    assert "Task completed: yes" in text and "Credits at the start: 60" in text
    assert "Credits spent: 7 (tokens 4, broker fees 3)" in text and "Credits left: 93" in text
    assert "ideal" not in text.lower() and "17" not in text and "agent-1" not in text
    assert "Task completed: no" in (run / "artifacts" / "agent-1" / "memory" / RESULTS_FILE).read_text()
    assert json.loads((run / "memory_seed.json").read_text())["results_round"] == 1


def test_results_text_lists_messages_only_when_there_were_any():
    base = {"spent_on_tokens": 0, "spent_on_offers": 1, "spent_on_fees": 0, "received": 0, "credits_left": 9,
            "completed": False}
    assert "messages" not in results_text("a", base | {"spent_on_messages": 0}, 10)
    assert "messages 2" in results_text("a", base | {"spent_on_messages": 2}, 10)


@pytest.mark.parametrize("changes, error", [
    ({"session": "fresh"}, "has no memory"),
    ({"session": "fresh", "memory": {"enabled": True}}, "starts a new team"),
    ({"agents": [{"provider": "scripted", "model": "other", "script": "honest", "count": 3}]}, "same team"),
])
def test_a_chained_session_needs_memory_and_the_same_team(tmp_path, changes, error):
    first = finished_run(tmp_path)
    with pytest.raises(SystemExit, match=error):
        create_run(experiment(tmp_path, "second", **changes), tmp_path / "runs", mode="local", run_id="second",
                   memory_from=first)


def test_the_monitor_seeds_the_memory_volume_in_docker_mode(tmp_path):
    first = finished_run(tmp_path)
    run = create_run(experiment(tmp_path, "second"), tmp_path / "runs", mode="docker", run_id="second",
                     memory_from=first)
    # Nothing is copied into a host folder in docker mode: the monitor copies memory_seed/<agent> into the volume.
    assert (run / "memory_seed" / "agent-1" / "notes.md").exists()
    assert not (run / "artifacts" / "agent-1" / "memory").exists()
    from hivemarket.orchestrator.carry import seed_memory
    volume = tmp_path / "volume"
    assert seed_memory(run / "memory_seed" / "agent-2", volume) == ["plan.md"]
    assert (volume / "plan.md").read_text() == "ask agent-1 first"
