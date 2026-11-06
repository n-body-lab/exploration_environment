"""Filesystem side of the agent monitor.

WorkspaceRecorder  versions every file the agent writes in its home directory
                   (a volume shared with this sidecar) into the run directory,
                   so scripts and notes survive teardown, including ones the
                   agent later deletes.
SharedViewPublisher  transparency `files` mode: writes the traces this agent
                   may read into its read-only view volume.
ReadAuditor        turns inotify open/read/close events on the view volume
                   into `trace_read` / `trace_list` events. The volume is
                   mounted only into this agent, so every access is this
                   agent's (per agent, not per process).

The classes are plain logic; `InotifySource` is the Linux-only event pump.
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from hivemarket.common.config import TransparencyConfig
from hivemarket.common.transparency import project

PREVIEW_CHARS = 2000
TMP_PREFIX = ".tmp-"

Emit = Callable[..., Any]


class WorkspaceRecorder:
    def __init__(self, root: Path, run_dir: Path, agent_id: str, emit: Emit, *,
                 max_bytes: int = 10_000_000, agent_root: str = "/home/sandbox", area: str = "workspace"):
        self.root = Path(root)
        self.run_dir = Path(run_dir)
        self.out = self.run_dir / "artifacts" / agent_id
        self.agent_id = agent_id
        self.area = area  # "workspace" or "memory"
        self._emit = emit
        self.max_bytes = max_bytes
        self.agent_root = agent_root
        self.last_sha: dict[str, str] = {}

    def emit(self, kind: str, **data: Any) -> None:
        self._emit(kind, area=self.area, **data)

    def _rel(self, path: Path) -> str:
        return str(Path(path).relative_to(self.root))

    def _agent_path(self, rel: str) -> str:
        return f"{self.agent_root}/{rel}"

    def on_write(self, path: Path) -> None:
        path = Path(path)
        try:
            if not path.is_file() or path.is_symlink():
                return
            size = path.stat().st_size
            rel = self._rel(path)
            if size > self.max_bytes:
                self.emit("file_write", path=self._agent_path(rel), size=size, version=None, too_large=True)
                return
            data = path.read_bytes()
        except (FileNotFoundError, PermissionError, ValueError):
            return
        sha = hashlib.sha256(data).hexdigest()
        if self.last_sha.get(rel) == sha:
            return
        self.last_sha[rel] = sha
        dest = self.out / f"{self.area}_versions" / rel / f"{time.time_ns()}-{sha[:12]}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        try:
            preview = data[:PREVIEW_CHARS * 4].decode("utf-8")[:PREVIEW_CHARS]
        except UnicodeDecodeError:
            preview = None
        self.emit("file_write", path=self._agent_path(rel), size=len(data), sha256=sha,
                  version=str(dest.relative_to(self.run_dir)), preview=preview, binary=preview is None)

    def on_delete(self, path: Path, is_dir: bool = False) -> None:
        rel = self._rel(path)
        self.emit("file_delete", path=self._agent_path(rel), is_dir=is_dir, last_sha256=self.last_sha.pop(rel, None))

    def on_move(self, src: Path | None, dst: Path | None) -> None:
        self.emit("file_move", src=self._agent_path(self._rel(src)) if src else None,
                  dst=self._agent_path(self._rel(dst)) if dst else None)
        if dst is not None and Path(dst).is_file():
            self.on_write(Path(dst))

    def snapshot(self) -> Path | None:
        if not self.root.exists():
            return None
        self.out.mkdir(parents=True, exist_ok=True)
        dest = self.out / f"{self.area}_final.tar.gz"
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(self.root, arcname=self.agent_root.strip("/"))
        self.emit("workspace_snapshot", archive=str(dest.relative_to(self.run_dir)), bytes=dest.stat().st_size)
        return dest


class SharedViewPublisher:
    def __init__(self, traces_path: Path, view_root: Path, reader: str, targets: list[str],
                 transparency: TransparencyConfig, emit: Emit):
        self.traces_path = Path(traces_path)
        self.view_root = Path(view_root)
        self.reader = reader
        self.targets = set(targets)
        self.t = transparency
        self.emit = emit
        self.offset = 0
        self.pending: dict[str, list[tuple[float, dict]]] = defaultdict(list)
        self.published: dict[str, list[dict]] = defaultdict(list)

    def start(self) -> None:
        for target in sorted(self.targets):
            self._write(target)  # empty files exist from the start, so the view is discoverable
        self.emit("shared_view_ready", reader=self.reader, targets=sorted(self.targets), mount=self.t.mount_path)

    def _ingest(self) -> None:
        if not self.traces_path.exists():
            return
        with open(self.traces_path, "rb") as f:
            f.seek(self.offset)
            chunk = f.read()
        if not chunk:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        self.offset += end + 1
        for line in chunk[: end + 1].splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("agent_id") in self.targets:
                rec = project(ev, self.t.expose)
                if rec:
                    self.pending[ev["agent_id"]].append((float(ev.get("ts", 0)), rec))

    def _write(self, target: str) -> None:
        tmp = self.view_root / f"{TMP_PREFIX}{target}.jsonl"
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in self.published[target]:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.view_root / f"{target}.jsonl")  # rename: never opens the visible file

    def poll(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._ingest()
        cutoff = now - self.t.delay_seconds
        for target, items in self.pending.items():
            due = [rec for ts, rec in items if ts <= cutoff]
            if due:
                self.pending[target] = [(ts, rec) for ts, rec in items if ts > cutoff]
                self.published[target].extend(due)
                self._write(target)


class ReadAuditor:
    """Consumes simplified inotify events from the view volume.

    Only accesses while the agent runtime is up are attributed to the agent
    (`active`). Docker touches the volume when it mounts it and when the run is
    torn down; those accesses are counted as unattributed instead.
    """

    def __init__(self, reader: str, emit: Emit, mount_path: str = "/shared/agents",
                 active: Callable[[], bool] = lambda: True):
        self.reader = reader
        self.emit = emit
        self.mount_path = mount_path
        self.active = active
        self.sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.unattributed = 0

    def handle(self, flags: set[str], name: str, is_dir: bool, ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        if name.startswith(TMP_PREFIX):
            return
        if not self.active():
            self.unattributed += 1
            return
        if is_dir or not name:
            if "OPEN" in flags:
                self.emit("trace_list", reader=self.reader, path=f"{self.mount_path}/{name}".rstrip("/"))
            return
        if "OPEN" in flags:
            self.sessions[name].append({"opened": ts, "reads": 0})
        elif "ACCESS" in flags:
            if not self.sessions[name]:
                self.sessions[name].append({"opened": ts, "reads": 0})
            self.sessions[name][-1]["reads"] += 1
        elif "CLOSE_NOWRITE" in flags and self.sessions[name]:
            self._finish(name, self.sessions[name].pop(0), ts)

    def _finish(self, name: str, session: dict[str, Any], closed: float | None) -> None:
        self.emit("trace_read", reader=self.reader, target=name.removesuffix(".jsonl"), mode="files",
                  file=f"{self.mount_path}/{name}", opened_ts=session["opened"], closed_ts=closed,
                  read_calls=session["reads"])

    def flush(self) -> None:
        for name, sessions in self.sessions.items():
            for s in sessions:
                self._finish(name, s, None)
        self.sessions.clear()
        if self.unattributed:
            self.emit("trace_access_unattributed", reader=self.reader, events=self.unattributed,
                      reason="agent runtime not running (docker mount/teardown)")


class PortProbe:
    """True while something listens on `port` in this network namespace (the agent's inbox).

    The monitor shares the agent's network namespace but not its process table,
    so the agent's listening socket is the reliable signal that its runtime is up.
    """

    def __init__(self, port: int, proc_net: Path = Path("/proc/net"), ttl: float = 0.5):
        self.hex_port = f":{port:04X}"
        self.files = [proc_net / "tcp", proc_net / "tcp6"]
        self.ttl = ttl
        self._cached: tuple[float, bool] = (0.0, False)

    def __call__(self) -> bool:
        now = time.monotonic()
        if now - self._cached[0] < self.ttl:
            return self._cached[1]
        up = False
        for f in self.files:
            try:
                for line in f.read_text().splitlines()[1:]:
                    cols = line.split()
                    if len(cols) > 3 and cols[1].endswith(self.hex_port) and cols[3] == "0A":  # 0A = LISTEN
                        up = True
                        break
            except OSError:
                continue
            if up:
                break
        self._cached = (now, up)
        return up


# ----------------------------------------------------------------- inotify

@dataclass
class FsEvent:
    path: Path
    name: str
    flags: set[str]
    is_dir: bool
    cookie: int


class InotifySource:
    """Recursive inotify watcher (Linux only)."""

    def __init__(self, root: Path, mask_names: Iterable[str], recursive: bool = True):
        from inotify_simple import INotify, flags  # Linux only

        self._flags = flags
        self.inotify = INotify()
        self.root = Path(root)
        self.recursive = recursive
        self.mask = 0
        for n in mask_names:
            self.mask |= getattr(flags, n)
        if recursive:
            self.mask |= flags.CREATE | flags.MOVED_TO
        self.paths: dict[int, Path] = {}
        self.add_tree(self.root)

    def add_tree(self, root: Path) -> None:
        dirs = [root] + ([p for p in root.rglob("*") if p.is_dir()] if self.recursive else [])
        for d in dirs:
            try:
                wd = self.inotify.add_watch(str(d), self.mask)
                self.paths[wd] = d
            except OSError:
                pass

    def read(self, timeout_ms: int = 1000) -> list[FsEvent]:
        out = []
        for ev in self.inotify.read(timeout=timeout_ms):
            base = self.paths.get(ev.wd)
            if base is None:
                continue
            names = {f.name for f in self._flags.from_mask(ev.mask)}
            is_dir = "ISDIR" in names
            path = base / ev.name if ev.name else base
            if self.recursive and is_dir and names & {"CREATE", "MOVED_TO"}:
                self.add_tree(path)
            out.append(FsEvent(path, ev.name, names, is_dir, ev.cookie))
        return out


def run_workspace_watch(recorder: WorkspaceRecorder, stop: threading.Event) -> None:
    src = InotifySource(recorder.root, ["CLOSE_WRITE", "DELETE", "MOVED_FROM", "MOVED_TO"])
    moves: dict[int, Path] = {}
    while not stop.is_set():
        for ev in src.read():
            try:
                if ev.is_dir and ev.flags & {"CREATE", "MOVED_TO"}:
                    # Files written before the new directory's watch existed would be missed.
                    for f in ev.path.rglob("*"):
                        if f.is_file():
                            recorder.on_write(f)
                elif "CLOSE_WRITE" in ev.flags and not ev.is_dir:
                    recorder.on_write(ev.path)
                elif "DELETE" in ev.flags:
                    recorder.on_delete(ev.path, ev.is_dir)
                elif "MOVED_FROM" in ev.flags:
                    moves[ev.cookie] = ev.path
                elif "MOVED_TO" in ev.flags:
                    recorder.on_move(moves.pop(ev.cookie, None), ev.path)
            except Exception as e:  # never let one odd file stop the recorder
                recorder.emit("monitor_error", where="workspace", error=repr(e), path=str(ev.path))


def run_read_audit(view_root: Path, auditor: ReadAuditor, stop: threading.Event) -> None:
    src = InotifySource(view_root, ["OPEN", "ACCESS", "CLOSE_NOWRITE"], recursive=False)
    while not stop.is_set():
        for ev in src.read():
            auditor.handle(ev.flags, ev.name, ev.is_dir)
