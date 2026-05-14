-- Migration: google_integrations
-- Google integration tables: calendar_events, email_index, drive_index
-- Run manually: docker compose exec postgres psql -U $POSTGRES_USER -d $POSTGRES_DB \
--                 -f /app/db/migrations/002_google_integrations.sql

-- ── Calendar ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS calendar_events (
    id              SERIAL PRIMARY KEY,
    google_event_id TEXT UNIQUE NOT NULL,
    summary         TEXT,
    description     TEXT,
    location        TEXT,
    start_time      TIMESTAMPTZ,
    end_time        TIMESTAMPTZ,
    all_day         BOOLEAN DEFAULT FALSE,
    attendees       JSONB,
    status          TEXT,           -- confirmed | tentative | cancelled
    html_link       TEXT,
    synced_at       TIMESTAMPTZ DEFAULT NOW(),
    embedded_at     TIMESTAMPTZ     -- NULL until embedded into ChromaDB
);

CREATE INDEX IF NOT EXISTS ix_calendar_events_start
    ON calendar_events(start_time);

CREATE INDEX IF NOT EXISTS ix_calendar_events_status
    ON calendar_events(status);

-- ── Gmail ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS email_index (
    id          SERIAL PRIMARY KEY,
    message_id  TEXT UNIQUE NOT NULL,   -- Gmail message ID (immutable)
    thread_id   TEXT,
    subject     TEXT,
    sender      TEXT,
    received_at TIMESTAMPTZ,
    snippet     TEXT,                   -- Gmail's 100-char preview for reference
    embedded_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_email_index_received
    ON email_index(received_at);

CREATE INDEX IF NOT EXISTS ix_email_index_thread
    ON email_index(thread_id);

-- ── Google Drive ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS drive_index (
    id              SERIAL PRIMARY KEY,
    file_id         TEXT UNIQUE NOT NULL,   -- Drive file ID (immutable)
    name            TEXT,
    mime_type       TEXT,
    modified_time   TIMESTAMPTZ,
    embedded_at     TIMESTAMPTZ,
    chroma_doc_id   TEXT,                   -- ID used in drive_docs collection
    parse_mode      TEXT DEFAULT 'embed'    -- embed | postgres | skip
);

CREATE INDEX IF NOT EXISTS ix_drive_index_modified
    ON drive_index(modified_time);
