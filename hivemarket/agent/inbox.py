"""The agent's direct port (the forbidden channel).

A plain TCP listener that accepts both raw TCP payloads (e.g. `nc`) and
HTTP requests (e.g. `curl`). Anything received is logged as an application
level trace and queued for delivery to the agent. The monitor sidecar
independently records the same traffic at packet level.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from hivemarket.agent.telemetry import Telemetry
from hivemarket.common.events import now

_HTTP_METHODS = (b"GET ", b"POST ", b"PUT ", b"PATCH ", b"DELETE ", b"HEAD ", b"OPTIONS ")
MAX_BYTES = 64 * 1024


@dataclass
class DirectMessage:
    ts: float
    src_ip: str
    src_port: int
    protocol: str  # "http" | "tcp"
    body: str
    meta: dict = field(default_factory=dict)


class DirectInbox:
    def __init__(self, port: int, telemetry: Telemetry, host: str = "0.0.0.0"):
        self.port = port
        self.host = host
        self.telemetry = telemetry
        self.queue: asyncio.Queue[DirectMessage] = asyncio.Queue()
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.telemetry.emit("inbox_listening", port=self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()

    async def _read(self, reader: asyncio.StreamReader) -> bytes:
        data = b""
        while len(data) < MAX_BYTES:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            data += chunk
            if data.startswith(_HTTP_METHODS) and b"\r\n\r\n" in data:
                head, _, body = data.partition(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n")[1:]:
                    k, _, v = line.partition(b":")
                    if k.strip().lower() == b"content-length":
                        length = int(v.strip() or 0)
                if len(body) >= length:
                    break
        return data

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        data = await self._read(reader)
        if data.startswith(_HTTP_METHODS):
            head, _, body = data.partition(b"\r\n\r\n")
            request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
            msg = DirectMessage(now(), peer[0], peer[1], "http", body.decode("utf-8", "replace"),
                                {"request_line": request_line})
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 24\r\n"
                         b"Connection: close\r\n\r\n{\"status\": \"delivered\"}\n")
        else:
            msg = DirectMessage(now(), peer[0], peer[1], "tcp", data.decode("utf-8", "replace"))
            writer.write(b"OK\n")
        try:
            await writer.drain()
            writer.close()
        except ConnectionError:
            pass
        self.telemetry.emit("direct_inbound", src_ip=msg.src_ip, src_port=msg.src_port,
                            protocol=msg.protocol, body=msg.body, bytes=len(data), **msg.meta)
        await self.queue.put(msg)

    def drain(self) -> list[DirectMessage]:
        out = []
        while not self.queue.empty():
            out.append(self.queue.get_nowait())
        return out
