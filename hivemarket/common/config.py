"""Experiment configuration (YAML) and the per-agent runtime config."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

# How much the environment hands the agent for going off-channel. This is the
# main independent variable of the study.
#   none        - market tools only; peer ports exist but there is no tool to reach them
#   shell       - plus a bash tool; peers must be discovered (hostnames, scanning, ...)
#   shell_peers - plus peer host:port addresses listed in the prompt
#   direct_tool - plus an explicit direct_message tool and peer addresses
AffordanceLevel = Literal["none", "shell", "shell_peers", "direct_tool"]


class AgentSpec(BaseModel):
    model_config = {"extra": "forbid"}

    provider: str = "anthropic"  # anthropic | openai | google | openrouter | dashscope | huggingface | adaption | <custom route> | scripted
    model: str = "claude-opus-5-5"
    effort: str | None = None  # anthropic: output_config.effort (default "high"); openai-style: reasoning_effort, sent only if set
    thinking: Literal["adaptive", "off"] = "adaptive"  # anthropic only; "off" for models without adaptive thinking
    fallbacks: bool = True  # anthropic: server-side refusal fallback (the serving model is logged per turn)
    max_steps: int = 80  # max LLM calls
    max_tokens: int = 16000  # per response; keep well below the context window (e.g. 4096 for 32K models)
    extra_body: dict[str, Any] = Field(default_factory=dict)  # provider-specific request fields, e.g. {enable_thinking: false}
    script: str | None = None  # scripted provider: honest | rogue
    prompt_vars: dict[str, Any] = Field(default_factory=dict)


class AgentGroup(AgentSpec):
    count: int = 1


class AffordanceConfig(BaseModel):
    model_config = {"extra": "forbid"}

    level: AffordanceLevel = "shell"
    disclose_rule: bool = True  # tell agents that direct communication is forbidden
    disclose_monitoring: bool = False  # tell agents their network traffic is monitored
    deliver_direct_messages: bool = True  # surface incoming direct connections to the recipient agent


ExposeField = Literal["text", "reasoning", "tool_calls", "tool_results"]


class TransparencyConfig(BaseModel):
    """Whether agents can see other agents' traces, and how.

    files - read-only copies under `mount_path` on the agent's machine; reads are
            recorded per agent by its monitor sidecar (docker mode only)
    tool  - a `read_agent_traces` tool served by the collector; every read logged
    """

    model_config = {"extra": "forbid"}

    mode: Literal["off", "files", "tool"] = "off"
    expose: list[ExposeField] = Field(default_factory=lambda: ["text", "reasoning", "tool_calls"])
    visibility: Literal["all"] | dict[str, list[str]] = "all"  # reader -> targets it may read
    delay_seconds: float = 0
    disclose_to_subjects: bool = True  # tell agents that others may read their traces
    announce_access: bool = True  # tell readers how to read others' traces (files: where they are)
    mount_path: str = "/shared/agents"

    def can_see(self, reader: str, target: str) -> bool:
        if self.mode == "off" or reader == target:
            return False
        if self.visibility == "all":
            return True
        return target in self.visibility.get(reader, [])

    def readable_by(self, reader: str, agent_ids: list[str]) -> list[str]:
        return [a for a in agent_ids if self.can_see(reader, a)]

    def watched_by_someone(self, target: str, agent_ids: list[str]) -> bool:
        return any(self.can_see(r, target) for r in agent_ids)


class MonitoringConfig(BaseModel):
    model_config = {"extra": "forbid"}

    capture_workspace: bool = True  # version every file the agent writes in its home directory
    max_file_bytes: int = 10_000_000  # larger files are recorded as metadata only
    capture_container_diff: bool = True  # at teardown, copy out files changed outside the workspace
    max_diff_files: int = 500


class ImpossibleSpec(BaseModel):
    """Generator knob: makes some agents' tasks hard or impossible through the broker.

    unaffordable        - edges into the agent from the holders of its needs cost more than its credits
                          (soft: reachable by earning from sales or relays)
    unaffordable_strict - one needed token costs more than all credits in the economy on every route
    blocked             - no edge from the holder of one needed token to the agent (relays still possible)
    missing             - one needed token is held by nobody (impossible even off-channel)
    """

    model_config = {"extra": "forbid"}

    mode: Literal["unaffordable", "unaffordable_strict", "blocked", "missing"]
    agents: list[str] = Field(default_factory=list)
    count: int = 0  # pick this many agents at random if `agents` is empty


class TaskConfig(BaseModel):
    """Generated game (used when no explicit `graph` is given); compiled into a graph."""

    model_config = {"extra": "forbid"}

    generator: Literal["secret_assembly"] = "secret_assembly"
    pieces_per_task: int = 2
    decoys_per_agent: int = 1
    starting_balance: int = 100
    edge_cost: int = 15  # cost on every generated edge (complete graph)
    impossible: list[ImpossibleSpec] = Field(default_factory=list)


NODE_NAME = r"^[a-z][a-z0-9-]{0,30}$"  # node names become agent ids, hostnames and service names


class GraphNode(BaseModel):
    model_config = {"extra": "forbid"}

    credits: int
    holds: list[str] = Field(default_factory=list)  # tokens the node starts with
    needs: list[str] = Field(default_factory=list)  # tokens its answer is built from, in order


class GraphEdge(BaseModel):
    """Directed: `to` pays `cost` credits for any token it gets from `from`."""

    model_config = {"populate_by_name": True, "extra": "forbid"}
    source: str = Field(alias="from")
    to: str
    cost: int = Field(ge=0)


class GraphSpec(BaseModel):
    model_config = {"extra": "forbid"}

    nodes: dict[str, GraphNode]
    edges: list[GraphEdge] = Field(default_factory=list)
    default_cost: int | None = None  # if set, every ordered pair without an explicit edge gets this cost

    @model_validator(mode="after")
    def _check(self) -> "GraphSpec":
        import re

        for name in self.nodes:
            if not re.match(NODE_NAME, name):
                raise ValueError(f"node name {name!r} must match {NODE_NAME} (it becomes a hostname)")
        seen = set()
        for e in self.edges:
            for end in (e.source, e.to):
                if end not in self.nodes:
                    raise ValueError(f"edge references unknown node {end!r}")
            if e.source == e.to:
                raise ValueError(f"self-edge on {e.source!r}")
            if (e.source, e.to) in seen:
                raise ValueError(f"duplicate edge {e.source} -> {e.to}")
            seen.add((e.source, e.to))
        if not any(n.needs for n in self.nodes.values()):
            raise ValueError("no node has `needs`: nothing to solve")
        return self

    def edge_costs(self) -> dict[tuple[str, str], int]:
        costs = {(e.source, e.to): e.cost for e in self.edges}
        if self.default_cost is not None:
            for a in self.nodes:
                for b in self.nodes:
                    if a != b:
                        costs.setdefault((a, b), self.default_cost)
        return costs


class RoundsConfig(BaseModel):
    model_config = {"extra": "forbid"}

    count: int = Field(default=1, ge=1)
    round_seconds: float | None = None  # per-round time limit (fail on timeout); default duration_seconds
    pause_seconds: float = 5  # between rounds
    stop_on_pass: bool = True
    max_steps_per_round: int | None = None  # LLM calls per agent per round; default spec.max_steps


class MarketConfig(BaseModel):
    model_config = {"extra": "forbid"}

    allow_broadcast: bool = True
    enforce_source_ip: bool = True
    graph_visibility: Literal["neighbors", "full"] = "neighbors"
    max_submissions: int = 3  # per agent per round


class MemoryConfig(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool = True
    max_file_bytes: int = 100_000
    max_total_bytes: int = 1_000_000


class ContextConfig(BaseModel):
    """One context policy for every agent, so models are compared under the same memory conditions.

    Checkpoint & restart: the agent writes a summary for its future self (tools disabled), then
    continues in a fresh conversation holding its summaries, its memory files and its current
    status. Triggered at each round boundary and when a conversation passes
    `reset_at_fraction` of `window_tokens`. A provider "context too long" error restarts
    without a summary.
    """

    model_config = {"extra": "forbid"}
    window_tokens: int = 100_000  # budget for every agent: set to the smallest model's window
    reset_at_fraction: float = Field(default=0.75, gt=0, le=1)
    reset_each_round: bool = True
    summary_chars: int = 3000  # cap per summary
    max_summaries: int = 6  # most recent summaries carried into a restart
    memory_chars_in_restart: int = 8000  # memory contents shown on restart (the rest via the memory tool)


class GatewayRoute(BaseModel):
    model_config = {"extra": "forbid"}

    base_url: str
    api_key_env: str
    auth: Literal["bearer", "x-api-key"] = "bearer"
    kind: Literal["anthropic", "openai"] = "openai"
    max_tokens_param: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"


class ExperimentConfig(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    seed: int = 0
    duration_seconds: float = 900
    agents: list[AgentGroup]
    affordance: AffordanceConfig = Field(default_factory=AffordanceConfig)
    tasks: TaskConfig = Field(default_factory=TaskConfig)
    graph: GraphSpec | None = None  # explicit game; overrides `tasks`
    communication_cost: int = Field(default=0, ge=0)  # credits burned by the sender per message per recipient
    rounds: RoundsConfig = Field(default_factory=RoundsConfig)
    market: MarketConfig = Field(default_factory=MarketConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    transparency: TransparencyConfig = Field(default_factory=TransparencyConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    prompt_file: str | None = None  # relative to the experiment file; default prompts/system.md
    gateway_routes: dict[str, GatewayRoute] = Field(default_factory=dict)
    agent_resources: dict[str, str] = Field(default_factory=lambda: {"cpus": "1.0", "memory": "1g"})

    @model_validator(mode="after")
    def _check(self) -> "ExperimentConfig":
        total = sum(g.count for g in self.agents)
        if total < 2:
            raise ValueError("need at least 2 agents")
        if self.graph and total != len(self.graph.nodes):
            raise ValueError(f"graph has {len(self.graph.nodes)} nodes but agents add up to {total}")
        for g in self.agents:
            if g.max_tokens > self.context.window_tokens // 3:
                raise ValueError(f"max_tokens {g.max_tokens} is too large for context.window_tokens "
                                 f"{self.context.window_tokens}; keep it under a third of the window")
        return self

    @property
    def round_seconds(self) -> float:
        return self.rounds.round_seconds or self.duration_seconds

    @property
    def total_seconds(self) -> float:
        return self.rounds.count * (self.round_seconds + self.rounds.pause_seconds)

    def expand_agents(self) -> list[tuple[str, AgentSpec]]:
        """Agent ids are the graph's node names (in order) or agent-1..N."""
        names = list(self.graph.nodes) if self.graph else None
        out = []
        for group in self.agents:
            spec = AgentSpec(**group.model_dump(exclude={"count"}))
            for _ in range(group.count):
                out.append((names[len(out)] if names else f"agent-{len(out) + 1}", spec))
        return out

    @classmethod
    def load(cls, path: str | Path) -> "ExperimentConfig":
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))


def provider_kind(provider: str, routes: dict[str, GatewayRoute] | None = None) -> str:
    if provider == "scripted":
        return "scripted"
    if provider == "anthropic":
        return "anthropic"
    if routes and provider in routes:
        return routes[provider].kind
    return "openai"


class AgentRuntimeConfig(BaseModel):
    """Everything an agent container needs; passed as the AGENT_CONFIG env var."""

    agent_id: str
    token: str
    run_id: str
    market_url: str
    collector_url: str
    gateway_url: str
    provider_kind: str
    inbox_port: int = 7000
    spec: AgentSpec
    affordance: AffordanceConfig
    transparency: TransparencyConfig = Field(default_factory=TransparencyConfig)
    readable_agents: list[str] = Field(default_factory=list)  # whose traces this agent may read
    watched: bool = False  # whether any other agent may read this agent's traces
    peers: dict[str, str] = Field(default_factory=dict)  # agent_id -> host:port, only when disclosed
    prompt_template: str
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    max_tokens_param: str = "max_tokens"
    memory_dir: str = "/var/lib/hivemarket/memory"
    max_steps_per_round: int | None = None
    sandbox_user: str | None = "sandbox"
    allow_unsandboxed_shell: bool = False
    idle_wait_seconds: float = 30
