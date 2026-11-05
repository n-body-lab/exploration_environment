"""Netmon sidecar: python -m hivemarket.netmon

Runs in the agent container's network namespace (compose
`network_mode: service:<agent>`) with NET_RAW, which the agent itself does
not have, so the agent cannot see or stop it. tcpdump writes pcap to stdout;
we tee it to a pcap file (ground truth for later forensics) and parse it
live into flow events.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import dpkt

from hivemarket.common.events import EventLog, JsonlSink
from hivemarket.common.manifest import Manifest
from hivemarket.netmon.flows import FlowTracker, Packet


class TeeReader:
    def __init__(self, src, out):
        self.src, self.out = src, out

    def read(self, n: int = -1) -> bytes:
        data = self.src.read(n)
        if data:
            self.out.write(data)
            self.out.flush()
        return data


def _tcp_flags(tcp) -> set[str]:
    f = set()
    if tcp.flags & dpkt.tcp.TH_SYN:
        f.add("SYN")
    if tcp.flags & dpkt.tcp.TH_ACK:
        f.add("ACK")
    if tcp.flags & dpkt.tcp.TH_FIN:
        f.add("FIN")
    if tcp.flags & dpkt.tcp.TH_RST:
        f.add("RST")
    return f


def parse(ts: float, buf: bytes, linktype: int) -> Packet | None:
    try:
        if linktype == dpkt.pcap.DLT_LINUX_SLL:
            l2 = dpkt.sll.SLL(buf)
        elif linktype == 276:  # LINKTYPE_LINUX_SLL2
            l2 = dpkt.sll2.SLL2(buf)
        else:
            l2 = dpkt.ethernet.Ethernet(buf)
        ip = l2.data
        if isinstance(ip, dpkt.ip.IP):
            src, dst = socket.inet_ntop(socket.AF_INET, ip.src), socket.inet_ntop(socket.AF_INET, ip.dst)
        elif isinstance(ip, dpkt.ip6.IP6):
            src, dst = socket.inet_ntop(socket.AF_INET6, ip.src), socket.inet_ntop(socket.AF_INET6, ip.dst)
        else:
            return None
        l4 = ip.data
        if isinstance(l4, dpkt.tcp.TCP):
            return Packet(ts, "tcp", src, l4.sport, dst, l4.dport, _tcp_flags(l4), bytes(l4.data))
        if isinstance(l4, dpkt.udp.UDP):
            queries = []
            if 53 in (l4.sport, l4.dport) or dst == "127.0.0.11" or src == "127.0.0.11":
                try:
                    dns = dpkt.dns.DNS(bytes(l4.data))
                    if dns.qr == dpkt.dns.DNS_Q:
                        queries = [q.name for q in dns.qd]
                except (dpkt.UnpackError, IndexError):
                    pass
            return Packet(ts, "udp", src, l4.sport, dst, l4.dport, set(), bytes(l4.data), queries)
        if isinstance(l4, dpkt.icmp.ICMP):
            return Packet(ts, "icmp", src, 0, dst, 0, set(), b"")
        return None
    except (dpkt.UnpackError, ValueError, AttributeError):
        return None


def main() -> None:
    run_dir = Path(os.environ.get("RUN_DIR", "/run_data"))
    agent_id = os.environ["AGENT_ID"]
    manifest = Manifest.load(run_dir / "manifest.json")
    me = manifest.agent(agent_id)
    log = EventLog("netmon", manifest.run_id, JsonlSink(run_dir / "logs" / f"netmon-{agent_id}.jsonl"))
    emit_lock = threading.Lock()

    def emit(kind: str, **data):
        with emit_lock:
            log.emit(kind, agent_id=agent_id, **data)

    tracker = FlowTracker(
        agent_id,
        my_ips={me.ip},
        peers={a.ip: a.id for a in manifest.agents if a.id != agent_id},
        services={s.ip: name for name, s in manifest.services.items()},
        emit=emit,
        payload_skip={"gateway"},  # LLM traffic is fully logged by the gateway itself
    )
    pcap_path = run_dir / "pcap" / f"{agent_id}.pcap"
    pcap_path.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        ["tcpdump", "-i", "any", "-y", "LINUX_SLL", "-U", "-s", "0", "-n", "-w", "-"],
        stdout=subprocess.PIPE, stderr=sys.stderr,
    )
    time.sleep(1)  # give tcpdump time to attach before reporting healthy
    if proc.poll() is not None:
        emit("capture_failed", returncode=proc.returncode)
        sys.exit(1)
    Path("/tmp/capturing").touch()  # healthcheck: the agent container starts only after this
    emit("capture_started", interface="any", pcap=str(pcap_path.relative_to(run_dir)))

    tracker_lock = threading.Lock()

    def flusher():
        while proc.poll() is None:
            time.sleep(1)
            with tracker_lock:
                tracker.flush_idle()

    threading.Thread(target=flusher, daemon=True).start()

    def on_term(signum, frame):
        proc.terminate()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_term)
    with open(pcap_path, "wb") as out:
        reader = dpkt.pcap.Reader(TeeReader(proc.stdout, out))
        linktype = reader.datalink()
        try:
            for ts, buf in reader:
                pkt = parse(ts, buf, linktype)
                if pkt:
                    with tracker_lock:
                        tracker.feed(pkt)
        except (ValueError, dpkt.NeedData, SystemExit):
            pass
        finally:
            with tracker_lock:
                tracker.flush_idle(force=True)
            emit("capture_stopped")


if __name__ == "__main__":
    main()
