"""ClickHouse schema for the observatory (applied idempotently by `hivemarket observatory up`).

events     one row per event from every source, the common envelope; `data` is the JSON payload.
           ReplacingMergeTree on (run_id, ts, event_id) makes re-ingesting a run harmless.
           Market events of note: round_start, balances (every agent's credits after each change),
           graph_state (full market state after each change), exchange, message, buy_failed, submission,
           stuck_declared, round_end, game_over (see hivemarket/market/state.py).
runs       one row per run: config, ground truth, manifest (tokens removed), summary, status.
artifacts  every file uploaded to object storage (pcaps, workspace versions, snapshots, logs).
analysis   outputs of `hivemarket analyze` (backchannel, signal, exploration_action, exploration_summary,
           former_holder_buy);
           replaced wholesale whenever a run is re-analysed.
embeddings one vector per agent text unit; search with cosineDistance(vector, ...).
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
    CREATE TABLE IF NOT EXISTS {DATABASE}.analysis (
        run_id      LowCardinality(String),
        kind        LowCardinality(String),
        ts          DateTime64(6, 'UTC'),
        agent_id    LowCardinality(Nullable(String)),
        data        String CODEC(ZSTD(3)),
        analyzed_at DateTime64(3, 'UTC')
    ) ENGINE = MergeTree
    ORDER BY (run_id, kind, ts)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE}.embeddings (
        run_id    LowCardinality(String),
        unit_id   String,
        agent_id  LowCardinality(String),
        ts        DateTime64(6, 'UTC'),
        round     Nullable(UInt32),
        kind      LowCardinality(String),
        model     LowCardinality(String),
        text      String CODEC(ZSTD(3)),
        vector    Array(Float32)
    ) ENGINE = MergeTree
    ORDER BY (run_id, unit_id)
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
    f"DROP VIEW IF EXISTS {DATABASE}.v_trades",
    f"""
    CREATE OR REPLACE VIEW {DATABASE}.v_exchanges AS
    SELECT run_id, ts,
           JSONExtractUInt(data, 'round') AS round,
           JSONExtractString(data, 'buyer') AS buyer,
           JSONExtractString(data, 'seller') AS seller,
           JSONExtractString(data, 'token') AS token,
           JSONExtractInt(data, 'cost') AS cost,
           JSONExtractBool(data, 'relay') AS relay
    FROM {DATABASE}.events FINAL
    WHERE source = 'market' AND kind = 'exchange'
    """,
    f"""
    CREATE OR REPLACE VIEW {DATABASE}.v_messages AS
    SELECT run_id, ts,
           JSONExtractUInt(data, 'round') AS round,
           JSONExtractString(data, 'sender') AS sender,
           JSONExtractString(data, 'to') AS to,
           JSONExtract(data, 'recipients', 'Array(String)') AS recipients,
           JSONExtractInt(data, 'cost') AS cost,
           JSONExtractString(data, 'body') AS body
    FROM {DATABASE}.events FINAL
    WHERE source = 'market' AND kind = 'message'
    """,
    f"""
    CREATE OR REPLACE VIEW {DATABASE}.v_rounds AS
    SELECT run_id, ts AS ended_at,
           JSONExtractUInt(data, 'round') AS round,
           JSONExtractUInt(data, 'of') AS rounds_total,
           JSONExtractString(data, 'outcome') AS outcome,
           JSONExtractString(data, 'reason') AS reason,
           JSONExtractInt(data, 'credits_spent') AS credits_spent,
           JSONExtractInt(data, 'credits_spent_on_exchanges') AS credits_on_tokens,
           JSONExtractInt(data, 'credits_spent_on_communication') AS credits_on_messages,
           JSONExtract(data, 'ideal_credits', 'Nullable(Int64)') AS ideal_credits,
           JSONExtract(data, 'excess_over_ideal', 'Nullable(Int64)') AS excess_over_ideal,
           JSONExtractFloat(data, 'duration_s') AS duration_s,
           JSONExtractUInt(data, 'messages') AS messages,
           JSONExtractRaw(data, 'who_spoke_to_whom') AS who_spoke_to_whom,
           JSONExtractRaw(data, 'credits_remaining') AS credits_remaining,
           JSONExtractBool(data, 'credits_check', 'consistent') AS credits_consistent,
           JSONExtractRaw(data, 'per_agent') AS per_agent
    FROM {DATABASE}.events FINAL
    WHERE source = 'market' AND kind = 'round_end'
    """,
    f"""
    CREATE OR REPLACE VIEW {DATABASE}.v_balances AS
    SELECT run_id, ts,
           JSONExtractUInt(data, 'round') AS round,
           JSONExtractString(data, 'cause') AS cause,
           kv.1 AS agent,
           kv.2 AS credits,
           JSONExtractInt(data, 'deltas', kv.1) AS delta
    FROM {DATABASE}.events FINAL
    ARRAY JOIN JSONExtractKeysAndValues(data, 'credits', 'Int64') AS kv
    WHERE source = 'market' AND kind = 'balances'
    """,
    # One row per graph_state snapshot (whole market state after each change), in causal order:
    # state_seq, not ts, since two changes can share a timestamp.
    f"""
    CREATE OR REPLACE VIEW {DATABASE}.v_graph_state AS
    SELECT run_id, ts,
           JSONExtractUInt(data, 'state_seq') AS state_seq,
           JSONExtractUInt(data, 'round') AS round,
           JSONExtractString(data, 'phase') AS phase,
           JSONExtractString(data, 'cause') AS cause,
           agent_id,
           JSONExtractInt(data, 'total_credits') AS total_credits,
           JSONExtract(data, 'credits', 'Map(String, Int64)') AS credits,
           JSONExtract(data, 'holdings', 'Map(String, Array(String))') AS holdings,
           JSONExtract(data, 'owners', 'Map(String, Array(String))') AS owners,
           JSONExtract(data, 'known', 'Map(String, Array(String))') AS known
    FROM {DATABASE}.events FINAL
    WHERE source = 'market' AND kind = 'graph_state'
    ORDER BY run_id, state_seq
    """,
    f"""
    CREATE OR REPLACE VIEW {DATABASE}.v_memory AS
    SELECT run_id, ts, agent_id,
           JSONExtractUInt(data, 'round') AS round,
           JSONExtractString(data, 'args', 'command') AS command,
           JSONExtractString(data, 'args', 'path') AS path,
           JSONExtractString(data, 'args', 'content') AS content,
           JSONExtractString(data, 'output') AS output
    FROM {DATABASE}.events FINAL
    WHERE source = 'agent' AND kind = 'tool_call' AND JSONExtractString(data, 'name') = 'memory'
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
       OR (kind = 'message' AND JSONLength(data, 'leaked_unheld') > 0)
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
    SELECT run_id, ts, agent_id, kind, JSONExtractString(data, 'area') AS area,
           coalesce(nullIf(JSONExtractString(data, 'path'), ''), JSONExtractString(data, 'dst')) AS path,
           JSONExtractUInt(data, 'size') AS size,
           JSONExtractString(data, 'version') AS version,
           JSONExtractString(data, 'preview') AS preview
    FROM {DATABASE}.events FINAL
    WHERE source = 'monitor' AND kind IN ('file_write', 'file_delete', 'file_move')
    """,
    f"""
    CREATE OR REPLACE VIEW {DATABASE}.v_backchannel AS
    SELECT run_id, ts,
           JSONExtract(data, 'round', 'Nullable(UInt32)') AS round,
           JSONExtractString(data, 'from') AS sender,
           JSONExtractString(data, 'to') AS recipient,
           JSONExtract(data, 'port', 'Nullable(UInt32)') AS port,
           JSONExtractString(data, 'status') AS status,
           JSONExtractString(data, 'message') AS message,
           JSONExtractString(data, 'response') AS response,
           JSONExtract(data, 'tokens_leaked', 'Array(String)') AS tokens_leaked,
           JSONExtract(data, 'observed_by', 'Array(String)') AS observed_by,
           JSONExtractString(data, 'via') AS via,
           JSONExtractString(data, 'reply_to') AS reply_to
    FROM {DATABASE}.analysis WHERE kind = 'backchannel'
    """,
    f"""
    CREATE OR REPLACE VIEW {DATABASE}.v_signals AS
    SELECT run_id, ts, agent_id,
           JSONExtract(data, 'round', 'Nullable(UInt32)') AS round,
           JSONExtractString(data, 'tier') AS tier,
           JSONExtractString(data, 'concept') AS concept,
           JSONExtractFloat(data, 'score') AS score,
           JSONExtractString(data, 'unit_kind') AS unit_kind,
           JSONExtractString(data, 'text') AS text,
           data
    FROM {DATABASE}.analysis WHERE kind = 'signal'
    """,
    f"""
    CREATE OR REPLACE VIEW {DATABASE}.v_exploration AS
    SELECT run_id, agent_id,
           JSONExtract(data, 'round', 'Nullable(UInt32)') AS round,
           JSONExtractUInt(data, 'actions') AS actions,
           JSONExtractUInt(data, 'exploratory_actions') AS exploratory_actions,
           JSONExtractFloat(data, 'exploration_ratio') AS exploration_ratio,
           JSONExtractUInt(data, 'new_action_types') AS new_action_types,
           JSONExtractUInt(data, 'blocked_attempts') AS blocked_attempts,
           JSONExtractUInt(data, 'scans') AS scans,
           JSONExtractUInt(data, 'peer_dns_lookups') AS peer_dns_lookups,
           JSONExtractRaw(data, 'categories') AS categories
    FROM {DATABASE}.analysis WHERE kind = 'exploration_summary'
    """,
    f"""
    CREATE OR REPLACE VIEW {DATABASE}.v_exploration_actions AS
    SELECT run_id, ts, agent_id,
           JSONExtract(data, 'round', 'Nullable(UInt32)') AS round,
           JSONExtractString(data, 'type') AS type,
           JSONExtract(data, 'categories', 'Array(String)') AS categories,
           JSONExtractString(data, 'detail') AS detail
    FROM {DATABASE}.analysis WHERE kind = 'exploration_action'
    """,
]
