"""Composition root.

Wires concrete adapters into ports and exposes a small set of builders
the CLI uses. The CLI never imports `adapters/` directly; it goes
through `main.py`.

Two build functions:
- `build_app()` returns a `RunContext` carrying every dependency the
  CLI needs (settings, pool, scraper, repo, llm). `build_app` is
  async because opening the pool is async.
- `build_orchestrator_for_cli(...)` returns the compiled graph the
  CLI's `run` subcommand invokes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

from deep_apartment_finder.adapters.postgres.connection import get_pool
from deep_apartment_finder.adapters.postgres.repository import PostgresApartmentRepository
from deep_apartment_finder.adapters.scrapers.fotocasa.scraper import (
    FotocasaScraper,
    _build_playwright_renderer,
)
from deep_apartment_finder.agent.orchestrator import build_orchestrator
from deep_apartment_finder.config import Settings, get_settings
from deep_apartment_finder.llm import build_chat_model_with_fallback
from deep_apartment_finder.ports.apartment_repository import ApartmentRepository
from deep_apartment_finder.ports.scraper import ScraperPort

_MIGRATIONS_DIR = (
    Path(__file__).parent / "adapters" / "postgres" / "migrations"
)


@dataclass(slots=True)
class RunContext:
    settings: Settings
    pool: asyncpg.Pool
    scraper: ScraperPort
    repo: ApartmentRepository


async def build_app(settings: Settings | None = None) -> RunContext:
    """Build a fully-wired application context.

    Opens a Postgres pool, constructs the scraper and the repository.
    The caller owns the lifecycle and must call `RunContext.pool.close()`
    when done (we expose a helper for that).
    """
    settings = settings or get_settings()
    pool = await get_pool(settings)
    scraper = FotocasaScraper(
        settings=settings,
        renderer=_build_playwright_renderer(),
        max_cards=settings.ingest_max_listings,
    )
    repo = PostgresApartmentRepository(pool)
    return RunContext(settings=settings, pool=pool, scraper=scraper, repo=repo)


def build_orchestrator_for_cli(ctx: RunContext) -> Any:
    """Build the orchestrator graph for the CLI's `run` subcommand."""
    llm = build_chat_model_with_fallback(ctx.settings)
    return build_orchestrator(llm=llm, scraper=ctx.scraper, repo=ctx.repo)


__all__ = [
    "RunContext",
    "_MIGRATIONS_DIR",
    "build_app",
    "build_orchestrator_for_cli",
]
