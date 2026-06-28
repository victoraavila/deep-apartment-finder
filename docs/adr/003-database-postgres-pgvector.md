# ADR-003 — Database: Postgres + pgvector

- Status: Accepted (Sprint 1)
- Date: 2026-06-27

## Context

We need a database that:

1. Stores listings with hard-filter columns we can index and query cheaply.
2. Will need to store embeddings by Sprint 4 without a painful migration.
3. Runs locally on a laptop and is portable to a single VPS.
4. Plays well with raw SQL + `asyncpg` (no ORM, per project principles).

## Decision

- **Postgres 16** with the **`pgvector` extension**, run via `docker compose`
  for local dev and on a single VPS for Sprint 5.
- **Raw SQL + `asyncpg`**. Migrations are `.sql` files under
  `adapters/postgres/migrations/` and applied by a small runner in
  composition root.
- The Sprint 1 schema reserves a nullable `embedding vector(1536)` column so
  activating embeddings in Sprint 4 is a non-breaking change.

## Consequences

- One container, one volume, one connection pool. Simple to operate.
- `pgvector` is in the official `pgvector/pgvector` Docker image — no extra
  build step.
- Migrations are forward-only; we don't ship a downgrade path. New columns
  ship in new migrations.
- We do **not** use an ORM. The repository is a thin asyncpg adapter behind
  the `ApartmentRepository` protocol, and tests use an in-memory fake.
