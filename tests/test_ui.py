"""Observer UI: the API over run directories, the log tail, and the page's model (under Node)."""

import asyncio
import json
import shutil
import socket
import subprocess
from pathlib import Path

import httpx
import pytest
import uvicorn

from hivemarket.analysis.pipeline import analyze
from hivemarket.orchestrator.local import run_local
from hivemarket.orchestrator.run import create_run
from hivemarket.ui.server import MAX_STRING, LogTail, create_app, trim

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def finished_run(tmp_path_factory):
    """One scripted run with a rogue agent (direct messages), shared by the tests below."""
    runs = tmp_path_factory.mktemp("runs")
    run_dir = create_run(ROOT / "experiments" / "local_fail.yaml", runs, mode="local", local_base_port=19700)
    asyncio.run(run_local(run_dir))
    analyze(run_dir, embeddings="hashing")  # the CLI does this after every run
    return run_dir


def test_trim_bounds_long_strings_and_lists():
    ev = {"data": {"body": "x" * (MAX_STRING + 50), "items": list(range(500)), "n": 3}}
    out = trim(ev)
    assert len(out["data"]["body"]) < MAX_STRING + 40 and out["data"]["body"].endswith("[50 more chars]")
    assert len(out["data"]["items"]) == 201 and out["data"]["items"][-1] == "… [300 more items]"
    assert out["data"]["n"] == 3


def test_log_tail_reads_complete_lines_across_files(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    a = logs / "market.jsonl"
    a.write_text('{"event_id": "1", "kind": "x"}\n{"event_id": "2", "ki')  # second line still being written
    tail = LogTail(logs)
    assert [e["event_id"] for e in tail.read()] == ["1"]
    with open(a, "a") as f:
        f.write('nd": "y"}\n')
    (logs / "monitor-agent-1.jsonl").write_text('{"event_id": "3"}\nnot json\n')  # a new file appears
    got = tail.read()
    assert [(e["event_id"], e["_file"]) for e in got] == [("2", "market.jsonl"), ("3", "monitor-agent-1.jsonl")]
    assert tail.read() == []
    a.write_text('{"event_id": "4"}\n')  # replaced (shorter): read from the start again
    assert [e["event_id"] for e in tail.read()] == ["4"]


async def test_api_lists_runs_without_credentials(finished_run):
    app = create_app(finished_run.parent)
    manifest = json.loads((finished_run / "manifest.json").read_text())
    secrets = [manifest["admin_token"]] + [a["token"] for a in manifest["agents"]]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ui") as c:
        runs = (await c.get("/api/runs")).json()
        assert [r["run_id"] for r in runs] == [finished_run.name] and runs[0]["status"] == "finished"
        r = await c.get(f"/api/runs/{finished_run.name}")
        body = r.text
        assert r.status_code == 200 and not any(s in body for s in secrets)
        info = r.json()
        assert [a["id"] for a in info["agents"]] == ["agent-1", "agent-2", "agent-3"]
        assert set(info["world"]["nodes"]) == {"agent-1", "agent-2", "agent-3"}
        assert info["world"]["ideal"]["ideal_credits"] is not None
        bc = (await c.get(f"/api/runs/{finished_run.name}/backchannel")).json()
        assert bc["available"] and len(bc["records"]) == 2
        for bad in ("nope", "..", "..%2F..%2Fetc"):
            assert (await c.get(f"/api/runs/{bad}")).status_code == 404
        page = await c.get("/")
        assert page.status_code == 200 and "model.js" in page.text
        assert (await c.get("/static/app.js")).status_code == 200


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_stream_sends_backlog_then_new_events(tmp_path):
    run = tmp_path / "r1"
    (run / "logs").mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({"run_id": "r1", "agents": [], "created_at": 1}))
    log = run / "logs" / "market.jsonl"
    log.write_text(json.dumps({"event_id": "a", "ts": 1, "kind": "round_start"}) + "\n")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(tmp_path), host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    try:
        batches = []
        async with httpx.AsyncClient(timeout=10) as c:
            async with c.stream("GET", f"http://127.0.0.1:{port}/api/runs/r1/stream") as r:
                assert r.headers["content-type"].startswith("text/event-stream")
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        batches.append(json.loads(line[6:]))
                        if len(batches) == 1:  # backlog received: now the run writes something new
                            with open(log, "a") as f:
                                f.write(json.dumps({"event_id": "b", "ts": 2, "kind": "exchange"}) + "\n")
                        else:
                            break
        assert [[e["event_id"] for e in b] for b in batches] == [["a"], ["b"]]
    finally:
        server.should_exit = True
        await task


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
async def test_page_model_against_a_real_run(finished_run, tmp_path):
    app = create_app(finished_run.parent)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ui") as c:
        info = (await c.get(f"/api/runs/{finished_run.name}")).json()
    events = []
    for path in sorted((finished_run / "logs").glob("*.jsonl")):
        events += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    final = json.loads((finished_run / "final_state.json").read_text())
    fixture = {"events": sorted(events, key=lambda e: e["ts"]), "agents": info["agents"], "world": info["world"],
               "final": {"credits": final["credits"], "holds": final["holds"]},
               "rogue": "agent-3", "target": "agent-1", "expected_contacts": 4}
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture))
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "ui" / "model.test.js")],
                       env={"HM_FIXTURE": str(path), "PATH": "/usr/local/bin:/usr/bin:/bin"},
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


