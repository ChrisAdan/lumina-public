-- Migration 005: add embed_hash to tools table
-- Stores an MD5 prefix of each tool's description so embed_tools() can
-- skip re-embedding unchanged tools on every startup.
ALTER TABLE tools ADD COLUMN IF NOT EXISTS embed_hash TEXT;
