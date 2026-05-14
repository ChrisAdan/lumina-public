-- 011_reminders.sql
-- Persistent reminder store for ntfy push notifications.
-- fire_at: one-shot reminders (scheduled datetime, UTC)
-- cron_expr: recurring reminders (5-field cron, e.g. "0 9 * * MON")
-- repeat=FALSE one-shots: enabled set to FALSE after first fire.
CREATE TABLE IF NOT EXISTS reminders (
    id            SERIAL PRIMARY KEY,
    message       TEXT        NOT NULL,
    fire_at       TIMESTAMPTZ,
    cron_expr     TEXT,
    repeat        BOOLEAN     NOT NULL DEFAULT FALSE,
    topic         TEXT        NOT NULL DEFAULT 'lumina-alerts',
    enabled       BOOLEAN     NOT NULL DEFAULT TRUE,
    last_fired_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
