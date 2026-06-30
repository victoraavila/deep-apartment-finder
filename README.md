# Deep Apartment Finder

An AI agent that helps you find the rental apartments in Spain that best
match what you're looking for. It ingests listings from rental portals,
scores them against your hard filters (price, size, location, …) and
soft criteria (neighborhood safety, pet policy, furnished, …), and
emails you the top picks on a daily cadence — so you never miss a
listing that would have been perfect for you.

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Running the pipeline](#running-the-pipeline)
- [Scheduling](#scheduling)
- [Architecture](#architecture)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Project documents](#project-documents)
- [Responsible use](#responsible-use)

## Features

- Multi-source listing ingestion from Fotocasa and Idealista.
- Idealista detail-page enrichment for bathrooms, room count, size,
  and long-form descriptions using Playwright-backed rendering.
- Parallel scraper execution so independent portal agents can work at
  the same time during a run.
- Hard filters for price, size, rooms, bathrooms, and location-driven
  constraints.
- Soft ranking criteria for neighborhood distance, pet policy, and
  furnished status.
- Cross-portal duplicate detection using a deterministic deduplication
  key.
- Gmail SMTP notifications for the daily top-ranked apartments.
- Structured CLI progress, persisted run reports, and optional
  LangSmith tracing.

## How it works

Deep Apartment Finder runs as an agent-driven pipeline:

1. The researcher bootstraps local safety context by populating the
   dangerous-neighborhoods table on the first run.
2. The orchestrator launches portal-specific scraper subagents for
   Fotocasa and Idealista.
3. Scraper tools collect listing cards, fetch listing details, apply
   hard filters, and ingest accepted apartments into Postgres.
4. The deterministic ranker scores eligible listings against the
   configured soft criteria.
5. The notifier emails the top-ranked apartments once per day and
   records the send so repeat runs are idempotent.

The project is designed as a local, inspectable system: every run can
be observed in the terminal, replayed from the persisted run report,
and traced in LangSmith when tracing credentials are configured.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) for dependency management
- Docker and Docker Compose for local Postgres + pgvector
- Playwright Chromium for Idealista detail-page enrichment
- An OpenCode API key for the primary reasoning model
- Optional provider credentials for Groq fallback, Exa first-run
  research, Gmail notifications, and LangSmith tracing

## Quick start

```bash
# Install Python dependencies.
uv sync --extra dev

# Create local configuration and fill in credentials.
cp .env.example .env

# Start Postgres + pgvector.
docker compose up -d

# Apply database migrations.
uv run python -m deep_apartment_finder migrate

# Install the browser used by Playwright-backed scrapers.
uv run deep-apartment-finder install-browsers

# First run: bootstrap the dangerous-neighborhoods table.
uv run python -m deep_apartment_finder run

# Inspect the bootstrapped safety data, then run the full pipeline.
uv run python -m deep_apartment_finder list-dangerous
uv run python -m deep_apartment_finder run

# Inspect ingested data and quality checks.
uv run python -m deep_apartment_finder validate-quality
```

The first run stops after the researcher has populated the safety
table. This gives you a chance to inspect the generated neighborhood
list before apartments are ranked and emailed.

## Configuration

Copy `.env.example` to `.env` and keep `.env` local. It contains
credentials, database settings, scraper behavior, ranking weights, and
notification settings.

### Required for a normal run

| Variable | Purpose |
| --- | --- |
| `POSTGRES_DSN` | Connection string for the local Postgres database. |
| `OPENCODE_API_KEY` | Primary reasoning model credential. |

### Primary model defaults

| Variable | Purpose |
| --- | --- |
| `OPENCODE_BASE_URL` | Optional override for the OpenCode-compatible endpoint. |
| `OPENCODE_MODEL` | Primary reasoning model name; defaults are provided in `.env.example`. |

### Optional integrations

| Variable | Purpose |
| --- | --- |
| `GROQ_API_KEY`, `GROQ_MODEL` | Fallback reasoning model provider. |
| `EXA_API_KEY` | Web search for first-run researcher bootstrapping. |
| `LANGSMITH_API_KEY`, `LANGSMITH_TRACING`, `LANGSMITH_PROJECT` | Full-run tracing and debugging. |
| `GMAIL_SMTP_ADDRESS`, `GMAIL_SMTP_APP_PASSWORD`, `NOTIFY_TO_ADDRESS` | Daily email notification delivery. |
| `GMAIL_SMTP_HOST`, `GMAIL_SMTP_PORT` | SMTP host and port overrides. |

### Runtime behavior

| Variable | Purpose |
| --- | --- |
| `INGEST_MAX_LISTINGS` | Maximum listings to ingest per CLI invocation. |
| `SCRAPER_DELAY_SECONDS` | Polite delay between Fotocasa requests. |
| `IDEALISTA_SCRAPER_DELAY_SECONDS` | Polite delay between Idealista requests. |
| `IDEALISTA_IMPERSONATE` | `curl_cffi` browser impersonation profile. |
| `IDEALISTA_ENABLED` | Set to `false` to run Fotocasa only. |
| `IDEALISTA_DETAIL_FETCH` | Set to `false` to skip Playwright detail enrichment. |
| `RANK_WEIGHT_DISTANCE`, `RANK_WEIGHT_PET_POLICY`, `RANK_WEIGHT_FURNISHED` | Soft ranking weights. |
| `RANK_MAX_DISTANCE_M` | Distance where the neighborhood-distance score saturates. |
| `RANK_TOP_N` | Number of apartments to include in the daily email. |

### Gmail App Password setup

The notifier uses Gmail SMTP with an App Password. A regular Gmail
password will not work over SMTP.

1. Enable 2-Step Verification on your Google account.
2. Visit [https://myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Create an App Password with any name, such as
   `deep-apartment-finder`.
4. Put the 16-character password in `.env` as
   `GMAIL_SMTP_APP_PASSWORD=...` without spaces.
5. Set `GMAIL_SMTP_ADDRESS` to the Gmail account and
   `NOTIFY_TO_ADDRESS` to the recipient address.

If SMTP is misconfigured, the CLI logs the notification error without
breaking ranking or ingestion.

## Running the pipeline

| Command | What it does |
| --- | --- |
| `uv run python -m deep_apartment_finder migrate` | Apply pending SQL migrations. |
| `uv run deep-apartment-finder install-browsers` | Install Playwright Chromium for browser-backed scraper paths. |
| `uv run python -m deep_apartment_finder run` | Run the full orchestrator pipeline. |
| `uv run python -m deep_apartment_finder run --no-detail-fetch` | Disable Idealista browser detail enrichment for this run. |
| `uv run python -m deep_apartment_finder run --skip-llm` | Skip scraper subagents and run deterministic ranking + notification over existing data. |
| `uv run python -m deep_apartment_finder run --trace` | Compatibility flag; tracing is enabled when LangSmith credentials are present. |
| `uv run python -m deep_apartment_finder validate-quality` | Print per-source field coverage, invalid coordinate counts, and deduplication checks. |
| `uv run python -m deep_apartment_finder list-dangerous` | Print the dangerous-neighborhoods table. |
| `uv run python -m deep_apartment_finder show-run <run-uuid>` | Reprint a persisted run report. |
| `uv run python -m deep_apartment_finder backfill-dedup-keys` | Compute missing cross-portal deduplication keys for existing rows. |

Run reports are persisted under the orchestrator filesystem subtree
and in the `run_reports` Postgres table. Use `show-run` with the run
UUID printed by the CLI to inspect a previous execution.

To run without Playwright detail-page enrichment:

```bash
uv run python -m deep_apartment_finder run --no-detail-fetch
# or
IDEALISTA_DETAIL_FETCH=false uv run python -m deep_apartment_finder run
```

The scraper falls back to the search-card data path. Parallel scraper
execution and run reporting still work.

## Scheduling

Add a single crontab entry on the host that runs the CLI:

```cron
TZ=Europe/Madrid
0 9 * * * cd /path/to/deep_apartment_finder && .venv/bin/python -m deep_apartment_finder run >> logs/cron.log 2>&1
```

The pipeline is designed to tolerate repeat runs. Listing ingestion is
deduplicated, cross-portal siblings are tracked by `dedup_key`, and
notifications are sent at most once per day.

## Architecture

```
src/deep_apartment_finder/
  domain/       # Apartment model, filters, ranking, notification logic, reports
  ports/        # Repository, scraper, notifier, distance, and observer interfaces
  adapters/     # Postgres, scrapers, Gmail SMTP, distance, and observability I/O
  tools/        # LangChain tools exposed to the orchestrator and subagents
  subagents/    # Registered specialist agents and their prompts
  agent/        # Deep Agents orchestrator assembly
  filesystem/   # Per-agent filesystem routing
  cli.py        # Typer command-line interface
  main.py       # Composition root
```

The code follows a ports-and-adapters structure. Domain behavior
depends on interfaces from `ports/`, while concrete I/O lives in
`adapters/` and is wired at the composition root. This keeps scraper
changes, persistence changes, notification providers, and ranking
criteria isolated from each other.

Specialist agents are registered up front and receive only the tools
they need. Filesystem writes are routed per subagent so research notes,
scraper artifacts, and orchestrator reports stay separated.

## Development

Install development dependencies:

```bash
uv sync --extra dev
```

Run the test suite:

```bash
uv run pytest
```

Run linting and type checks:

```bash
uv run ruff check .
uv run mypy src
```

Useful inspection commands:

```bash
uv run python -m deep_apartment_finder validate-quality
uv run python -m deep_apartment_finder show-run <run-uuid>
```

## Troubleshooting

- **`OPENCODE_API_KEY is not set`**: copy `.env.example` to `.env`
  and configure the primary model credentials.
- **Postgres connection errors**: start the local database with
  `docker compose up -d` and confirm `POSTGRES_DSN` matches the
  compose credentials.
- **Missing Playwright browser**: run
  `uv run deep-apartment-finder install-browsers`.
- **First run does not send listings**: expected behavior. Inspect the
  researcher output with `list-dangerous`, then run the pipeline again.
- **No email is sent**: configure `GMAIL_SMTP_ADDRESS`,
  `GMAIL_SMTP_APP_PASSWORD`, and `NOTIFY_TO_ADDRESS`. The project
  records daily sends, so repeat runs on the same day may intentionally
  skip notification.
- **Idealista detail enrichment is unavailable**: use
  `--no-detail-fetch` or `IDEALISTA_DETAIL_FETCH=false` to run with
  search-card data only.

## Project documents

- [`docs/adr/`](docs/adr/) records architecture decisions.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) captures the original product
  direction and design principles.
- [`docs/SPRINT1.md`](docs/SPRINT1.md),
  [`docs/SPRINT2.md`](docs/SPRINT2.md),
  [`docs/SPRINT3.md`](docs/SPRINT3.md), and
  [`docs/SPRINT4.md`](docs/SPRINT4.md) preserve implementation history.

## Responsible use

This project is intended for personal apartment search automation.
Use polite delays, respect portal terms, keep credentials out of source
control, and review generated safety data before relying on it for
ranking decisions.
