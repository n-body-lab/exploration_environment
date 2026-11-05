"""Packet -> flow/event classification for one agent's network namespace.

Pure logic (no capture), fed with parsed packets so it can be unit tested.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

PREVIEW_BYTES = 4096
IDLE_FLUSH_SECONDS = 5.0


@dataclass
class Packet:
    ts: float
    proto: str  # "tcp" | "udp" | "icmp" | other
    src: str
    sport: int
    dst: str
    dport: int
    flags: set[str] = field(default_factory=set)  # SYN ACK FIN RST
    payload: bytes = b""
    dns_queries: list[str] = field(default_factory=list)


@dataclass
class Flow:
    key: tuple
    first_ts: float
    last_ts: float
    initiator: tuple[str, int]
    responder: tuple[str, int]
    proto: str
    packets: int = 0
    bytes_out: int = 0  # initiator -> responder
    bytes_in: int = 0
    preview_out: bytes = b""
    preview_in: bytes = b""
    established: bool = False
    reset: bool = False
    closed: bool = False


class FlowTracker:
    """Classifies traffic relative to one agent.

    `peers` maps IP -> agent id for every *other* agent; `services` maps
    IP -> service name (market, gateway, collector). Anything touching a peer
    IP is off-channel.
    """

    def __init__(self, agent_id: str, my_ips: set[str], peers: dict[str, str], services: dict[str, str],
                 emit: Callable[..., Any], payload_skip: set[str] | None = None):
        self.agent_id = agent_id
        self.my_ips = my_ips
        self.peers = peers
        self.services = services
        self.emit = emit
        self.payload_skip = payload_skip or set()  # service names whose payloads are logged elsewhere
        self.flows: dict[tuple, Flow] = {}

    def classify(self, ip: str) -> tuple[str, str | None]:
        if ip in self.peers:
            return "agent_peer", self.peers[ip]
        if ip in self.services:
            return "service", self.services[ip]
        if ip in self.my_ips or ip.startswith("127.") or ip == "::1":
            return "self", None
        return "unknown", None

    def _flow_key(self, p: Packet) -> tuple:
        a, b = (p.src, p.sport), (p.dst, p.dport)
        return (p.proto,) + tuple(sorted([a, b]))

    def feed(self, p: Packet) -> None:
        if p.dns_queries:
            self.emit("dns_query", queries=p.dns_queries, src=p.src, dst=p.dst, _ts=p.ts)

        key = self._flow_key(p)
        flow = self.flows.get(key)
        if flow is None:
            # For TCP the SYN sender is the initiator; otherwise the first packet's sender.
            if p.proto == "tcp" and "SYN" in p.flags and "ACK" in p.flags:
                initiator, responder = (p.dst, p.dport), (p.src, p.sport)
            else:
                initiator, responder = (p.src, p.sport), (p.dst, p.dport)
            flow = Flow(key, p.ts, p.ts, initiator, responder, p.proto)
            self.flows[key] = flow
            self._on_new(flow, p)

        flow.last_ts = p.ts
        flow.packets += 1
        outbound = (p.src, p.sport) == flow.initiator
        if outbound:
            flow.bytes_out += len(p.payload)
            if len(flow.preview_out) < PREVIEW_BYTES:
                flow.preview_out += p.payload[: PREVIEW_BYTES - len(flow.preview_out)]
        else:
            flow.bytes_in += len(p.payload)
            if len(flow.preview_in) < PREVIEW_BYTES:
                flow.preview_in += p.payload[: PREVIEW_BYTES - len(flow.preview_in)]
        if p.proto == "tcp":
            if "SYN" in p.flags and "ACK" in p.flags:
                flow.established = True
            if "RST" in p.flags:
                flow.reset = True
                self._finish(flow)
            elif "FIN" in p.flags:
                flow.closed = True

    def _describe(self, flow: Flow) -> dict[str, Any]:
        (ia, ip_), (ra, rp) = flow.initiator, flow.responder
        icat, iname = self.classify(ia)
        rcat, rname = self.classify(ra)
        if icat == "self" and rcat == "self":
            direction = "local"
        elif icat == "self":
            direction = "outbound"
        elif rcat == "self":
            direction = "inbound"
        else:
            direction = "transit"
        remote_cat, remote_name, remote_ip = (rcat, rname, ra) if direction == "outbound" else (icat, iname, ia)
        return {
            "proto": flow.proto,
            "direction": direction,
            "initiator": f"{ia}:{ip_}",
            "responder": f"{ra}:{rp}",
            "remote_ip": remote_ip,
            "remote_category": remote_cat,
            "remote_name": remote_name,
            "off_channel": remote_cat == "agent_peer",
        }

    def _on_new(self, flow: Flow, p: Packet) -> None:
        desc = self._describe(flow)
        self.emit("flow_start", **desc, syn=("SYN" in p.flags), _ts=p.ts)
        if desc["off_channel"]:
            self.emit("off_channel_contact", **desc, _ts=p.ts)

    def _finish(self, flow: Flow) -> None:
        if flow.key not in self.flows:
            return
        del self.flows[flow.key]
        desc = self._describe(flow)
        skip = desc["remote_category"] == "service" and desc["remote_name"] in self.payload_skip
        self.emit(
            "flow",
            **desc,
            _ts=flow.last_ts,
            first_ts=flow.first_ts,
            last_ts=flow.last_ts,
            duration_s=round(flow.last_ts - flow.first_ts, 4),
            packets=flow.packets,
            bytes_out=flow.bytes_out,
            bytes_in=flow.bytes_in,
            established=flow.established or flow.proto != "tcp",
            reset=flow.reset,
            payload_out=None if skip else flow.preview_out.decode("utf-8", "replace"),
            payload_in=None if skip else flow.preview_in.decode("utf-8", "replace"),
        )

    def flush_idle(self, now: float | None = None, force: bool = False) -> None:
        now = time.time() if now is None else now
        for flow in list(self.flows.values()):
            if force or flow.closed or now - flow.last_ts > IDLE_FLUSH_SECONDS:
                self._finish(flow)
