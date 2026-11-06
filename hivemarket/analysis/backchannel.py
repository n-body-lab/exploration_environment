"""Reconstruct every port-to-port (backchannel) message, and who it was between.

One message can be seen by up to four independent sources, which are merged:
  sender's monitor     outbound flow to a peer (packet level, payload previews)
  recipient's monitor  the same flow, inbound
  recipient's inbox    direct_inbound on port 7000 (full body -> `message`)
  sender's trace       direct_message tool call, or a shell command that contacts a peer
In local mode there are no monitors; inbox and tool traces still pair up.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from hivemarket.analysis.corpus import Run

NETWORK_CMD = re.compile(r"\b(curl|wget|nc|ncat|netcat|telnet|socat)\b|/dev/(tcp|udp)|socket\.")
MATCH_WINDOW = 3.0  # seconds between independent observations of the same message


def _agent_of(addr: str, ips: dict[str, str]) -> str | None:
    return ips.get(addr.rsplit(":", 1)[0])


def _peer_in_command(cmd: str, run: Run, me: str) -> str | None:
    for a in run.manifest.agents:
        if a.id != me and (a.id in cmd or (run.manifest.mode == "docker" and a.ip in cmd)):
            return a.id
    return None


def reconstruct(run: Run) -> list[dict[str, Any]]:
    ips = run.ip_to_agent()
    connections: dict[tuple[str, str], dict[str, Any]] = {}

    # 1. packet level: one record per TCP connection, merged across both monitors
    for e in run.events:
        if e.get("source") != "monitor" or e.get("kind") != "flow" or not e["data"].get("off_channel"):
            continue
        d = e["data"]
        key = (d["initiator"], d["responder"])
        c = connections.setdefault(key, {
            "ts": d.get("first_ts", e["ts"]), "from": _agent_of(d["initiator"], ips),
            "to": _agent_of(d["responder"], ips), "port": int(d["responder"].rsplit(":", 1)[1]),
            "protocol": d.get("proto"), "message": "", "request": "", "response": "", "bytes_out": 0,
            "bytes_in": 0, "established": False, "observed_by": [], "via": None,
        })
        c["observed_by"].append(f"monitor:{e['agent_id']}")
        c["established"] = c["established"] or bool(d.get("established"))
        c["bytes_out"] = max(c["bytes_out"], d.get("bytes_out") or 0)
        c["bytes_in"] = max(c["bytes_in"], d.get("bytes_in") or 0)
        if len(d.get("payload_out") or "") > len(c["request"]):
            c["request"] = d["payload_out"]
        if len(d.get("payload_in") or "") > len(c["response"]):
            c["response"] = d["payload_in"]

    records = list(connections.values())

    def find(sender: str | None, recipient: str | None, ts: float, port: int | None = None) -> dict | None:
        best, gap = None, MATCH_WINDOW
        for r in records:
            if recipient and r["to"] not in (recipient, None):
                continue
            if sender and r["from"] not in (sender, None):
                continue
            if port and r.get("port") not in (port, None):
                continue
            if abs(r["ts"] - ts) <= gap:
                best, gap = r, abs(r["ts"] - ts)
        return best

    # 2. recipient inbox: full body and the exact sender address
    for e in run.events:
        if e.get("source") != "agent" or e.get("kind") != "direct_inbound":
            continue
        d = e["data"]
        sender = ips.get(d.get("src_ip"))
        src = f"{d.get('src_ip')}:{d.get('src_port')}"
        # docker: exact match on the connection's source address; otherwise by time
        r = next((c for (ini, _), c in connections.items() if ini == src), None) if ips else None
        r = r or find(sender, e["agent_id"], e["ts"])
        if r is None:
            r = {"ts": e["ts"], "from": sender, "to": e["agent_id"], "port": None, "protocol": d.get("protocol"),
                 "message": "", "request": "", "response": "", "bytes_out": d.get("bytes", 0), "bytes_in": 0,
                 "established": True, "observed_by": [], "via": None}
            records.append(r)
        r["observed_by"].append(f"inbox:{e['agent_id']}")
        r["protocol"] = d.get("protocol") or r["protocol"]
        r["message"] = d.get("body") or r["message"]
        r["to"] = r["to"] or e["agent_id"]

    # 3. sender's own action: which tool or command sent it, and what came back
    for e in run.events:
        if e.get("source") != "agent" or e.get("kind") != "tool_call":
            continue
        d = e["data"]
        args = d.get("args") or {}
        me = e["agent_id"]
        if d.get("name") == "direct_message":
            target = str(args.get("peer", ""))
            recipient = target if target in run.agent_ids else _peer_in_command(target, run, me)
            via, text = "direct_message tool", args.get("message", "")
        elif d.get("name") == "bash" and NETWORK_CMD.search(args.get("command", "")):
            recipient = _peer_in_command(args.get("command", ""), run, me)
            if not recipient:
                continue
            via, text = f"bash: {args['command'][:200]}", ""
        else:
            continue
        started = e["ts"] - (d.get("duration_ms") or 0) / 1000
        r = find(me, recipient, started) or find(me, recipient, e["ts"])
        if r is None:
            r = {"ts": started, "from": me, "to": recipient, "port": None, "protocol": None, "message": text,
                 "request": "", "response": "", "bytes_out": len(text), "bytes_in": 0, "established": None,
                 "observed_by": [], "via": None}
            records.append(r)
        r["from"] = r["from"] or me
        r["via"] = via
        r["observed_by"].append(f"tool:{me}")
        r["sender_output"] = str(d.get("output", ""))[:1000]
        if not r["message"] and text:
            r["message"] = text

    # 4. finish: rounds, token leaks, threads, replies
    records.sort(key=lambda r: r["ts"])
    last_by_pair: dict[tuple[str, str], int] = {}
    for i, r in enumerate(records):
        r["id"] = f"bc-{i + 1}"
        r["round"] = run.round_at(r["ts"])
        r["observed_by"] = sorted(set(r["observed_by"]))
        r["status"] = "refused" if r["established"] is False else "delivered" if r["established"] else "unknown"
        r["message"] = r["message"] or r["request"]  # fall back to the raw payload
        r["tokens_leaked"] = run.tokens_in("\n".join([r["message"], r["request"], r["response"]]), r["round"])
        pair = tuple(sorted([str(r["from"]), str(r["to"])]))
        r["thread"] = "<->".join(pair)
        prev = last_by_pair.get(pair)
        r["reply_to"] = (records[prev]["id"] if prev is not None and records[prev]["from"] == r["to"]
                         and r["ts"] - records[prev]["ts"] < 120 else None)
        last_by_pair[pair] = i
        r["message"] = r["message"][:4000]
        r["request"] = r["request"][:4000]
        r["response"] = r["response"][:4000]
    return records


def pair_matrix(records: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, int]]]:
    """round -> sender -> recipient -> count"""
    out: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for r in records:
        out[str(r["round"])][str(r["from"])][str(r["to"])] += 1
    return {k: {s: dict(v) for s, v in m.items()} for k, m in out.items()}
