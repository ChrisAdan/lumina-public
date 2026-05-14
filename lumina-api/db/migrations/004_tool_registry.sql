-- Migration: tools (self-describing tool registry)
-- Each capability registers itself at startup with description + router + summary URL.
-- ChromaDB holds the embedding side; this table is the source of truth for
-- enable/disable, cost class, and the params schema.
-- Run manually: docker compose exec -T postgres psql -U $POSTGRES_USER -d $POSTGRES_DB \
--                 < lumina-api/db/migrations/004_tool_registry.sql

CREATE TABLE IF NOT EXISTS tools (
    id              TEXT PRIMARY KEY,
    router          TEXT NOT NULL,
    summary_url     TEXT,
    description     TEXT NOT NULL,
    params          JSONB NOT NULL DEFAULT '{}'::jsonb,
    cost_class      TEXT NOT NULL DEFAULT 'free',
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS tools_enabled_idx    ON tools (enabled);
CREATE INDEX IF NOT EXISTS tools_cost_class_idx ON tools (cost_class);
