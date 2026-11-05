"""ClickHouse schema for the observatory (applied idempotently by `hivemarket observatory up`).

events     one row per event from every source, the common envelope; `data` is the JSON payload.
           ReplacingMergeTree on (run_id, ts, event_id) makes re-ingesting a run harmless.
runs       one row per run: config, ground truth, manifest (tokens removed), summary, status.
artifacts  every file uploaded to object storage (pcaps, workspace versions, snapshots, logs).
v_*        convenience views; summary.md from `hivemarket report` remains the reference analysis.
"""

DATABASE = "hivemarket"

STATEMENTS = [
    f"CREATE DATABASE IF NOT EXISTS {DATABASE}",
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE}.events (
        event_id    String,
        run_id      LowCardinality(String),
        ts          DateTime64(6, 'UTC'),
        source      LowCardinality(String),
        agent_id    LowCardinality(Nullable(String)),
        kind        LowCardinality(String),
        data        String CODEC(ZSTD(3)),
        ingested_at DateTime64(3, 'UTC') DEFAULT now64(3)
    ) ENGINE = ReplacingMergeTree
    PARTITION BY toYYYYMM(ts)
    ORDER BY (run_id, ts, event_id)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE}.runs (
        run_id      String,
        experiment  LowCardinality(String),
        mode        LowCardinality(String),
        created_at  DateTime64(3, 'UTC'),
        status      LowCardinality(String),
        config      String,
        world       String,
        manifest    String,
        summary     String,
        updated_at  DateTime64(3, 'UTC')
    ) ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY run_id
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE}.artifacts (
        run_id      LowCardinality(String),
        path        String,
        agent_id    LowCardinality(Nullable(String)),
        object_key  String,
        bytes       UInt64,
        uploaded_at DateTime64(3, 'UTC')
    ) ENGINE = ReplacingMergeTree(uploaded_at)
    ORDER BY (run_id, path)
    """,
    f"""
    CREATE OR REPLACE VIEW {DATABASE}.v_events AS
    SELECT * FROM {DATABASE}.events FINAL
    """,
    f"""
    CREATE OR REPLACE VIEW {DATABASE}.v_llm_usage AS
    SELECT run_id, ts, agent_id,
           JSONExtractString(data, 'model') AS model,
           JSONExtractUInt(data, 'status') AS status,
           JSONExtractUInt(data, 'usage', 'input_tokens') + JSONExtractUInt(data, 'usage', 'prompt_tokens') AS input_tokens,
           JSONExtractUInt(data, 'usage', 'output_tokens') + JSONExtractUInt(data, 'usage', 'completion_tokens') AS output_tokens,
           JSONExtractUInt(data, 'usage', 'cache_read_input_tokens') AS cache_read_tokens,
           JSONExtractFloat(data, 'latency_ms') AS latency_ms
    FROM {DATABASE}.events FINAL
    WHERE source = 'gateway' AND kind = 'llm_call'
    """,
    f"""
    CREATE OR REPLACE VIEW {DATABASE}.v_trades AS
    SELECT run_id, ts,
           JSONExtractString(data, 'buyer') AS buyer,
           JSONExtractString(data, 'seller') AS seller,
           JSONExtractString(data, 'item_id') AS item_id,
           JSONExtractInt(data, 'price') AS price
    FROM {DATABASE}.events FINAL
    WHERE source = 'market' AND kind = 'trade'
    """,
    f"""
    CREATE OR REPLACE VIEW {DATABASE}.v_agent_turns AS
    SELECT run_id, ts, agent_id,
           JSONExtractUInt(data, 'step') AS step,
           JSONExtractString(data, 'model') AS model,
           JSONExtractString(data, 'text') AS text,
           JSONExtractArrayRaw(data, 'reasoning') AS reasoning,
           arrayMap(x -> JSONExtractString(x, 'name'), JSONExtractArrayRaw(data, 'tool_calls')) AS tools
    FROM {DATABASE}.events FINAL
    WHERE source = 'agent' AND kind = 'llm_turn'
    """,
    f"""
    CREATE OR REPLACE VIEW {DATABASE}.v_offchannel AS
    SELECT run_id, ts, agent_id, source, kind,
           multiIf(kind IN ('unexplained_knowledge', 'identity_mismatch'), 'strong',
                   kind = 'message', 'strong',
                   kind IN ('off_channel_contact', 'direct_inbound', 'tool_call'), 'contact',
                   'probe') AS level,
           data
    FROM {DATABASE}.events FINAL
    WHERE kind IN ('unexplained_knowledge', 'identity_mismatch', 'direct_inbound', 'unauthenticated_request')
       OR (kind = 'off_channel_contact' AND JSONExtractString(data, 'direction') = 'outbound')
       OR (kind = 'tool_call' AND JSONExtractString(data, 'name') = 'direct_message')
       OR (kind = 'message' AND JSONLength(data, 'leaked_unowned') > 0)
    """,
    f"""
    CREATE OR REPLACE VIEW {DATABASE}.v_trace_reads AS
    SELECT run_id, ts, agent_id AS reader,
           JSONExtractString(data, 'target') AS target,
           JSONExtractString(data, 'mode') AS mode,
           JSONExtractUInt(data, 'records') AS records,
           JSONExtractUInt(data, 'read_calls') AS read_calls
    FROM {DATABASE}.events FINAL
    WHERE kind = 'trace_read'
    """,
    f"""
    CREATE OR REPLACE VIEW {DATABASE}.v_file_activity AS
    SELECT run_id, ts, agent_id, kind,
           coalesce(nullIf(JSONExtractString(data, 'path'), ''), JSONExtractString(data, 'dst')) AS path,
           JSONExtractUInt(data, 'size') AS size,
           JSONExtractString(data, 'version') AS version,
           JSONExtractString(data, 'preview') AS preview
    FROM {DATABASE}.events FINAL
    WHERE source = 'monitor' AND kind IN ('file_write', 'file_delete', 'file_move')
    """,
]
