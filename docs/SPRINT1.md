# Sprint 1 — Fotocasa ingestion MVP + quality validation

**Goal:** validate that we can extract valid Zaragoza apartments for free
using agents and persist them to Postgres, while establishing the project
skeleton along SOLID + Ports & Adapters lines.

This sprint intentionally excludes ranking, soft criteria, distance,
pet-policy extraction, notifications, embeddings, the cron scheduler, and
any second scraping source. See `ROADMAP.md` for principles, stack, execution
model, and the abstract view of later sprints — this document does not
repeat that material.

## Scope

### Deliverables
- Python project scaffold with `uv`, `.env.example`, and a `docker compose`
  stack for Postgres + pgvector.
- The layered package structure (`domain/`, `ports/`, `adapters/`, `agent/`,
  `tools/`, `main.py` as composition root).
- `PostgresApartmentRepository` + migration `001_init_apartments.sql`
  (raw SQL, applied via `asyncpg`), including a nullable
  `embedding vector(1536)` column prepared for Sprint 4 and a
  `UNIQUE(source, external_id)` constraint for dedup.
- `FotocasaScraper` adapter implementing `ScraperPort`: `httpx` +
  `selectolax` for SSR pages, `playwright` fallback for CSR list-detail
  pages. CSS/JSON-LD selectors isolated in `selectors.py` so they can be
  updated without touching parsing logic.
- `ingest_apartment` tool backed by an injected `ApartmentRepository` — no
  SQL inside the tool.
- `fotocasa_scraper` subagent (own tools, own filesystem subtree, own prompt)
  and an orchestrator that delegates to it via `task` and inspects the
  returned handoff.
- `CompositeBackend` with routes for `/fotocasa_scraper/` and
  `/orchestrator/`; other routes are added in later sprints.
- CLI entrypoints `run` and `validate-quality`.
- ADRs 001–005 under `docs/adr/`.

### Out of scope (explicitly deferred)
- Ranker, soft criteria, distance, pet-policy LLM extraction
- Notifications
- Embeddings (the column exists but stays null)
- Local cron (runs are manual via CLI in this sprint)
- Idealista and any second scraping source

## Hard filters applied at query time
Applied as URL filters on Fotocasa where supported; applied in Python
post-fetch otherwise:

- City: Zaragoza
- Rooms: ≥ 2
- Bathrooms: ≥ 2 (when the portal exposes a filter; else applied post-fetch)
- Size: ≥ 50 m²
- Price: ≤ €1,200/month (rental)

These are **hard** filters: a listing failing any of them is dropped. Soft
(scoring) criteria are introduced in Sprint 2.

## Database schema

Single table `apartments`. Hard-filter columns are stored explicitly so they
can be queried with SQL; `embedding` is present but nullable.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | `bigserial` | PK |
| `source` | `text` | e.g. `'fotocasa'` |
| `external_id` | `text` | portal's listing id |
| `url` | `text` | |
| `title` | `text` | |
| `price_eur` | `numeric(10,2)` | monthly rent |
| `rooms` | `int` | |
| `bathrooms` | `int` | |
| `size_m2` | `numeric(7,2)` | |
| `address` | `text` | |
| `lat` | `numeric(9,6)` | |
| `lng` | `numeric(9,6)` | |
| `description` | `text` | |
| `pet_policy` | `text` | nullable; populated in S2 |
| `raw_json` | `jsonb` | full raw payload for replay/debug |
| `scraped_at` | `timestamptz` | |
| `embedding` | `vector(1536)` | nullable; populated in S4 |

Constraints: `UNIQUE(source, external_id)` (drives dedup in the repository and
the `ingest_apartment` tool). Extension `pgvector` is created in the migration.

