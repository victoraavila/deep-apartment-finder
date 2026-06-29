-- 003_sprint3.sql
-- Sprint 3: cross-portal dedup preparation + run reports support.
-- All changes are additive: 001 and 002 are not modified.

-- Best-effort deterministic key for cross-portal dedup. Populated by
-- the scrapers at ingest time. NULL for Sprint 1/2 rows; backfilled
-- by a one-off `backfill-dedup-keys` CLI command (see SPRINT3.md).
ALTER TABLE apartments
    ADD COLUMN IF NOT EXISTS dedup_key text;

-- Soft cross-portal dedup: at most one row per dedup_key. The
-- partial index lets Sprint 1/2 rows (NULL dedup_key) coexist
-- without forcing a backfill before Sprint 3 is enabled.
CREATE UNIQUE INDEX IF NOT EXISTS apartments_dedup_key_idx
    ON apartments (dedup_key)
    WHERE dedup_key IS NOT NULL;

-- Run reports: one row per CLI `run` invocation, pointing at the
-- persisted JSON on disk and carrying the phase-level counts for
-- quick SQL inspection without reading the file.
CREATE TABLE IF NOT EXISTS run_reports (
    id              bigserial PRIMARY KEY,
    run_id          uuid        NOT NULL UNIQUE,
    started_at      timestamptz NOT NULL,
    finished_at     timestamptz,
    phases          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    counts          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    ranking_run_id  uuid,
    notification_sent boolean,
    report_path     text,
    trace_url       text
);
CREATE INDEX IF NOT EXISTS run_reports_started_at_idx
    ON run_reports (started_at DESC);
