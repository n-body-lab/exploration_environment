"""The post-session questionnaire: session: reflection with a reflection_file (prompts/postsession.md).

With rounds: 1, session: reflection plays exactly like session: fresh during the session (same system prompt,
tools and memory) and adds one turn, tools off, once trading has closed: the questionnaire, sent as written.
"""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

from hivemarket.agent.prompts import reflection_text, render_system_prompt
from hivemarket.agent.tools import tools_for_config
from hivemarket.common.config import AgentRuntimeConfig, ExperimentConfig
from hivemarket.common.events import read_jsonl
from hivemarket.orchestrator.local import run_local
from hivemarket.orchestrator.run import create_run

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS = ROOT / "experiments"
POSTSESSION = ROOT / "prompts" / "postsession.md"


def config(**extra) -> ExperimentConfig:
    return ExperimentConfig.model_validate({"name": "s", "agents": [{"provider": "scripted", "count": 2}], **extra})


def test_reflection_file_is_only_for_session_reflection():
    assert config(session="reflection", reflection_file="../prompts/postsession.md").reflection_file
    for session in ("fresh", "memory"):
        with pytest.raises(ValidationError, match="reflection_file is only used with session: reflection"):
            config(session=session, reflection_file="../prompts/postsession.md")


def test_the_main_arm_ends_with_the_postsession_questionnaire():
    cfg = ExperimentConfig.load(EXPERIMENTS / "session_main.yaml")
    assert (cfg.rounds.count, cfg.session) == (1, "reflection")
    assert (EXPERIMENTS / cfg.reflection_file).resolve() == POSTSESSION
    assert cfg.end_grace_seconds > 0  # the run waits for the answers before it is torn down


def test_the_questionnaire_is_sent_as_written():
    """It has no $placeholders, so the reflection prompt is the file itself, whatever the round's result."""
    text = POSTSESSION.read_text()
    assert "$" not in text and "Trading is closed." in text and "Reply in exactly this format" in text
    cfg = SimpleNamespace(reflection_template=text, spec=SimpleNamespace(prompt_vars={}))
    assert reflection_text(cfg, {"round": 1, "body": "Round 1 of 1 is over: FAIL."}, {}) == text.strip()


def one_round_arm(tmp_path: Path, session: str, port: int) -> Path:
    """A one-round local run with scripted agents (experiments/local_fail.yaml), set up like the main arm:
    the brokerage prompt and a memory tool; with session: reflection, the questionnaire as reflection_file.
    The experiment sits in experiments/ next to a copy of prompts/, so the relative paths resolve as in the repo."""
    base = tmp_path / session
    shutil.copytree(ROOT / "prompts", base / "prompts")
    (base / "experiments").mkdir(parents=True)
    cfg = yaml.safe_load((EXPERIMENTS / "local_fail.yaml").read_text())
    cfg |= {"name": f"arm-{session}", "session": session, "memory": {"enabled": True},
            "prompt_file": "../prompts/system_brokerage.md",
            "rounds": {"count": 1, "round_seconds": 20, "pause_seconds": 1}}
    if session == "reflection":
        cfg["reflection_file"] = "../prompts/postsession.md"
    exp = base / "experiments" / "arm.yaml"
    exp.write_text(yaml.safe_dump(cfg))
    return create_run(exp, base / "runs", mode="local", local_base_port=port)


async def test_with_one_round_reflection_plays_like_fresh_then_asks_the_questionnaire(tmp_path):
    fresh, reflection = one_round_arm(tmp_path, "fresh", 20300), one_round_arm(tmp_path, "reflection", 20350)

    # During the session: the same system prompt and the same tools for every agent.
    status = {"participants": ["agent-1", "agent-2", "agent-3"], "rounds_total": 1, "offer_tax": 1}
    for path in sorted((fresh / "agents").glob("*.json")):
        f = AgentRuntimeConfig.model_validate_json(path.read_text())
        r = AgentRuntimeConfig.model_validate_json((reflection / "agents" / path.name).read_text())
        assert render_system_prompt(f, status) == render_system_prompt(r, status)
        assert tools_for_config(f) == tools_for_config(r)
        assert r.reflection_template == POSTSESSION.read_text() and not f.reflection_template
    assert (reflection / "reflection_prompt_template.md").read_text() == POSTSESSION.read_text()

    for run_dir in (fresh, reflection):
        await run_local(run_dir)
    agents = ("agent-1", "agent-2", "agent-3")

    # After the session: fresh clears the memory notes; reflection asks the questionnaire, once per agent,
    # after trading has closed.
    traces = read_jsonl(fresh / "logs" / "agent_traces.jsonl")
    assert not [e for e in traces if e["kind"] == "round_reflection"]
    assert {e["agent_id"] for e in traces if e["kind"] == "memory_cleared"} == set(agents)

    traces = read_jsonl(reflection / "logs" / "agent_traces.jsonl")
    [ended] = [e["ts"] for e in read_jsonl(reflection / "logs" / "market.jsonl") if e["kind"] == "round_end"]
    answers = [e for e in traces if e["kind"] == "round_reflection"]
    assert sorted(e["agent_id"] for e in answers) == list(agents)
    assert all(e["data"]["round"] == 1 and e["data"]["text"] and e["ts"] > ended for e in answers)
    assert not [e for e in traces if e["kind"] == "memory_cleared"]
