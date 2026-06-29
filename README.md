# Deep Apartment Finder

Agent-driven apartment finder for Zaragoza rentals. Ingests listings from
rental portals, ranks them by hard + soft criteria, and notifies the top 5
on a daily cadence.

This repository is currently on **Sprint 2 — Ranking + soft filters +
notification**. See [`docs/SPRINT2.md`](docs/SPRINT2.md) for the active
scope and [`docs/ROADMAP.md`](docs/ROADMAP.md) for the full roadmap.

## Quick start (Sprint 2)

Prereqs: Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), Docker.

```bash
# 1. Install deps
uv sync --extra dev

# 2. Configure env
cp .env.example .env
# edit .env — set OPENCODE_API_KEY (and optionally GROQ_API_KEY as fallback),
# GMAIL_SMTP_ADDRESS + GMAIL_SMTP_APP_PASSWORD (for the daily notification),
# and EXA_API_KEY (for the researcher's first-run web search).

# 3. Bring up Postgres + pgvector
docker compose up -d

# 4. Apply migrations (idempotent; applies 001 and 002)
uv run python -m deep_apartment_finder migrate

# 5. First run: the researcher subagent bootstraps the
#    dangerous-neighborhoods table. After it returns, re-run.
uv run python -m deep_apartment_finder run

# 6. Inspect the bootstrapped list, then re-run the full pipeline.
uv run python -m deep_apartment_finder list-dangerous
uv run python -m deep_apartment_finder run

# 7. Inspect what was ingested / ranked
uv run python -m deep_apartment_finder validate-quality
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
`notifications` partial unique index make a second run on the
same day a no-op (the notifier logs "already notified today").

## Architecture (Sprint 2)

```
src/deep_apartment_finder/
  domain/      # value objects (Apartment, HardFilters, Geo, RankableApartment, ...)
               # soft criteria (distance_to_dangerous, pet_policy, furnished, registry)
               # ranking + notifier (deterministic Python)
  ports/       # abstractions (ApartmentRepository, ScraperPort,
               #   DangerousNeighborhoodRepository, RankingRepository,
               #   DistanceProvider, Notifier)
  adapters/    # concrete I/O (Postgres, Fotocasa, Gmail SMTP, Haversine)
  tools/       # LangChain tools the agent can call
  subagents/   # registered LLM subagents (fotocasa_scraper, researcher) + prompts
  agent/       # orchestrator built with create_deep_agent
  filesystem/  # CompositeBackend routes per subagent
  cli.py       # typer entrypoints
  main.py      # composition root
```

Decisions: [`docs/adr/`](docs/adr/).

## What's new in Sprint 2

- **Researcher subagent** that bootstraps a constants table of
  Zaragoza's dangerous neighborhoods from public web data
  (ADR-006). Runs once on the first run; operator can override
  rows manually.
- **Three soft criteria** registered in
  `domain/soft_criteria/registry.py`: distance to dangerous
  neighborhood (haversine, ADR-007), pet policy, furnished. The
  scraper subagent now extracts `pet_policy` and `furnished` at
  ingest time.
- **Deterministic ranker** (no LLM at rank time) — pure
  Python, weighted-average scoring, with per-criterion trace
  rows in `apartment_scores`.
- **Gmail SMTP notifier** (ADR-008) — top-5 email with
  at-most-one-per-day dedup enforced at the DB level.
- **New migration** `002_sprint2.sql` (additive).
- **New CLI** `list-dangerous` for operator inspection.
- **Daily cron** at 09:00 Europe/Madrid (documented above).
- **New ADRs** 006 / 007 / 008.
