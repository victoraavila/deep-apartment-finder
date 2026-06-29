# Deep Apartment Finder

An AI agent that helps you find the rental apartments in Spain that best
match what you're looking for. It ingests listings from rental portals,
scores them against your hard filters (price, size, location, …) and
soft criteria (neighborhood safety, pet policy, furnished, …), and
emails you the top picks on a daily cadence — so you never miss a
listing that would have been perfect for you.

This repository is on **Sprint 3 — Observability + second scraping
source + cross-portal dedup preparation**. See
[`docs/SPRINT3.md`](docs/SPRINT3.md) for the active scope and
[`docs/ROADMAP.md`](docs/ROADMAP.md) for the full roadmap.

## Table of contents

- [Quick start (Sprint 3)](#quick-start-sprint-3)
- [Gmail App Password setup](#gmail-app-password-setup)
- [Daily cron (09:00 Europe/Madrid)](#daily-cron-0900-europemadrid)
- [CLI commands](#cli-commands)
- [Architecture (Sprint 3)](#architecture-sprint-3)
- [What's new in Sprint 3](#whats-new-in-sprint-3)

## Quick start (Sprint 3)

Prereqs: Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), Docker.

```bash
# 1. Install deps
uv sync --extra dev

# 2. Configure env
cp .env.example .env
# edit .env — set OPENCODE_API_KEY (and optionally GROQ_API_KEY as fallback),
# GMAIL_SMTP_ADDRESS + GMAIL_SMTP_APP_PASSWORD (for the daily notification),
# and EXA_API_KEY (for the researcher's first-run web search).
# IDEALISTA_ENABLED defaults to true; set to false to fall back to
# Fotocasa-only behaviour (no second scraper).

# 3. Bring up Postgres + pgvector
docker compose up -d

# 4. Apply migrations (idempotent; applies 001, 002, and 003)
uv run python -m deep_apartment_finder migrate

# 5. First run: the researcher subagent bootstraps the
#    dangerous-neighborhoods table. After it returns, re-run.
uv run python -m deep_apartment_finder run

# 6. Inspect the bootstrapped list, then re-run the full pipeline.
uv run python -m deep_apartment_finder list-dangerous
uv run python -m deep_apartment_finder run

# 7. Inspect what was ingested / ranked
uv run python -m deep_apartment_finder validate-quality
#   - per-source, per-field null rate
#   - count of rows with invalid coordinates
#   - count of cross-portal dedup-key collisions

# 8. Inspect a past run (Sprint 3 Pillar A)
uv run python -m deep_apartment_finder show-run <run-uuid>
```

## Gmail App Password setup

The notifier uses Gmail SMTP with an App Password (your normal
Gmail password will not work over SMTP).

1. Enable 2-Step Verification on your Google account.
2. Visit [https://myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Create an App Password (any name; e.g. "deep-apartment-finder").
4. Copy the 16-character password into `.env` as
   `GMAIL_SMTP_APP_PASSWORD=...` (no spaces). Set
   `GMAIL_SMTP_ADDRESS` to your Gmail address.

The first time the notifier runs after a misconfiguration, the
CLI logs a clear error and continues; the ranker output is
unaffected. Subsequent runs keep working.

## Daily cron (09:00 Europe/Madrid)

Add a single crontab entry on the host that runs the CLI:

```cron
TZ=Europe/Madrid
0 9 * * * cd /path/to/deep_apartment_finder && .venv/bin/python -m deep_apartment_finder run >> logs/cron.log 2>&1
```

The CLI is idempotent: the S1 `ingest` dedup + the S2
`notifications` partial unique index + the S3
`apartments_dedup_key_idx` make a second run on the same day a
no-op (the notifier logs "already notified today" and the
scraper subagent sees every listing as `duplicate` or
`updated`).

## CLI commands

| Command | What it does |
| --- | --- |
| `migrate` | Apply pending SQL migrations (001 + 002 + 003). |
| `run` | Drive the orchestrator end-to-end. Sprint 3 prints phase headers + counters to **stderr** in real time and persists a structured `RunReport` to `/orchestrator/reports/<run-uuid>.json` and the `run_reports` Postgres table. |
| `run --trace` | Compatibility flag. Tracing is enabled automatically whenever `LANGSMITH_API_KEY` is configured; without that key tracing stays off. |
| `validate-quality` | Sprint 3 added: per-source, per-field null rate; count of invalid coordinates; count of cross-portal dedup-key collisions. |
| `list-dangerous` | Print the bootstrapped dangerous-neighborhoods table. |
| `show-run <run-uuid>` | Re-print a persisted run report (Sprint 3 Pillar A). |
| `backfill-dedup-keys` | Compute `dedup_key` for every existing apartment row that has `NULL` (Sprint 3 Pillar F). Idempotent; collisions are logged and left `NULL`. |

## Architecture (Sprint 3)

```
src/deep_apartment_finder/
  domain/      # value objects (Apartment, HardFilters, Geo (is_valid_coordinate,
               #   compute_dedup_key), RankableApartment, RunReport)
               # soft criteria (distance_to_dangerous, pet_policy, furnished, registry)
               # ranking + notifier (deterministic Python)
  ports/       # abstractions (ApartmentRepository, ScraperPort,
               #   DangerousNeighborhoodRepository, RankingRepository,
               #   DistanceProvider, Notifier, RunObserver)
  adapters/    # concrete I/O (Postgres, Fotocasa, Idealista, Gmail SMTP, Haversine,
               #   CliRunObserver, RecordingRunObserver, langsmith tracing)
  tools/       # LangChain tools the agent can call
  subagents/   # registered LLM subagents (fotocasa_scraper, idealista_scraper,
               #   researcher) + prompts
  agent/       # orchestrator built with create_deep_agent
  filesystem/  # CompositeBackend routes per subagent
  cli.py       # typer entrypoints (migrate / run / validate-quality /
               #   list-dangerous / show-run / backfill-dedup-keys)
  main.py      # composition root
```

Decisions: [`docs/adr/`](docs/adr/).

## What's new in Sprint 3

- **Operator-facing run observability (Pillar A).** A
  `RunObserver` port with two adapters (`CliRunObserver` →
  stderr in real time; `RecordingRunObserver` →
  `/orchestrator/reports/<run-uuid>.json` + `run_reports`
  Postgres table). A new `show-run <run-uuid>` subcommand
  re-prints a persisted report.
- **LangSmith full-pipeline tracing (Pillar B).** Every
  significant operation carries a span: orchestrator
  planning, each scraper subagent, each `search_listings`
  page, each `fetch_listing`, each `ingest_apartment`, every
  Postgres read/write, `compute_ranking` (with per-criterion
  child spans), `render_email`, `send_email`, the dedup-skip
  path, and the report writes. Tracing is enabled automatically
  whenever `LANGSMITH_API_KEY` is configured.
- **Ranked result explainability (Pillar C).** The CLI stdout,
  the persisted run report, and the email body all show, for
  each top-N row: `title`, `price_eur`, `rooms`, `bathrooms`,
  `size_m2`, `address`, `url`, `final_score`, and the
  per-criterion `breakdown`. Three surfaces, same fields, same
  order.
- **Listing data quality before ranking (Pillar D).**
  - `is_valid_coordinate(lat, lng)` rejects `None`, `(0, 0)`,
    NaN/Inf, and any point outside a coarse Zaragoza bounding
    box. Invalid coordinates land in the DB as `NULL`, not
    `0`. `DistanceToDangerousCriterion` returns a neutral
    `0.5` for bogus coordinates (never rewards).
  - Duplicate backfill: the repository's `upsert` changed
    from `ON CONFLICT DO NOTHING` to `COALESCE` + `WHERE ...
    IS DISTINCT FROM ...`, returning a third `Updated` outcome
    with `changed_fields`. A Sprint 1 row with `NULL`
    `pet_policy` gets the value on the next scrape.
  - `validate-quality` reports per-source null rate +
    invalid-coordinate count + cross-portal dup count.
- **Second scraping source (Pillar E).** `IdealistaScraper`
  implementing the same `ScraperPort` (no orchestrator change).
  Strategy documented in ADR-011: `curl_cffi` Chrome 131
  impersonation, polite delay 2.0s, SSR search, card-only
  detail reconstruction (DataDome blocks the detail page for
  non-browser clients).
- **Cross-portal dedup preparation (Pillar F).** A new
  nullable `dedup_key` column populated by the scraper at
  ingest time with a deterministic hash
  (`sha1(normalized_address + rooms + size_bucket +
  price_bucket)`). A partial unique index provides soft
  cross-portal dedup. The ranker drops the lower-scoring
  sibling from the top-N. The `backfill-dedup-keys` CLI
  subcommand computes the key for existing rows. Full
  embeddings activation is Sprint 4 (Q2).
- **New ADRs** 009 / 010 / 011 / 012.
- **New migration** `003_sprint3.sql` (additive).
