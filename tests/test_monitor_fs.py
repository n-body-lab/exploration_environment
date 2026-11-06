"""Monitor filesystem logic: workspace versioning, shared trace views, read auditing."""

import json
import tarfile

from hivemarket.common.config import TransparencyConfig
from hivemarket.monitor.fs import PortProbe, ReadAuditor, SharedViewPublisher, WorkspaceRecorder


def collect():
    events = []
    return events, lambda kind, **d: events.append((kind, d))


def test_workspace_versions_dedup_delete_and_snapshot(tmp_path):
    ws, run = tmp_path / "ws", tmp_path / "run"
    ws.mkdir()
    events, emit = collect()
    rec = WorkspaceRecorder(ws, run, "agent-1", emit)

    f = ws / "notes" / "plan.sh"
    f.parent.mkdir()
    f.write_text("curl agent-2:7000")
    rec.on_write(f)
    rec.on_write(f)  # unchanged content -> no new version
    f.write_text("nc agent-2 7000")
    rec.on_write(f)
    (ws / ".hidden").write_text("x")
    rec.on_write(ws / ".hidden")
    f.unlink()
    rec.on_delete(f)

    writes = [d for k, d in events if k == "file_write"]
    assert [w["path"] for w in writes] == ["/home/sandbox/notes/plan.sh"] * 2 + ["/home/sandbox/.hidden"]
    assert writes[0]["preview"] == "curl agent-2:7000"
    versions = sorted((run / "artifacts" / "agent-1" / "workspace_versions" / "notes" / "plan.sh").iterdir())
    assert [v.read_text() for v in versions] == ["curl agent-2:7000", "nc agent-2 7000"]  # deleted file survives
    delete = next(d for k, d in events if k == "file_delete")
    assert delete["last_sha256"] == writes[1]["sha256"]

    archive = rec.snapshot()
    with tarfile.open(archive) as t:
        assert "home/sandbox/.hidden" in t.getnames()


def test_workspace_large_file_metadata_only(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    events, emit = collect()
    rec = WorkspaceRecorder(ws, tmp_path / "run", "agent-1", emit, max_bytes=10)
    (ws / "big.bin").write_bytes(b"x" * 100)
    rec.on_write(ws / "big.bin")
    assert events == [("file_write", {"area": "workspace", "path": "/home/sandbox/big.bin", "size": 100,
                                      "version": None, "too_large": True})]


def trace(agent, ts, kind="llm_turn", **data):
    return {"event_id": f"{agent}{ts}", "ts": ts, "ts_iso": str(ts), "source": "agent", "agent_id": agent,
            "kind": kind, "data": data}


def test_shared_view_filters_fields_targets_and_delay(tmp_path):
    traces, view = tmp_path / "agent_traces.jsonl", tmp_path / "view"
    view.mkdir()
    lines = [
        trace("agent-2", 100.0, step=1, text="hi", reasoning=["secret plan"],
              tool_calls=[{"id": "x", "name": "buy", "args": {"item_id": "P-1"}}]),
        trace("agent-3", 101.0, step=1, text="not visible to agent-1"),
        trace("agent-2", 102.0, kind="tool_call", step=1, name="buy", output="ok"),
        trace("agent-2", 150.0, step=2, text="later"),
    ]
    traces.write_text("".join(json.dumps(x) + "\n" for x in lines))
    t = TransparencyConfig(mode="files", expose=["text", "tool_calls"], delay_seconds=20)
    events, emit = collect()
    pub = SharedViewPublisher(traces, view, "agent-1", ["agent-2"], t, emit)
    pub.start()
    assert (view / "agent-2.jsonl").read_text() == ""
    pub.poll(now=130.0)  # only events older than 110 are due
    recs = [json.loads(x) for x in (view / "agent-2.jsonl").read_text().splitlines()]
    assert recs == [{"ts": "100.0", "step": 1, "type": "turn", "text": "hi",
                     "tool_calls": [{"name": "buy", "args": {"item_id": "P-1"}}]}]  # no reasoning, no tool result
    assert not (view / "agent-3.jsonl").exists()
    pub.poll(now=200.0)
    assert len((view / "agent-2.jsonl").read_text().splitlines()) == 2
    assert not list(view.glob(".tmp-*"))


def test_read_auditor_attributes_reads_and_ignores_publisher():
    events, emit = collect()
    a = ReadAuditor("agent-1", emit)
    a.handle({"OPEN"}, ".tmp-agent-2.jsonl", False, 1.0)  # publisher writing
    a.handle({"OPEN"}, "", True, 2.0)  # ls /shared/agents
    a.handle({"OPEN"}, "agent-2.jsonl", False, 3.0)
    a.handle({"ACCESS"}, "agent-2.jsonl", False, 3.1)
    a.handle({"ACCESS"}, "agent-2.jsonl", False, 3.2)
    a.handle({"CLOSE_NOWRITE"}, "agent-2.jsonl", False, 3.3)
    a.handle({"OPEN"}, "agent-3.jsonl", False, 4.0)
    a.flush()
    assert events[0] == ("trace_list", {"reader": "agent-1", "path": "/shared/agents"})
    reads = [d for k, d in events if k == "trace_read"]
    assert reads[0]["target"] == "agent-2" and reads[0]["read_calls"] == 2 and reads[0]["closed_ts"] == 3.3
    assert reads[1]["target"] == "agent-3" and reads[1]["closed_ts"] is None


def test_read_auditor_ignores_access_while_agent_is_down():
    events, emit = collect()
    up = {"v": False}
    a = ReadAuditor("agent-1", emit, active=lambda: up["v"])
    a.handle({"OPEN"}, "", True)  # docker mounting the volume
    a.handle({"OPEN"}, "agent-2.jsonl", False)
    up["v"] = True
    a.handle({"OPEN"}, "agent-2.jsonl", False, 5.0)
    a.handle({"CLOSE_NOWRITE"}, "agent-2.jsonl", False, 6.0)
    up["v"] = False
    a.handle({"OPEN"}, "agent-2.jsonl", False)  # docker cp at teardown
    a.flush()
    assert [k for k, _ in events] == ["trace_read", "trace_access_unattributed"]
    assert events[1][1]["events"] == 3


def test_port_probe_reads_listening_sockets(tmp_path):
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    (tmp_path / "tcp").write_text(header + "   0: 00000000:1B58 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 1\n")
    (tmp_path / "tcp6").write_text(header)
    assert PortProbe(7000, tmp_path, ttl=0)()
    assert not PortProbe(7001, tmp_path, ttl=0)()
    (tmp_path / "tcp").write_text(header + "   0: 0B4D000A:1B58 0C4D000A:9C40 01 00000000:00000000 00:00000000 00000000 0 0 1\n")
    assert not PortProbe(7000, tmp_path, ttl=0)()  # established, not listening
