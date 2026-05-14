-- Migration: documents (generic ingested-document registry)
-- Backs the /library/ router and the document_search tool. One row per ingested
-- source (manual, datasheet, Wikipedia article, cookbook, etc.); per-chunk text
-- + embeddings live in ChromaDB collection `documents` keyed by doc_id+chunk_idx.
-- Run manually: docker compose exec -T postgres psql -U $POSTGRES_USER -d $POSTGRES_DB \
--                 < lumina-api/db/migrations/007_documents.sql

BEGIN;

CREATE TABLE IF NOT EXISTS documents (
    id            TEXT PRIMARY KEY,           -- e.g. "supernote_nomad_v1", "wiki_python_3_12"
    title         TEXT NOT NULL,
    source_path   TEXT NOT NULL,              -- on-disk path at ingest time (audit trail)
    sha256        TEXT NOT NULL,              -- content hash; lets us detect re-ingest of the same bytes
    page_count    INT NOT NULL,
    chunk_count   INT NOT NULL DEFAULT 0,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS documents_title_idx ON documents (title);

COMMIT;
