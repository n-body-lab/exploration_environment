"""The observer UI's view of the observatory: read-only SQL on ClickHouse, and where Grafana is.

Queries run with ClickHouse's `readonly=1` (no writes, no setting changes) plus time and
row limits. Before that, only a single SELECT / WITH / SHOW / DESCRIBE / EXPLAIN statement
is accepted, so a mistake in the query box can't touch the data even on a misconfigured
server.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

READ_ONLY = re.compile(r"^\s*(SELECT|WITH|SHOW|DESCRIBE|DESC|EXPLAIN)\b", re.I)
FORMAT_CLAUSE = re.compile(r"\bFORMAT\s+\w+\s*;?\s*$", re.I)
MAX_ROWS = 5000
DASHBOARD_UID = "hivemarket-run"

# Starting points for the query box; {run_id} is replaced with the selected run, quoted.
EXAMPLES = [
    ("Rounds of this run", "SELECT round, outcome, reason, credits_spent, ideal_credits, excess_over_ideal, duration_s, "
                           "credits_consistent\nFROM hivemarket.v_rounds\nWHERE run_id = {run_id}\nORDER BY round"),
    ("Backchannel messages", "SELECT ts, round, sender, recipient, port, status, message, observed_by\n"
                             "FROM hivemarket.v_backchannel\nWHERE run_id = {run_id}\nORDER BY ts"),
    ("Off-channel indicators", "SELECT ts, agent_id, level, kind, source\nFROM hivemarket.v_offchannel\n"
                               "WHERE run_id = {run_id}\nORDER BY ts"),
    ("Collusion signals", "SELECT ts, agent_id, round, tier, concept, round(score, 3) AS score, text\n"
                          "FROM hivemarket.v_signals\nWHERE run_id = {run_id}\nORDER BY score DESC\nLIMIT 100"),
    ("Exploration by agent and round", "SELECT agent_id, round, actions, exploratory_actions, "
                                       "round(exploration_ratio, 2) AS ratio, scans, peer_dns_lookups, categories\n"
                                       "FROM hivemarket.v_exploration\nWHERE run_id = {run_id}\n"
                                       "ORDER BY agent_id, round"),
    ("Token exchanges", "SELECT ts, round, seller, buyer, token, cost, relay\nFROM hivemarket.v_exchanges\n"
                        "WHERE run_id = {run_id}\nORDER BY ts"),
    ("Broker messages", "SELECT ts, round, sender, to, cost, body\nFROM hivemarket.v_messages\n"
                        "WHERE run_id = {run_id}\nORDER BY ts"),
    ("Credits over time", "SELECT ts, round, cause, agent, credits, delta\nFROM hivemarket.v_balances\n"
                          "WHERE run_id = {run_id}\nORDER BY ts, agent"),
    ("LLM usage by agent", "SELECT agent_id, model, count() AS calls, sum(input_tokens) AS input_tokens,\n"
                           "       sum(output_tokens) AS output_tokens, round(avg(latency_ms)) AS avg_latency_ms\n"
                           "FROM hivemarket.v_llm_usage\nWHERE run_id = {run_id}\nGROUP BY agent_id, model\n"
                           "ORDER BY agent_id"),
    ("Events by source and kind", "SELECT source, kind, count() AS n\nFROM hivemarket.events FINAL\n"
                                  "WHERE run_id = {run_id}\nGROUP BY source, kind\nORDER BY n DESC"),
    ("All runs", "SELECT run_id, experiment, mode, status, created_at,\n"
                 "       JSONExtractString(summary, 'rounds_outcome') AS outcomes\n"
                 "FROM hivemarket.runs FINAL\nORDER BY created_at DESC\nLIMIT 100"),
    ("Backchannel across runs", "SELECT run_id, count() AS messages, countIf(status = 'delivered') AS delivered,\n"
                                "       uniqExact(sender) AS senders, min(round) AS first_round\n"
                                "FROM hivemarket.v_backchannel\nGROUP BY run_id\nORDER BY run_id DESC"),
]


class QueryError(ValueError):
    pass


def _clickhouse_message(error: str) -> str:
    """The useful part of a ClickHouse error (it may come wrapped in a half-written JSON result)."""
    text = re.sub(r"^ClickHouse error \d+: ", "", error)
    m = re.search(r'"exception":\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        text = json.loads(f'"{m.group(1)}"')
    return re.sub(r"\s*\(version [^)]*\)\s*$", "", text.strip())


def check_sql(sql: str) -> str:
    """One read-only statement, without its own FORMAT clause (results come back as JSON)."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise QueryError("Empty query.")
    if ";" in sql:
        raise QueryError("One statement at a time.")
    if not READ_ONLY.match(sql):
        raise QueryError("Only SELECT, WITH, SHOW, DESCRIBE and EXPLAIN queries are allowed here.")
    if FORMAT_CLAUSE.search(sql):
        raise QueryError("Leave out FORMAT: results are returned as a table.")
    return sql


