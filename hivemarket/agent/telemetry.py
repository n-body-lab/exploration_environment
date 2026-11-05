"""Ships agent-runtime trace events to the collector (and to stdout).

Events are buffered and sent in batches; on failure they stay buffered and
are retried, so a collector restart does not lose traces. Stdout copies end
up in `docker logs`, which the orchestrator saves as a second source.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from hivemarket.common.events import now


class Telemetry:
    def __init__(self, collector_url: str, token: str, agent_id: str, *, echo: bool = True,
                 client: httpx.AsyncClient | None = None):
        self.url = collector_url.rstrip("/") + "/events"
        self.agent_id = agent_id
        self.echo = echo
        self._buf: list[dict[str, Any]] = []
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._headers = {"authorization": f"Bearer {token}"}
        self._task: asyncio.Task | None = None
        self._stopping = False

    def emit(self, kind: str, **data: Any) -> None:
        ev = {"ts": now(), "kind": kind, "claimed_agent_id": self.agent_id, "data": data}
        self._buf.append(ev)
        if self.echo:
            print(json.dumps({"agent": self.agent_id, "kind": kind, **data}, default=str)[:1000], flush=True)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(0.5)
            await self.flush()

    async def flush(self) -> None:
        if not self._buf:
            return
        batch, self._buf = self._buf, []
        try:
            r = await self._client.post(self.url, content=json.dumps(batch, default=str),
                                        headers={**self._headers, "content-type": "application/json"})
            r.raise_for_status()
        except httpx.HTTPError:
            self._buf = batch + self._buf

    async def close(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
        for _ in range(5):
            await self.flush()
            if not self._buf:
                break
            await asyncio.sleep(1)
