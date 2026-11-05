"""Thin HTTP client for the market API, used by the agent runtime's tools."""

from __future__ import annotations

from typing import Any

import httpx


class MarketClient:
    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient | None = None,
                 collector_url: str | None = None):
        self._c = client or httpx.AsyncClient(timeout=90.0)
        self._base = base_url.rstrip("/")
        self._collector = (collector_url or "").rstrip("/")
        self._h = {"authorization": f"Bearer {token}"}

    async def _req(self, method: str, path: str, base: str | None = None, **kw: Any) -> tuple[int, Any]:
        r = await self._c.request(method, (base or self._base) + path, headers=self._h, **kw)
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text}
        return r.status_code, body

    async def status(self):
        return await self._req("GET", "/me")

    async def catalog(self):
        return await self._req("GET", "/catalog")

    async def set_price(self, item_id: str, price: int | None, buyer: str | None = None):
        return await self._req("POST", "/price", json={"item_id": item_id, "price": price, "buyer": buyer})

    async def buy(self, item_id: str, max_price: int):
        return await self._req("POST", "/buy", json={"item_id": item_id, "max_price": max_price})

    async def send_message(self, to: str, body: str):
        return await self._req("POST", "/messages", json={"to": to, "body": body})

    async def submit(self, answer: str):
        return await self._req("POST", "/submit", json={"answer": answer})

    async def inbox(self, since: int, wait: float = 0):
        return await self._req("GET", "/inbox", params={"since": since, "wait": wait})

    async def read_traces(self, agent_id: str, cursor: int = 0):
        # Served by the collector (transparency tool mode), not the market.
        return await self._req("GET", f"/traces/{agent_id}", base=self._collector, params={"since": cursor})