class Warehouse:
    """Lazily connects to the observatory configured on this machine (ObservatorySettings)."""

    def __init__(self, clickhouse: Any = None, grafana_url: str | None = None):
        self._ch = clickhouse
        self._grafana_url = grafana_url
        self._loaded = clickhouse is not None

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            from hivemarket.observatory.clients import ClickHouse
            from hivemarket.observatory.settings import ObservatorySettings

            s = ObservatorySettings.load()
            self._ch = ClickHouse.from_settings(s)
            self._grafana_url = self._grafana_url or s.grafana_url
        except Exception:  # no observatory configured on this machine
            self._ch = None

    @property
    def grafana_url(self) -> str:
        self._load()
        return (self._grafana_url or "http://127.0.0.1:3000").rstrip("/")

    def status(self) -> dict[str, Any]:
        self._load()
        grafana_ok = False
        try:
            grafana_ok = httpx.get(f"{self.grafana_url}/api/health", timeout=2).status_code == 200
        except httpx.HTTPError:
            pass
        return {"clickhouse": bool(self._ch and self._ch.ping()), "grafana": grafana_ok,
                "grafana_url": self.grafana_url, "dashboard_uid": DASHBOARD_UID}

    def query(self, sql: str, limit: int = MAX_ROWS) -> dict[str, Any]:
        self._load()
        if not self._ch:
            raise QueryError("The observatory is not configured on this machine (hivemarket observatory up).")
        sql = check_sql(sql)
        limit = max(1, min(int(limit), MAX_ROWS))
        settings = {"readonly": 1, "max_execution_time": 30, "max_result_rows": limit,
                    "result_overflow_mode": "break", "output_format_json_quote_64bit_integers": 0}
        try:
            text = self._ch.execute(sql + "\nFORMAT JSONCompact", settings=settings)
        except RuntimeError as e:
            raise QueryError(_clickhouse_message(str(e))) from None
        out = json.loads(text)
        rows = out.get("data", [])
        return {
            "columns": out.get("meta", []),
            "rows": rows,
            "rows_returned": len(rows),
            "truncated": len(rows) >= limit or bool(out.get("rows_before_limit_at_least", 0) > len(rows)),
            "elapsed_s": (out.get("statistics") or {}).get("elapsed"),
            "rows_read": (out.get("statistics") or {}).get("rows_read"),
        }

    def schema(self) -> list[dict[str, Any]]:
        res = self.query("SELECT table, name, type FROM system.columns WHERE database = 'hivemarket' "
                         "ORDER BY table, position")
        tables: dict[str, list[dict[str, str]]] = {}
        for table, name, type_ in res["rows"]:
            tables.setdefault(table, []).append({"name": name, "type": type_})
        return [{"table": t, "columns": cols} for t, cols in tables.items()]
