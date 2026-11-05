"""Run manifest: identities, credentials and addresses for one run.

The manifest is readable only by trusted services (market, gateway,
collector, netmon). Agents receive just their own entry via env vars.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class AgentEntry(BaseModel):
    id: str
    token: str
    ip: str
    inbox_host: str  # hostname other agents would use to reach this agent directly
    inbox_port: int
    provider: str
    model: str
    spec: dict[str, Any] = Field(default_factory=dict)


class ServiceEntry(BaseModel):
    ip: str
    url: str  # URL as seen from inside the agents' network


class Manifest(BaseModel):
    run_id: str
    experiment: str
    mode: str  # "docker" | "local"
    admin_token: str
    duration_seconds: float
    agents: list[AgentEntry]
    services: dict[str, ServiceEntry]
    enforce_source_ip: bool = True

    def agent(self, agent_id: str) -> AgentEntry | None:
        return next((a for a in self.agents if a.id == agent_id), None)

    def agent_by_token(self, token: str | None) -> AgentEntry | None:
        if not token:
            return None
        return next((a for a in self.agents if a.token == token), None)

    def agent_by_ip(self, ip: str | None) -> AgentEntry | None:
        if not ip or self.mode == "local":
            return None
        return next((a for a in self.agents if a.ip == ip), None)

    def label_ip(self, ip: str) -> str:
        """Human label for an IP: agent id, service name, or the IP itself."""
        a = self.agent_by_ip(ip)
        if a:
            return a.id
        for name, svc in self.services.items():
            if svc.ip == ip:
                return name
        return ip

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "Manifest":
        return cls.model_validate(json.loads(Path(path).read_text()))


def bearer(headers: Any) -> str | None:
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("x-api-key")
