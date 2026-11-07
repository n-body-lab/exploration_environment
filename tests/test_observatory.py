"""Shipper -> ClickHouse/object storage, against a real ClickHouse engine (chDB) behind a fake HTTP endpoint."""

import importlib.util
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
    engine = ChdbHTTP(tmp_path_factory.mktemp("ch"))  # chDB allows one embedded server per process
    yield engine
    engine.s.close()  # release it now, not whenever garbage collection gets to it, so another module can open one


@pytest.fixture
def ch(engine, finished_run):
    client = ClickHouse("http://ch.test:8123", "u", "p", transport=httpx.MockTransport(engine))
    client.apply_schema()
    for table in ("events", "runs", "artifacts", "analysis", "embeddings"):
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
    # the orchestrator's own admin close belongs to no agent, so it is no off-channel indicator
    assert count(ch, "SELECT count() AS c FROM hivemarket.v_offchannel WHERE kind = 'admin_request'") == 0

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


def test_analysis_loads_and_replaces(finished_run, ch):
    from hivemarket.analysis.pipeline import analyze

    analyze(finished_run, embeddings="hashing")
    s = Shipper(finished_run, ch, None)
    s.sync()
    first = count(ch, "SELECT count() AS c FROM hivemarket.analysis")
    rows = ch.query_json("SELECT sender, recipient, round FROM hivemarket.v_backchannel ORDER BY ts")
    assert [(r["sender"], r["recipient"], r["round"]) for r in rows] == [("agent-3", "agent-1", 1), ("agent-3", "agent-1", 2)]
    assert count(ch, "SELECT count() AS c FROM hivemarket.v_exploration") > 0
    assert count(ch, "SELECT count() AS c FROM hivemarket.embeddings") > 0
    # similarity search works on the stored vectors
    near = ch.query_json("SELECT text FROM hivemarket.embeddings ORDER BY cosineDistance(vector, "
                         "(SELECT vector FROM hivemarket.embeddings WHERE kind = 'backchannel_in' LIMIT 1)) LIMIT 1")
    assert "directly" in near[0]["text"]
    # re-analysis replaces instead of duplicating
    analyze(finished_run, embeddings="hashing")
    s.sync()
    assert count(ch, "SELECT count() AS c FROM hivemarket.analysis") == first


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


def test_market_probes_reach_the_warehouse(engine, probe_run):
    from hivemarket.analysis.pipeline import analyze as analyze_run

    client = ClickHouse("http://ch.test:8123", "u", "p", transport=httpx.MockTransport(engine))
    client.apply_schema()
    analyze_run(probe_run, embeddings="hashing")
    Shipper(probe_run, client, None).sync()
    rows = client.query_json(f"SELECT agent_id, detail FROM hivemarket.v_exploration_actions "
                             f"WHERE run_id = '{probe_run.name}' AND has(categories, 'market_probe')")
    assert len(rows) == 4 and {r["agent_id"] for r in rows} == {"agent-2"}
    dash = json.loads((ROOT / "observatory" / "grafana" / "dashboards" / "hivemarket.json").read_text())
    panel = next(p for p in dash["panels"] if p["title"].startswith("Market probes"))
    [row] = client.query_json(panel["targets"][0]["rawSql"].replace("${run_id}", probe_run.name))
    assert (row["agent_id"], row["round"], row["probes"]) == ("agent-2", 1, 4) and "allowed" in row["requests"]
    # the report's indicators show in the off-channel view too: the leaked admin token as strong
    levels = client.query_json(f"SELECT kind, level FROM hivemarket.v_offchannel WHERE run_id = '{probe_run.name}'")
    assert sorted((r["kind"], r["level"]) for r in levels) == [
        ("admin_request", "probe"), ("admin_request", "strong"), ("invalid_request", "probe"), ("invalid_request", "probe")]


@pytest.mark.skipif(importlib.util.find_spec("hivemarket_generator") is None,
                    reason='market_generator is not installed: pip install -e ".[generator]"')
def test_generated_run_difficulty_reaches_the_warehouse(ch, tmp_path):
    import asyncio

    run_dir = create_run(ROOT / "experiments" / "local_generated.yaml", tmp_path, mode="local", local_base_port=20000)
    asyncio.run(run_local(run_dir))
    write_report(run_dir)
    client = ch
    Shipper(run_dir, client, None).finish("finished")

    where = f"WHERE run_id = '{run_dir.name}'"
    [run] = client.query_json(f"SELECT * FROM hivemarket.v_run_difficulty {where}")
    world = json.loads((run_dir / "world.json").read_text())
    assert (run["n_agents"], run["n_tokens"], run["ideal_status"]) == (3, 4, "optimal")
    assert run["ideal_credits"] == world["ideal"]["ideal_credits"]
    assert run["shared_demand_fraction"] == world["generated"]["properties"]["interdependence"]["shared_demand_fraction"]
    rounds = client.query_json(f"SELECT credits_on_fees, ideal_status, economic_validation FROM hivemarket.v_rounds {where}")
    summary = json.loads((run_dir / "summary.json").read_text())["rounds"]
    assert [(r["credits_on_fees"], r["ideal_status"], r["economic_validation"]) for r in rounds] == \
        [(r["credits_spent_on_fees"], "optimal", "valid") for r in summary]
    fees = client.query_json(f"SELECT sum(buyer_fee + seller_fee) AS fees FROM hivemarket.v_exchanges {where}")[0]["fees"]
    assert fees == sum(r["credits_spent_on_fees"] for r in summary)
    # the final per-round check comes from the run's summary (where the host completes Docker runs)
    checks = client.query_json(f"SELECT round, status FROM hivemarket.v_round_validation {where} ORDER BY round")
    assert [(c["round"], c["status"]) for c in checks] == [(r["round"], "valid") for r in summary]


def test_failed_agents_and_what_they_did_after_reach_the_warehouse(ch, terminal_run):
    run_dir = terminal_run
    Shipper(run_dir, ch, None).finish("finished")

    where = f"WHERE run_id = '{run_dir.name}'"
    rows = ch.query_json(f"SELECT agent_id, kind, round, reason FROM hivemarket.v_agent_terminal {where} ORDER BY ts")
    assert [(r["agent_id"], r["kind"], r["round"]) for r in rows] == [("agent-3", "agent_terminal", 1),
                                                                     ("agent-3", "agent_terminal", 2)]
    after = ch.query_json(f"SELECT agent_id, category, count() AS c FROM hivemarket.v_post_terminal {where} "
                          "GROUP BY agent_id, category")
    counts = {r["category"]: r["c"] for r in after}
    assert {r["agent_id"] for r in after} == {"agent-3"}
    report = json.loads((run_dir / "summary.json").read_text())["agents"]["agent-3"]["post_terminal_actions"]
    assert counts["market"] == report["market"]  # the broker's own tags, without inbox polls
    assert counts["llm"] == report["llm_calls"] == 1  # placed when made, as the report does
    assert counts.get("off_channel", 0) >= 1  # the direct_message tool call
    dash = json.loads((ROOT / "observatory" / "grafana" / "dashboards" / "hivemarket.json").read_text())
    panel = next(p for p in dash["panels"] if p["title"].startswith("Agents marked FAILED"))
    rows = ch.query_json(panel["targets"][0]["rawSql"].replace("${run_id}", run_dir.name))
    assert [(r["agent_id"], r["failed_rounds"], r["market_requests"], r["llm_calls"]) for r in rows] == \
        [("agent-3", [1, 2], report["market"], 1)]
