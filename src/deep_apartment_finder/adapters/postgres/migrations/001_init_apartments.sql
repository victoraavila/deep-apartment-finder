-- 001_init_apartments.sql
-- Sprint 1: Fotocasa ingestion MVP schema.
-- Single table `apartments`. Hard-filter columns are stored explicitly so they
-- can be queried with SQL. The `embedding` column is present and nullable,
-- prepared for Sprint 4 (no breakage when activated).
--
-- The migration is idempotent. The application is expected to apply every
-- .sql file under adapters/postgres/migrations in lexicographic order, exactly
-- once, and to record the applied version in a `_migrations` table managed by
-- the migration runner (added in a later step).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS apartments (
    id              bigserial PRIMARY KEY,
    source          text        NOT NULL,
    external_id     text        NOT NULL,
    url             text        NOT NULL,
    title           text,
    price_eur       numeric(10, 2),
    rooms           int,
    bathrooms       int,
    size_m2         numeric(7, 2),
    address         text,
    lat             numeric(9, 6),
    lng             numeric(9, 6),
    description     text,
    pet_policy      text,                 -- nullable; populated in Sprint 2
    raw_json        jsonb,                -- full raw payload for replay/debug
    scraped_at      timestamptz NOT NULL DEFAULT now(),
    embedding       vector(1536),         -- nullable; populated in Sprint 4

    -- Dedup contract: a (source, external_id) pair is unique.
    -- The repository AND the ingest_apartment tool rely on this constraint to
    -- surface "already ingested" gracefully (ON CONFLICT DO NOTHING).
    CONSTRAINT apartments_source_external_id_uniq
        UNIQUE (source, external_id)
);

-- Common access patterns in Sprint 1+: filter by source, sort by scraped_at.
CREATE INDEX IF NOT EXISTS apartments_source_scraped_at_idx
    ON apartments (source, scraped_at DESC);

-- Hard filter queries.
CREATE INDEX IF NOT EXISTS apartments_price_eur_idx
    ON apartments (price_eur);
CREATE INDEX IF NOT EXISTS apartments_rooms_bathrooms_idx
    ON apartments (rooms, bathrooms);
