"""Shipper -> ClickHouse/object storage, against a real ClickHouse engine (chDB) behind a fake HTTP endpoint."""

import json
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

chdb_session = pytest.importorskip("chdb.session")

from hivemarket.observatory.clients import ClickHouse  # noqa: E402
from hivemarket.observatory.shipper import Shipper  # noqa: E402
from hivemarket.orchestrator.local import run_local  # noqa: E402
from hivemarket.orchestrator.report import analyze, write_report  # noqa: E402
from hivemarket.orchestrator.run import create_run  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SETTING = re.compile(r"^[a-z_]+$")


class ChdbHTTP:
    """Speaks enough of the ClickHouse HTTP interface for ClickHouse(), executing on chDB."""

    def __init__(self, path):
        self.s = chdb_session.Session(str(path))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ping":
            return httpx.Response(200, text="Ok.\n")
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if params.get("database"):
            self.s.query(f"CREATE DATABASE IF NOT EXISTS {params['database']}")
            self.s.query(f"USE {params['database']}")
        if "query" in params:
            settings = ", ".join(f"{k}='{v}'" for k, v in params.items()
                                 if k not in ("query", "database") and SETTING.match(k))
            sql = params["query"]
            if settings:
                sql = sql.replace(" FORMAT ", f" SETTINGS {settings} FORMAT ", 1)
            sql += "\n" + request.content.decode()
        else:
            sql = request.content.decode()
        try:
            return httpx.Response(200, text=str(self.s.query(sql)))
        except Exception as e:
            return httpx.Response(500, text=str(e))


class MemoryStore:
    def __init__(self):
        self.objects = {}

    def ensure_bucket(self, bucket):
        pass

    def put_file(self, bucket, key, path):
        self.objects[key] = Path(path).read_bytes()

    def put_bytes(self, bucket, key, data, content_type="application/json"):
        self.objects[key] = data


@pytest.fixture(scope="module")
def finished_run(tmp_path_factory):
    import asyncio

    tmp = tmp_path_factory.mktemp("runs")
    run_dir = create_run(ROOT / "experiments" / "local_fail.yaml", tmp, mode="local", local_base_port=19600)
    asyncio.run(run_local(run_dir))
    write_report(run_dir)
    return run_dir


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    return ChdbHTTP(tmp_path_factory.mktemp("ch"))  # chDB allows one embedded server per process


@pytest.fixture
def ch(engine, finished_run):
    client = ClickHouse("http://ch.test:8123", "u", "p", transport=httpx.MockTransport(engine))
    client.apply_schema()
    for table in ("events", "runs", "artifacts"):
        client.execute(f"TRUNCATE TABLE hivemarket.{table}")
    shutil.rmtree(finished_run / ".shipper", ignore_errors=True)  # fresh warehouse -> fresh checkpoint
    return client


def count(ch, sql):
    return ch.query_json(sql)[0]["c"]


def test_ship_run_events_artifacts_and_run_row(finished_run, ch):
    store = MemoryStore()
    shipper = Shipper(finished_run, ch, store)
    shipper.start(interval=0.2)
    result = shipper.finish("finished")

    lines = sum(len(p.read_text().splitlines()) for p in (finished_run / "logs").glob("*.jsonl"))
    assert result["events"] == lines
    assert count(ch, "SELECT count() AS c FROM hivemarket.v_events") == lines

    # same exchanges and rounds as the local report
    summary = analyze(finished_run)
    assert count(ch, "SELECT count() AS c FROM hivemarket.v_exchanges") == sum(a["bought"] for a in summary["agents"].values())
    rounds = ch.query_json("SELECT round, outcome, credits_spent, ideal_credits FROM hivemarket.v_rounds ORDER BY round")
    assert [(r["round"], r["outcome"]) for r in rounds] == [(1, "fail"), (2, "fail")]
    assert rounds[0]["credits_spent"] == summary["rounds"][0]["credits_spent"]
    final = ch.query_json("SELECT agent, argMax(credits, ts) AS c FROM hivemarket.v_balances "
                          "WHERE round = 2 GROUP BY agent ORDER BY agent")
    assert {r["agent"]: r["c"] for r in final} == summary["rounds"][1]["credits_remaining"]
    memory = ch.query_json("SELECT agent_id, command, content FROM hivemarket.v_memory WHERE command = 'append'")
    assert {m["agent_id"] for m in memory} == {"agent-2"} and "round 1" in memory[0]["content"]
    assert count(ch, "SELECT count() AS c FROM hivemarket.v_offchannel WHERE kind = 'direct_inbound'") >= 1

    run = ch.query_json("SELECT * FROM hivemarket.runs FINAL")[0]
    secrets = [a["token"] for a in json.loads((finished_run / "manifest.json").read_text())["agents"]]
    assert run["status"] == "finished" and run["summary"]
    assert not any(t in run["manifest"] for t in secrets)

    keys = set(store.objects)
    run_id = finished_run.name
    assert f"runs/{run_id}/logs/market.jsonl" in keys and f"runs/{run_id}/world.json" in keys
    assert not any("/agents/" in k or k.endswith("docker-compose.yml") or k.endswith("/manifest.json") for k in keys)
    shipped = b"".join(store.objects.values()).decode("utf-8", "replace")
    assert not any(t in shipped for t in secrets), "an agent token was shipped"
    assert count(ch, "SELECT count() AS c FROM hivemarket.artifacts FINAL") == len(keys)


def test_reingest_is_idempotent(finished_run, ch, tmp_path):
    Shipper(finished_run, ch, None).finish()
    first = count(ch, "SELECT count() AS c FROM hivemarket.v_events")
    s = Shipper(finished_run, ch, None)
    assert s.sync()["events"] == 0  # checkpoint: nothing new
    s.reset()
    s.finish("ingested")  # full re-ship
    assert count(ch, "SELECT count() AS c FROM hivemarket.v_events") == first
    assert ch.query_json("SELECT status FROM hivemarket.runs FINAL")[0]["status"] == "ingested"


def test_dashboard_queries_run(finished_run, ch):
    Shipper(finished_run, ch, None).finish()
    dash = json.loads((ROOT / "observatory" / "grafana" / "dashboards" / "hivemarket.json").read_text())
    for panel in dash["panels"]:
        sql = panel["targets"][0]["rawSql"].replace("${run_id}", finished_run.name)
        ch.execute(sql)  # raises on SQL errors
    rows = ch.query_json(dash["templating"]["list"][0]["query"])
    assert rows[0]["run_id"] == finished_run.name
