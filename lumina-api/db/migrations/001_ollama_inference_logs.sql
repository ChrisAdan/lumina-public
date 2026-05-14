-- Migration: ollama_inference_logs
-- Per-call Ollama inference telemetry. Companion to query_audit (HTTP layer).
-- Run manually: docker compose exec postgres psql -U $POSTGRES_USER -d $POSTGRES_DB \
--                 -f /app/db/migrations/001_ollama_inference_logs.sql

CREATE TABLE IF NOT EXISTS ollama_inference_logs (
    id                       SERIAL PRIMARY KEY,
    logged_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Request context
    request_id               TEXT,            -- correlate with query_audit via X-Request-ID
    model                    TEXT NOT NULL,
    vertical                 TEXT,            -- 'weather' | 'search' | 'recipes' | ...
    tool_context             TEXT,            -- e.g. 'groceries/receipt', 'briefing', 'search'
    prompt_preview           TEXT,            -- first 200 chars of prompt

    -- Phase 9 routing telemetry
    cache_hit                BOOLEAN NOT NULL DEFAULT FALSE,  -- semantic_cache hit, no Ollama call
    triage_bypassed          BOOLEAN NOT NULL DEFAULT FALSE,  -- triage skipped (manual /reason or /fast)

    -- Ollama timing fields (raw nanoseconds converted to ms)
    load_duration_ms         BIGINT,          -- HIGH = cold load, keep_alive not working
    prompt_eval_count        INT,             -- tokens in prompt
    prompt_eval_duration_ms  BIGINT,
    eval_count               INT,             -- tokens generated
    eval_duration_ms         BIGINT,
    total_duration_ms        BIGINT,

    -- Derived
    tokens_per_second        NUMERIC(8, 2),   -- eval_count / (eval_duration_ms / 1000)

    -- Outcome
    done_reason              TEXT,            -- 'stop' | 'length' | 'error'
    success                  BOOLEAN NOT NULL DEFAULT TRUE,
    error_message            TEXT
);

-- Index for time-series queries in Metabase
CREATE INDEX IF NOT EXISTS idx_ollama_logs_logged_at    ON ollama_inference_logs (logged_at DESC);
CREATE INDEX IF NOT EXISTS idx_ollama_logs_tool_context ON ollama_inference_logs (tool_context);
CREATE INDEX IF NOT EXISTS idx_ollama_logs_model        ON ollama_inference_logs (model);
CREATE INDEX IF NOT EXISTS idx_ollama_logs_vertical     ON ollama_inference_logs (vertical);
CREATE INDEX IF NOT EXISTS idx_ollama_logs_request_id   ON ollama_inference_logs (request_id);

COMMENT ON TABLE ollama_inference_logs IS
    'Per-call Ollama inference telemetry. High load_duration_ms = cold model load (keep_alive issue). tokens_per_second tracks throughput trends.';
COMMENT ON COLUMN ollama_inference_logs.load_duration_ms IS
    'Time Ollama spent loading model into memory. Repeatedly high = model evicted between calls. Fix: set OLLAMA_KEEP_ALIVE=-1.';