## Package layout
```
src/deep_apartment_finder/
  __init__.py
  __main__.py              # python -m ... -> cli
  cli.py                   # typer: run | validate-quality
  main.py                  # composition root (DI)
  config.py                # pydantic-settings
  llm.py                   # ChatGroq + fallback ChatOpenAI(opencode-go)
  domain/
    apartment.py           # Apartment (value object)
    source.py              # Source enum
    filters/hard.py         # HardFilters dataclass
  ports/
    apartment_repository.py # ApartmentRepository Protocol
    scraper.py             # ScraperPort Protocol
  adapters/
    postgres/
      connection.py
      repository.py        # PostgresApartmentRepository
      migrations/001_init_apartments.sql
    scrapers/
      base.py              # polite delays, retry, render fallback
      fotocasa/
        client.py
        listing_parser.py
        selectors.py
        scraper.py        # FotocasaScraper(ScraperPort)
  filesystem/
    routes.py             # CompositeBackend routes factory
    trees/                # .gitkeep-seeded subtrees + README per folder
  tools/
    ingest.py             # ingest_apartment(repo) -> tool
    fotocasa/
      search_listings.py  # tool (uses injected FotocasaScraper)
      fetch_listing.py
      save_snapshot.py    # forces prefix /fotocasa_scraper/raw/
  subagents/
    fotocasa_scraper.py
    prompts/
      orchestrator.md
      fotocasa_scraper.md
  agent/
    orchestrator.py        # create_deep_agent(...)
tests/
  unit/ ...               # InMemoryApartmentRepository, fake scraper
  integration/test_orchestrator_fotocasa.py
  conftest.py
```
External support files: `pyproject.toml`, `docker-compose.yml`,
`.env.example`, `README.md`, `docs/adr/001..005-*.md`.

## Per-subagent filesystem
Pre-seeded (`.gitkeep`-committed) subtrees, each with a `README.md`
documenting its contract so humans and the LLM agree on purpose:

```
/fotocasa_scraper/
  raw/         HTML/JSON snapshots for replay & debug
  extracted/   normalized JSON before ingest
  cache/       URLs already seen (pre-DB cross-run dedup)
  selectors/   editable selectors (no recompile)
  logs/        scraper run logs
/orchestrator/
  plans/       exported TodoLists / plans
  reports/     final per-run reports
```
Routes `/fotocasa_scraper/` and `/orchestrator/` are persistent
(`StoreBackend`); writes outside are ephemeral (`StateBackend`). Routes for
`/ranker/`, `/notifier/`, and `/memories/` are added in later sprints.

## LLM provider handling
`llm.py` exposes a factory returning a `ChatGroq`-backed model, with a
`ChatOpenAI` fallback pointed at the opencode-go/glm endpoint discovered at
runtime from the opencode environment. The swap triggers on Groq rate-limit.
This is an adapter detail; the agent code only sees a chat model.

## Acceptance criteria
1. `docker compose up -d` brings up Postgres and runs the migration; the
   `apartments` table exists with the columns and constraints above.
2. `python -m deep_apartment_finder run` — the orchestrator plans, delegates
   via `task` to `fotocasa_scraper`, which scrapes Fotocasa in Zaragoza with
   the hard filters, persists new listings to Postgres, and returns a
   handoff; the orchestrator prints a summary.
3. Re-running `run` does not duplicate rows already ingested (dedup via the
   `UNIQUE` constraint, surfaced gracefully by the repository).
4. `python -m deep_apartment_finder validate-quality` dumps counts (total,
   new vs duplicate), and three sampled rows with price, rooms, bathrooms,
   size_m2, url, lat/lng, and a description preview. Fields are populated for
   the expected majority of rows.
5. Scraping degrades gracefully: if Fotocasa serves the detail page via CSR,
   the `playwright` fallback kicks in and the run still completes. No paid
   proxy or scraping service is used.

## Definition of done
- All acceptance criteria pass on a clean local machine.
- Unit tests cover `domain/`, `FotocasaScraper` parsing (fixture HTML), and
  the repository dedup path (`InMemoryApartmentRepository`).
- One integration test exercises the orchestrator → `fotocasa_scraper` →
  repository flow with a fake scraper.
- ADRs 001–005 committed under `docs/adr/`.