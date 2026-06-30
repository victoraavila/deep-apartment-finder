# Deep Apartment Finder

An AI agent that helps you find the rental apartments in Spain that best
match what you're looking for. It ingests listings from rental portals,
scores them against your hard filters (price, size, location, …) and
soft criteria (neighborhood safety, pet policy, furnished, …), and
emails you the top picks on a daily cadence — so you never miss a
listing that would have been perfect for you.

This repository is on **Sprint 4 — Idealista detail-page upgrade +
parallel scraper execution**. See
[`docs/SPRINT4.md`](docs/SPRINT4.md) for the active scope and
[`docs/ROADMAP.md`](docs/ROADMAP.md) for the full roadmap.

## Table of contents

- [Quick start (Sprint 4)](#quick-start-sprint-4)
- [Gmail App Password setup](#gmail-app-password-setup)
- [Daily cron (09:00 Europe/Madrid)](#daily-cron-0900-europemadrid)
- [CLI commands](#cli-commands)
- [Architecture (Sprint 4)](#architecture-sprint-4)
- [What's new in Sprint 4](#whats-new-in-sprint-4)

## Quick start (Sprint 4)

Prereqs: Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), Docker,
and (for the Idealista detail-page fetch) a Chromium install via
playwright.

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
# IDEALISTA_DETAIL_FETCH defaults to true; the playwright-based detail
# enrichment is enabled when both the env var and the playwright install
# are present. Set to false to fall back to the search-card path only.

# 3. Bring up Postgres + pgvector
docker compose up -d

# 4. Apply migrations (idempotent; applies 001, 002, and 003)
uv run python -m deep_apartment_finder migrate

# 5. Install the playwright browser for the Idealista detail-page fetch
#    (only required if IDEALISTA_DETAIL_FETCH=true; one-time install).
uv run playwright install chromium

# 6. First run: the researcher subagent bootstraps the
#    dangerous-neighborhoods table. After it returns, re-run.
uv run python -m deep_apartment_finder run

# 7. Inspect the bootstrapped list, then re-run the full pipeline.
uv run python -m deep_apartment_finder list-dangerous
uv run python -m deep_apartment_finder run

# 8. Inspect what was ingested / ranked
uv run python -m deep_apartment_finder validate-quality
#   - per-source, per-field null rate (Idealista now has bathrooms
#     populated in ≥90% of rows)
#   - count of rows with invalid coordinates
#   - count of cross-portal dedup-key collisions

# 9. Inspect a past run (Sprint 3 Pillar A)
uv run python -m deep_apartment_finder show-run <run-uuid>
```

### Running without the detail-page fetch

If you'd rather not install Chromium (e.g. CI runners, or you want to
debug the ranker against search-card data only), disable the detail
fetch for that run:

```bash
uv run python -m deep_apartment_finder run --no-detail-fetch
# or
IDEALISTA_DETAIL_FETCH=disabled uv run python -m deep_apartment_finder run
```

The scraper falls back to the search-card path; `bathrooms` stays
`None` on every Idealista row (the Sprint 3 behaviour). All other
Sprint 4 changes (parallel subagent execution, the per-portal
counters in the run report) still apply.

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
`notifications` one-per-day invariant + the S3
`apartments_dedup_key_idx` make a second run on the same day a
no-op (the notifier logs "already notified today" and the
scraper subagent sees every listing as `duplicate` or
`updated`).

## CLI commands

| Command | What it does |
| --- | --- |
| `migrate` | Apply pending SQL migrations (001 + 002 + 003). |
| `run` | Drive the orchestrator end-to-end. Sprint 4 fires the two scraper subagents concurrently via the `run_scrapers` tool, prints phase headers + counters to **stderr** in real time, and persists a structured `RunReport` to `/orchestrator/reports/<run-uuid>.json` and the `run_reports` Postgres table. |
| `run --no-detail-fetch` | Sprint 4: disable the playwright-based Idealista detail page fetch for that run. The scraper falls back to the search-card path; `bathrooms` stays `None` on every Idealista row. |
| `run --trace` | Compatibility flag. LangSmith tracing is enabled automatically whenever `LANGSMITH_API_KEY` is configured; without that key tracing stays off. |
| `validate-quality` | Sprint 3: per-source, per-field null rate; count of invalid coordinates; count of cross-portal dedup-key collisions. |
| `list-dangerous` | Print the bootstrapped dangerous-neighborhoods table. |
| `show-run <run-uuid>` | Re-print a persisted run report (Sprint 3 Pillar A). |
| `backfill-dedup-keys` | Compute `dedup_key` for every existing apartment row that has `NULL` (Sprint 3 Pillar F). Idempotent; collisions are logged and left `NULL`. |

## Architecture (Sprint 4)

```
src/deep_apartment_finder/
  domain/      # value objects (Apartment, HardFilters, Geo (is_valid_coordinate,
               #   compute_dedup_key), RankableApartment, RunReport)
               # soft criteria (distance_to_dangerous, pet_policy, furnished, registry)
               # ranking + notifier (deterministic Python)
  ports/       # abstractions (ApartmentRepository, ScraperPort,
               #   DangerousNeighborhoodRepository, RankingRepository,
               #   DistanceProvider, Notifier, RunObserver)
  adapters/    # concrete I/O (Postgres, Fotocasa, Idealista + the new
               #   detail_client.py, Gmail SMTP, Haversine,
               #   CliRunObserver, RecordingRunObserver, langsmith tracing)
  tools/       # LangChain tools the agent can call
    orchestrator/
      run_scrapers.py   # Sprint 4: asyncio.gather of the two subagent graphs
    fotocasa/  idealista/  researcher/  ingest.py  listing_payload.py
  subagents/   # registered LLM subagents (fotocasa_scraper, idealista_scraper,
               #   researcher) + prompts (orchestrator.md now uses run_scrapers;
               #   idealista_scraper.md drops the bathrooms caveat and documents
               #   the new details_enriched / details_failed counters)
  agent/       # orchestrator built with create_deep_agent (compiles the
               #   scraper subagent graphs in parallel via run_scrapers)
  filesystem/  # CompositeBackend routes per subagent
  cli.py       # typer entrypoints (migrate / run [--no-detail-fetch] /
               #   validate-quality / list-dangerous / show-run /
               #   backfill-dedup-keys)
  main.py      # composition root
```

Decisions: [`docs/adr/`](docs/adr/).

## What's new in Sprint 4

- **Idealista detail-page upgrade (Pillar A).** A new
  `adapters/scrapers/idealista/detail_client.py` owns a single
  shared `playwright.async_api.BrowserContext`, created
  lazily on the first `fetch_listing` call and reused for
  every detail fetch in the run. The shared context is the
  whole point — it accumulates the DataDome trust-scoring
  signals (mouse movement, JS execution) a `curl_cffi` session
  can never produce. The scraper parses the canonical
  `<div class="details-property_features"><ul>...</ul></div>`
  block (via `parse_detail_page` / `apply_detail_enrichment`)
  to populate `bathrooms`, the long-form `description`, and
  re-assert `rooms` / `size_m2` when the block carries them.
  Acceptance criterion 1 is met:
  `validate-quality` shows `bathrooms` non-null rate ≥ 90% on
  `source='idealista'` rows (was 0% in Sprint 3), and the
  default `min_bathrooms=2` no longer rejects every Idealista
  row. The detail path gracefully falls back to the search-
  card walk when `IDEALISTA_DETAIL_FETCH=false`,
  `--no-detail-fetch` is passed, or playwright is not
  importable. Per-run counters `details_enriched` and
  `details_failed` track which path was taken and surface in
  the subagent's handoff and the run report.
- **Parallel scraper execution (Pillar B).** A new
  `run_scrapers(filters_brief: str) -> str` tool
  (`tools/orchestrator/run_scrapers.py`) fires the
  `fotocasa_scraper` and `idealista_scraper` subagent graphs
  concurrently via `asyncio.gather(...)` and returns a single
  combined handoff. A failure in one subagent does NOT cancel
  the other; the exception is captured in the handoff and the
  orchestrator decides how to react. The orchestrator's prompt
  is updated to call `run_scrapers` exactly once per run
  (instead of two sequential `task` calls). The `task` tool
  stays registered for single-portal debugging. The wall-time
  saving on the scraper phase is ~40-50% (the integration test
  `tests/integration/test_sprint4_pipeline.py::test_parallel_subagents_overlap_in_time`
  asserts the overlap). Inside each subagent the LLM may
  batch N `fetch_listing` calls; the underlying async session
  is concurrency-safe so the N calls complete in roughly the
  slowest call's time, not N× the slowest.
- **One new CLI flag.** `--no-detail-fetch` (env-equivalent
  `IDEALISTA_DETAIL_FETCH=disabled`) disables the playwright
  path for that run. Documented in `--help`.
- **One new env var.** `IDEALISTA_DETAIL_FETCH` (default
  `true`) — see `.env.example`.
- **New ADR**: ADR-013 — Parallel scraper execution.
  ADR-011 is updated in-place to mark its "Future work —
  detail-page upgrade" bullet as delivered.
- **No new migration** — the schema is unchanged; the
  detail-page upgrade populates an existing column.
- **No new dependency** at the Python level (playwright was
  already in `pyproject.toml` from Sprint 1). One new runtime
  install: `uv run playwright install chromium`.
