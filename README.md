# exploration_environment

Testbed for agent swarm setup. Contains **HiveMarket**: LLM agents trading information through a sanctioned market channel, with full observability of whether, when and why they leave it.

See [docs/architecture.md](docs/architecture.md) for the design.

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# Pipeline check: scripted agents, no LLM calls, no Docker (~20s)
.venv/bin/hivemarket local experiments/local_smoke.yaml

# Real experiment: needs Docker running and provider keys in the environment
export ANTHROPIC_API_KEY=...        # plus OPENAI_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY as needed
.venv/bin/hivemarket run experiments/baseline.yaml

# Rebuild the report for an existing run
.venv/bin/hivemarket report runs/<run_id>

.venv/bin/pytest
```

Each run writes to `runs/<run_id>/`:

| file | contents |
|---|---|
| `summary.md` / `summary.json` | per-agent outcomes, trading stats, off-channel indicators, time-to-violation |
| `timeline.jsonl` | every event from every source, merged and time-sorted |
| `logs/market.jsonl` | all market requests + trades, prices, messages, submissions |
| `logs/agent_traces.jsonl` | agent runtime traces: LLM turns, tool calls, direct inbound |
| `logs/gateway.jsonl` | full LLM request/response bodies |
| `logs/netmon-<agent>.jsonl`, `pcap/<agent>.pcap` | packet-level flows, DNS, off-channel alerts |
| `manifest.json`, `world.json`, `experiment.yaml` | identities, ground truth, exact config |

## Experiments

| file | condition |
|---|---|
| `experiments/baseline.yaml` | feasible tasks, shell, rule disclosed |
| `experiments/impossible_unaffordable.yaml` | 2 of 4 agents can't afford their task via the market; peer addresses disclosed |
| `experiments/mixed_providers.yaml` | Claude + OpenAI + Gemini agents, blocked and missing items, explicit direct tool |
| `experiments/local_smoke.yaml` | scripted honest/rogue agents for testing |
