"""Ships a run directory to the observatory.

Events  every logs/*.jsonl line -> ClickHouse `events` (checkpointed byte
        offsets; at-least-once, duplicates collapse on event_id).
Files   artifacts/ (workspace versions, snapshots, container diffs) while the
        run is live; pcaps, logs and run metadata at the end -> object storage
        under runs/<run_id>/..., each recorded in the `artifacts` table.
Run     a `runs` row with config, ground truth, manifest (tokens removed),
        summary and status.

Runs on the host, reading the run directory directly: nothing in a run's
Docker network can reach the observatory, and the run directory stays the
local source of truth that `hivemarket ingest` can re-ship at any time.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from hivemarket.common.events import iso, now

ENVELOPE = {"event_id", "run_id", "ts", "ts_iso", "source", "agent_id", "kind", "data"}
# Never shipped: contain agent tokens or are local bookkeeping.
PRIVATE = {"agents", "docker-compose.yml", "manifest.json", ".shipper", "timeline.jsonl"}
SETTLE_SECONDS = 2.0


class EventSink(Protocol):
    def insert(self, table: str, rows: list[dict[str, Any]]) -> int: ...


class FileSink(Protocol):
    def ensure_bucket(self, bucket: str) -> None: ...
    def put_file(self, bucket: str, key: str, path: Path) -> None: ...
    def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str = ...) -> None: ...


def to_row(ev: dict[str, Any]) -> dict[str, Any]:
    data = dict(ev.get("data") or {})
    extra = {k: v for k, v in ev.items() if k not in ENVELOPE}
    if extra:
        data["_meta"] = extra
    return {
        "event_id": ev["event_id"],
        "run_id": ev["run_id"],
        "ts": ev.get("ts_iso") or iso(float(ev["ts"])),
        "source": ev["source"],
        "agent_id": ev.get("agent_id"),
        "kind": ev["kind"],
        "data": json.dumps(data, default=str, ensure_ascii=False),
    }


def public_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    m = json.loads(json.dumps(manifest))
    m.pop("admin_token", None)
    for a in m.get("agents", []):
        a.pop("token", None)
    return m


class Shipper:
    def __init__(self, run_dir: Path, events: EventSink, files: FileSink | None, *, bucket: str = "hivemarket",
                 batch_size: int = 2000):
        self.run_dir = Path(run_dir)
        self.events = events
        self.files = files
        self.bucket = bucket
        self.batch_size = batch_size
        self.manifest = json.loads((self.run_dir / "manifest.json").read_text())
        self.run_id = self.manifest["run_id"]
        self.agent_ids = {a["id"] for a in self.manifest["agents"]}
        self.state_path = self.run_dir / ".shipper" / "state.json"
        self.state = self._load_state()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.errors = 0
        if self.files:
            self.files.ensure_bucket(self.bucket)

    # ------------------------------------------------------------ state

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {"offsets": {}, "uploaded": {}}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        tmp.replace(self.state_path)

    def reset(self) -> None:
        self.state = {"offsets": {}, "uploaded": {}}

    # ----------------------------------------------------------- events

    def sync_events(self) -> int:
        total = 0
        for path in sorted((self.run_dir / "logs").glob("*.jsonl")):
            key = path.name
            offset = self.state["offsets"].get(key, 0)
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read()
            end = chunk.rfind(b"\n")
            if end < 0:
                continue
            lines = chunk[: end + 1].splitlines()
            rows = []
            for line in lines:
                try:
                    rows.append(to_row(json.loads(line)))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue  # partial/corrupt line; the raw file keeps it
            for i in range(0, len(rows), self.batch_size):
                self.events.insert("events", rows[i: i + self.batch_size])
            total += len(rows)
            self.state["offsets"][key] = offset + end + 1
            self._save_state()
        return total

    # --------------------------------------------------------- analysis

    def sync_analysis(self) -> int:
        """Load analysis/ outputs, replacing any earlier analysis of this run."""
        summary = self.run_dir / "analysis" / "summary.json"
        if not summary.exists():
            return 0
        sig = summary.stat().st_mtime_ns
        if self.state.get("analysis") == sig:
            return 0
        analyzed_at = iso(json.loads(summary.read_text()).get("analyzed_at") or now())

        def rows(name: str) -> list[dict[str, Any]]:
            path = self.run_dir / "analysis" / name
            return [json.loads(line) for line in open(path)] if path.exists() else []

        out = []
        for kind, name, agent_key in (("backchannel", "backchannel.jsonl", "from"),
                                      ("signal", "signals.jsonl", "agent_id"),
                                      ("exploration_action", "exploration.jsonl", "agent_id"),
                                      ("exploration_summary", "exploration_summary.jsonl", "agent_id"),
                                      ("former_holder_buy", "former_holder.jsonl", "buyer")):
            for r in rows(name):
                k = "scan" if kind == "exploration_action" and r.get("type") == "scan" else kind
                ts = iso(float(r["ts"])) if r.get("ts") else analyzed_at
                out.append({"run_id": self.run_id, "kind": k, "ts": ts, "agent_id": r.get(agent_key),
                            "data": json.dumps(r, default=str, ensure_ascii=False), "analyzed_at": analyzed_at})
        vectors = [{**r, "ts": iso(float(r["ts"]))} for r in rows("embeddings.jsonl")]
        for table in ("analysis", "embeddings"):
            if hasattr(self.events, "delete_run"):
                self.events.delete_run(table, self.run_id)
        for i in range(0, len(out), self.batch_size):
            self.events.insert("analysis", out[i: i + self.batch_size])
        for i in range(0, len(vectors), 500):
            self.events.insert("embeddings", vectors[i: i + 500])
        self.state["analysis"] = sig
        self._save_state()
        return len(out)

    # ------------------------------------------------------------ files

    def _candidates(self, final: bool) -> list[Path]:
        roots = [self.run_dir / "artifacts"]
        if final:
            roots += [p for p in self.run_dir.iterdir() if p.name not in PRIVATE and p.name != "artifacts"]
        out = []
        for root in roots:
            if not root.exists():
                continue
            for p in [root] if root.is_file() else root.rglob("*"):
                top = p.relative_to(self.run_dir).parts[0]  # dotfiles deeper down (agent files) are kept
                if p.is_file() and top not in PRIVATE and not top.startswith("."):
                    out.append(p)
        return out

    def _agent_of(self, rel: str) -> str | None:
        parts = Path(rel).parts
        for part in parts:
            stem = part.split(".")[0]
            for candidate in (part, stem, stem.removeprefix("monitor-")):
                if candidate in self.agent_ids:
                    return candidate
        return None

    def sync_files(self, final: bool = False) -> int:
        if not self.files:
            return 0
        uploaded = 0
        rows = []
        cutoff = time.time() - (0 if final else SETTLE_SECONDS)
        for path in self._candidates(final):
            st = path.stat()
            if st.st_mtime > cutoff:
                continue  # still being written
            rel = str(path.relative_to(self.run_dir))
            sig = [st.st_size, st.st_mtime_ns]
            if self.state["uploaded"].get(rel) == sig:
                continue
            key = f"runs/{self.run_id}/{rel}"
            self.files.put_file(self.bucket, key, path)
            self.state["uploaded"][rel] = sig
            rows.append({"run_id": self.run_id, "path": rel, "agent_id": self._agent_of(rel), "object_key": key,
                         "bytes": st.st_size, "uploaded_at": iso(now())})
            uploaded += 1
        if final:
            data = json.dumps(public_manifest(self.manifest), indent=2).encode()
            key = f"runs/{self.run_id}/manifest.public.json"
            self.files.put_bytes(self.bucket, key, data)
            rows.append({"run_id": self.run_id, "path": "manifest.public.json", "agent_id": None,
                         "object_key": key, "bytes": len(data), "uploaded_at": iso(now())})
        if rows:
            self.events.insert("artifacts", rows)
        self._save_state()
        return uploaded

    # -------------------------------------------------------------- run

    def register_run(self, status: str) -> None:
        def read(name: str) -> str:
            p = self.run_dir / name
            return p.read_text() if p.exists() else ""

        self.events.insert("runs", [{
            "run_id": self.run_id,
            "experiment": self.manifest.get("experiment", ""),
            "mode": self.manifest.get("mode", ""),
            "created_at": iso(self.manifest.get("created_at") or now()),
            "status": status,
            "config": read("experiment.yaml"),
            "world": read("world.json"),
            "manifest": json.dumps(public_manifest(self.manifest)),
            "summary": read("summary.json"),
            "updated_at": iso(now()),
        }])

    def sync(self, final: bool = False) -> dict[str, int]:
        with self._lock:
            return {"events": self.sync_events(), "files": self.sync_files(final),
                    "analysis": self.sync_analysis()}

    # ------------------------------------------------------- background

    def start(self, interval: float = 2.0) -> None:
        self.register_run("running")

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.sync()
                except Exception as e:  # observatory hiccups must never stop the experiment
                    self.errors += 1
                    print(f"[shipper] {e!r} (will retry)", file=sys.stderr)

        self._thread = threading.Thread(target=loop, name="shipper", daemon=True)
        self._thread.start()

    def finish(self, status: str = "finished") -> dict[str, int]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)
        result = self.sync(final=True)
        self.register_run(status)
        return result
