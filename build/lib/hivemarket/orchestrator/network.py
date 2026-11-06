"""Per-run subnet allocation so several Docker runs can execute in parallel.

Each run gets its own 10.77.N.0/24. A subnet is free if no Docker network
uses it and no other run has reserved it (reservation files in
<runs_dir>/.subnets, created atomically; stale after a day).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

STALE_SECONDS = 86400


def docker_used_subnets() -> set[str]:
    try:
        ids = subprocess.run(["docker", "network", "ls", "-q"], capture_output=True, text=True, check=True).stdout.split()
        if not ids:
            return set()
        out = subprocess.run(["docker", "network", "inspect", *ids], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return set()
    used = set()
    for net in json.loads(out or "[]"):
        for cfg in (net.get("IPAM") or {}).get("Config") or []:
            if cfg.get("Subnet"):
                used.add(cfg["Subnet"])
    return used


def _reservation(runs_dir: Path, n: int) -> Path:
    return Path(runs_dir) / ".subnets" / f"10.77.{n}.0"


def allocate_subnet(runs_dir: Path, run_id: str, used: set[str] | None = None) -> str:
    used = docker_used_subnets() if used is None else used
    (Path(runs_dir) / ".subnets").mkdir(parents=True, exist_ok=True)
    for n in range(1, 255):
        subnet = f"10.77.{n}.0/24"
        if subnet in used:
            continue
        path = _reservation(runs_dir, n)
        if path.exists() and time.time() - path.stat().st_mtime > STALE_SECONDS:
            path.unlink(missing_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as f:
            f.write(run_id)
        return subnet
    raise RuntimeError("no free 10.77.N.0/24 subnet")


def release_subnet(runs_dir: Path, subnet: str) -> None:
    n = int(subnet.split(".")[2])
    _reservation(runs_dir, n).unlink(missing_ok=True)


def host(subnet: str, last_octet: int) -> str:
    return subnet.rsplit(".", 1)[0] + f".{last_octet}"
