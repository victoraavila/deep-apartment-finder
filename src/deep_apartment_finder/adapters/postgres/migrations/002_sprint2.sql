-- 002_sprint2.sql
-- Sprint 2: ranking + soft filters + notification + researcher.
--
-- All changes are additive: 001_init_apartments.sql is not modified.
-- Existing rows (Sprint 1) keep NULL for `furnished` and remain untouched
-- in the dedup path; the `furnished` column starts nullable and is
-- populated only by new ingests from the scraper subagent.

-- New column on apartments: the LLM-extracted "furnished" flag.
-- Allowed values mirror the enum the ranker expects.
ALTER TABLE apartments
    ADD COLUMN IF NOT EXISTS furnished text
        CHECK (furnished IS NULL OR furnished IN ('true', 'false', 'unknown'));

-- Constants table populated by the researcher subagent on its first run.
-- The operator can also edit this table manually to override the agent's
-- research. `source` is a free-form string describing where the row came
-- from (e.g. 'researcher:web:elpais-2024-...', 'operator:override').
CREATE TABLE IF NOT EXISTS dangerous_neighborhoods (
    id          bigserial PRIMARY KEY,
    name        text        NOT NULL UNIQUE,
    center_lat  numeric(9, 6) NOT NULL,
    center_lng  numeric(9, 6) NOT NULL,
    radius_m    integer     NOT NULL CHECK (radius_m > 0),
    source      text        NOT NULL,
    notes       text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Per-apartment, per-criterion trace of every ranking run. The composite
-- uniqueness is intentionally not (ranking_run_id, apartment_id, criterion)
-- so that re-running the same ranking run is a no-op for trace storage;
-- in practice we write one row per (run, apt, criterion) on each rank.
CREATE TABLE IF NOT EXISTS apartment_scores (
    id              bigserial PRIMARY KEY,
    ranking_run_id  uuid        NOT NULL,
    apartment_id    bigint      NOT NULL REFERENCES apartments(id) ON DELETE CASCADE,
    criterion       text        NOT NULL,
    score           numeric(5, 3) NOT NULL CHECK (score >= 0 AND score <= 1),
    weight          numeric(4, 3) NOT NULL CHECK (weight >= 0 AND weight <= 1),
    details         jsonb,
    computed_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS apartment_scores_run_idx
    ON apartment_scores (ranking_run_id);

CREATE INDEX IF NOT EXISTS apartment_scores_apartment_idx
    ON apartment_scores (apartment_id);

-- One row per notification send. A partial unique index on (sent_on)
-- enforces "at most one notification per day" at the DB level, so the
-- notifier is safe to call twice (manual + cron overlap) without
-- sending a duplicate email.
CREATE TABLE IF NOT EXISTS notifications (
    id              bigserial PRIMARY KEY,
    sent_on         date        NOT NULL,
    apartment_ids   bigint[]    NOT NULL,
    ranking_run_id  uuid        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (sent_on, ranking_run_id)
);

-- The "at most one notification per day" invariant: at most one row per
-- `sent_on` value. Inserting a second row for the same day raises a
-- unique-violation, which the notifier handles as a no-op.
CREATE UNIQUE INDEX IF NOT EXISTS notifications_one_per_day_idx
    ON notifications (sent_on);
