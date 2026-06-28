# Deep Apartment Finder

Agent-driven apartment finder for Zaragoza rentals. Ingests listings from
rental portals, ranks them by hard + soft criteria, and notifies the top 5
on a daily cadence.

This repository is currently on **Sprint 1 — Fotocasa ingestion MVP**. See
[`docs/SPRINT1.md`](docs/SPRINT1.md) for the active scope and
[`docs/ROADMAP.md`](docs/ROADMAP.md) for the full roadmap.

## Quick start (Sprint 1)

Prereqs: Python ≥ 3.10, [uv](https://docs.astral.sh/uv/), Docker.

```bash
# 1. Install deps
uv sync --extra dev

# 2. Configure env
cp .env.example .env
# edit .env — set GROQ_API_KEY (and optionally OPENCODE_API_KEY as fallback)

# 3. Bring up Postgres + pgvector
docker compose up -d

# 4. Apply migrations (idempotent)
uv run python -m deep_apartment_finder migrate

# 5. Run the orchestrator
uv run python -m deep_apartment_finder run

# 6. Inspect what was ingested
uv run python -m deep_apartment_finder validate-quality
```

## Architecture (Sprint 1)

```
src/deep_apartment_finder/
  domain/      # value objects (Apartment, HardFilters, Source)
  ports/       # abstractions (ApartmentRepository, ScraperPort)
  adapters/    # concrete I/O (Postgres, Fotocasa scraper)
  tools/       # LangChain tools the agent can call
  subagents/   # registered subagents (fotocasa_scraper) + prompts
  agent/       # orchestrator built with create_deep_agent
  filesystem/  # CompositeBackend routes per subagent
  cli.py       # typer entrypoints
  main.py      # composition root
```

Decisions: [`docs/adr/`](docs/adr/).