# ------------------------------------------------------------ warehouse (charts and SQL tabs)

class FakeClickHouse:
    """Records what the observer sends; answers like ClickHouse's JSONCompact output."""

    def __init__(self):
        self.calls = []

    def ping(self):
        return True

    def execute(self, sql, body=None, settings=None):
        self.calls.append((sql, settings))
        if "system.columns" in sql:
            data = [["v_rounds", "run_id", "String"], ["v_rounds", "round", "UInt32"], ["runs", "run_id", "String"]]
            meta = [{"name": "table", "type": "String"}, {"name": "name", "type": "String"}, {"name": "type", "type": "String"}]
        elif "boom" in sql:
            raise RuntimeError("ClickHouse error 500: Code: 47. Unknown identifier boom")
        else:
            data = [["r1", 1], ["r1", 2]]
            meta = [{"name": "run_id", "type": "String"}, {"name": "round", "type": "UInt32"}]
        return json.dumps({"meta": meta, "data": data, "rows": len(data), "statistics": {"elapsed": 0.002, "rows_read": 9}})


@pytest.mark.parametrize("sql, error", [
    ("INSERT INTO hivemarket.runs VALUES (1)", "Only SELECT"),
    ("DROP TABLE hivemarket.events", "Only SELECT"),
    ("SELECT 1; DROP TABLE hivemarket.events", "One statement"),
    ("SELECT 1 FORMAT CSV", "Leave out FORMAT"),
    ("   ", "Empty"),
])
def test_sql_guard_rejects_anything_but_one_read(sql, error):
    from hivemarket.ui.warehouse import QueryError, check_sql
    with pytest.raises(QueryError, match=error):
        check_sql(sql)
    assert check_sql("  with x as (select 1) select * from x;  ") == "with x as (select 1) select * from x"


async def test_sql_endpoints(tmp_path):
    from hivemarket.ui.warehouse import Warehouse
    fake = FakeClickHouse()
    app = create_app(tmp_path, warehouse=Warehouse(fake, grafana_url="http://grafana.test"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ui") as c:
        r = await c.post("/api/sql", json={"sql": "SELECT run_id, round FROM hivemarket.v_rounds", "limit": 50})
        body = r.json()
        assert r.status_code == 200 and body["rows"] == [["r1", 1], ["r1", 2]]
        assert [m["name"] for m in body["columns"]] == ["run_id", "round"] and body["elapsed_s"] == 0.002
        sql, settings = fake.calls[-1]
        assert sql.endswith("FORMAT JSONCompact")
        assert settings["readonly"] == 1 and settings["max_result_rows"] == 50 and settings["max_execution_time"] == 30
        r = await c.post("/api/sql", json={"sql": "DELETE FROM hivemarket.events WHERE 1"})
        assert r.status_code == 400 and "Only SELECT" in r.json()["error"] and len(fake.calls) == 1  # never sent
        r = await c.post("/api/sql", json={"sql": "SELECT boom"})
        assert r.status_code == 400 and "Unknown identifier boom" in r.json()["error"]
        schema = (await c.get("/api/sql/schema")).json()
        assert schema == [{"table": "v_rounds", "columns": [{"name": "run_id", "type": "String"},
                                                             {"name": "round", "type": "UInt32"}]},
                          {"table": "runs", "columns": [{"name": "run_id", "type": "String"}]}]
        examples = (await c.get("/api/sql/examples")).json()
        assert examples and all("{run_id}" in x["sql"] or "across runs" in x["title"] or x["title"] == "All runs"
                                for x in examples)
        status = (await c.get("/api/observatory")).json()
        assert status["clickhouse"] is True and status["grafana"] is False  # grafana.test doesn't exist
        assert status["grafana_url"] == "http://grafana.test" and status["dashboard_uid"] == "hivemarket-run"


async def test_example_queries_run_on_a_real_engine(tmp_path):
    """Every example query is valid against the real warehouse schema (chDB), with {run_id} filled in."""
    chdb_session = pytest.importorskip("chdb.session")
    from hivemarket.observatory.schema import STATEMENTS
    from hivemarket.ui.warehouse import EXAMPLES, check_sql
    s = chdb_session.Session(str(tmp_path / "ch"))
    s.query("CREATE DATABASE IF NOT EXISTS hivemarket")
    for stmt in STATEMENTS:
        s.query(stmt)
    for title, sql in EXAMPLES:
        s.query(check_sql(sql.replace("{run_id}", "'r1'")) + " FORMAT JSONCompact")  # raises if invalid


def test_clickhouse_errors_are_readable():
    from hivemarket.ui.warehouse import _clickhouse_message
    wrapped = ('ClickHouse error 500: {\n\t"meta": [],\n\t"rows": 0,\n\t"exception": "Code: 164. DB::Exception: '
               'Cannot modify \'readonly\' setting in readonly mode. (READONLY) (version 25.8.1.1)"\n}')
    assert _clickhouse_message(wrapped) == "Code: 164. DB::Exception: Cannot modify 'readonly' setting in readonly mode. (READONLY)"
    assert _clickhouse_message("ClickHouse error 500: Code: 47. Unknown identifier x (version 25.8)") == "Code: 47. Unknown identifier x"
