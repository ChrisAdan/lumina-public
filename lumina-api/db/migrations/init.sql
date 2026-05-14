-- Lumina schema
-- Runs automatically on first Postgres container start
-- (mounted at /docker-entrypoint-initdb.d/init.sql)
-- Safe to re-run: all statements use IF NOT EXISTS

-- ============================================================
-- CORE
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT UNIQUE,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS notes (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id),
    title       TEXT,
    content     TEXT,
    tags        TEXT[],
    created_at  TIMESTAMP DEFAULT NOW(),
    updated_at  TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- WEATHER
-- ============================================================

-- Each row is one forecast fetch snapshot. CRON adds one per day.
-- Always query latest row: ORDER BY fetched_at DESC LIMIT 1
CREATE TABLE IF NOT EXISTS weather_forecasts (
    id              SERIAL PRIMARY KEY,
    location_name   TEXT,
    latitude        NUMERIC(9,6),
    longitude       NUMERIC(9,6),
    fetched_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    forecast_date   DATE NOT NULL,
    today           JSONB,
    hourly          JSONB,
    daily           JSONB
);

-- ============================================================
-- OBSERVABILITY / AUDIT
-- ============================================================

CREATE TABLE IF NOT EXISTS observability_snapshots (
    id              SERIAL PRIMARY KEY,
    snapshot_at     TIMESTAMP DEFAULT NOW(),
    table_name      TEXT,
    row_count       INTEGER,
    snapshot_source VARCHAR(20) DEFAULT 'cron'
);

CREATE TABLE IF NOT EXISTS query_audit (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER REFERENCES users(id),
    query_type      TEXT,
    vertical        TEXT,
    executed_at     TIMESTAMP DEFAULT NOW(),
    duration_ms     INTEGER,
    success         BOOLEAN DEFAULT TRUE
);

-- ============================================================
-- SEED DATA
-- ============================================================

INSERT INTO users (name, email)
VALUES ('Lumina', 'lumina@local')
ON CONFLICT (email) DO NOTHING;
