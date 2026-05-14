-- Migration: semantic_cache
-- Durable audit/telemetry companion to the ChromaDB-backed semantic cache.
-- ChromaDB holds the actual embeddings + cached responses; this table makes
-- cache hit rate queryable in SQL (Phase 13 dashboards).
-- Run manually: docker compose exec -T postgres psql -U $POSTGRES_USER -d $POSTGRES_DB \
--                 < lumina-api/db/migrations/003_semantic_cache.sql

CREATE TABLE IF NOT EXISTS semantic_cache (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_text      TEXT NOT NULL,
    response_text   TEXT NOT NULL,
    vertical        TEXT,
    ttl_seconds     INT DEFAULT 3600,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    hit_count       INT NOT NULL DEFAULT 0,
    last_hit_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS semantic_cache_vertical_idx   ON semantic_cache (vertical);
CREATE INDEX IF NOT EXISTS semantic_cache_expires_at_idx ON semantic_cache (expires_at);
