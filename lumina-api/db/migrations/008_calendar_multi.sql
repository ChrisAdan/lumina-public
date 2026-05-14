-- Migration: calendar_multi
-- Tag every calendar_events row with its source calendar so Lumina can answer
-- "what's on Chris's calendar" vs "Sara's", and so the sync pipeline can hold
-- N calendars listed in obsidian/Lumina/Synapses/Calendars.md.
--
-- The google_event_id UNIQUE constraint becomes (google_event_id, calendar_id)
-- because the same event ID can legitimately appear in multiple synced calendars
-- (e.g., when you're invited to someone's event from a calendar you also sync).
--
-- Run manually: docker compose exec -T postgres psql -U $POSTGRES_USER -d $POSTGRES_DB \
--                 < lumina-api/db/migrations/008_calendar_multi.sql

BEGIN;

ALTER TABLE calendar_events
    ADD COLUMN IF NOT EXISTS calendar_id   TEXT,
    ADD COLUMN IF NOT EXISTS calendar_name TEXT;

ALTER TABLE calendar_events DROP CONSTRAINT IF EXISTS calendar_events_google_event_id_key;

ALTER TABLE calendar_events
    ADD CONSTRAINT calendar_events_event_calendar_uniq
    UNIQUE (google_event_id, calendar_id);

CREATE INDEX IF NOT EXISTS ix_calendar_events_calendar_id
    ON calendar_events(calendar_id);

COMMIT;
