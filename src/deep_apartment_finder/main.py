"""Composition root.

Wires concrete adapters into ports and exposes a small set of builders
the CLI uses. The CLI never imports `adapters/` directly; it goes
through `main.py`.

Two build functions:
- `build_app()` returns a `RunContext` carrying every dependency the
  CLI needs (settings, pool, scraper, repo, llm, plus the Sprint 2
  dangerous-neighborhood, ranking, and notifier deps, plus the
  Sprint 3 observability backend).
- `build_orchestrator_for_cli(...)` returns the composite orchestrator
  (LLM graph + deterministic steps) the CLI's `run` subcommand uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

from deep_apartment_finder.adapters.distance.haversine import HaversineDistanceProvider
from deep_apartment_finder.adapters.notifiers.gmail_smtp import GmailSmtpNotifier
from deep_apartment_finder.adapters.postgres.connection import get_pool
from deep_apartment_finder.adapters.postgres.dangerous_neighborhood_repository import (
    PostgresDangerousNeighborhoodRepository,
)
from deep_apartment_finder.adapters.postgres.ranking_repository import (
    PostgresRankingRepository,
)
from deep_apartment_finder.adapters.postgres.repository import (
    PostgresApartmentRepository,
)
from deep_apartment_finder.adapters.scrapers.fotocasa.scraper import (
    FotocasaScraper,
)
from deep_apartment_finder.adapters.scrapers.idealista.scraper import (
    IdealistaScraper,
)
from deep_apartment_finder.agent.orchestrator import Orchestrator, build_orchestrator
from deep_apartment_finder.config import Settings, get_settings
from deep_apartment_finder.filesystem.routes import build_backend
from deep_apartment_finder.llm import build_chat_model_with_fallback
from deep_apartment_finder.ports.apartment_repository import ApartmentRepository
from deep_apartment_finder.ports.dangerous_neighborhood_repository import (
    DangerousNeighborhoodRepository,
)
from deep_apartment_finder.ports.ranking_repository import RankingRepository
from deep_apartment_finder.ports.run_observer import RunObserver
from deep_apartment_finder.ports.scraper import ScraperPort
from deep_apartment_finder.tools.researcher.web_search import ExaSearchBackend

_MIGRATIONS_DIR = (
    Path(__file__).parent / "adapters" / "postgres" / "migrations"
)


@dataclass(slots=True)
class RunContext:
    settings: Settings
    pool: asyncpg.Pool
    scraper: ScraperPort  # Fotocasa (kept for backward compatibility)
    idealista_scraper: ScraperPort | None  # Sprint 3 second scraper
    repo: ApartmentRepository
    dangerous_repo: DangerousNeighborhoodRepository
    ranking_repo: RankingRepository
    # Sprint 3 (Pillar A): the `CompositeBackend` the CLI uses to
    # persist the run report JSON. Lives here so the CLI can hand
    # the same backend the orchestrator uses for `/orchestrator/`
    # writes to the `RecordingRunObserver.finalize(...)` call.
    observability_backend: Any = None


async def build_app(settings: Settings | None = None) -> RunContext:
    """Build a fully-wired application context.

    Opens a Postgres pool, constructs the scrapers and the
    repositories. The caller owns the lifecycle and must call
    `RunContext.pool.close()` when done.
    """
    settings = settings or get_settings()
    pool = await get_pool(settings)
    scraper = FotocasaScraper(
        settings=settings,
        max_cards=settings.ingest_max_listings,
    )
    idealista_scraper: ScraperPort | None = None
    if getattr(settings, "idealista_enabled", True):
        idealista_scraper = IdealistaScraper(
            settings=settings,
            max_cards=settings.ingest_max_listings,
        )
    repo = PostgresApartmentRepository(pool)
    dangerous_repo = PostgresDangerousNeighborhoodRepository(pool)
    ranking_repo = PostgresRankingRepository(pool)
    return RunContext(
        settings=settings,
        pool=pool,
        scraper=scraper,
        idealista_scraper=idealista_scraper,
        repo=repo,
        dangerous_repo=dangerous_repo,
        ranking_repo=ranking_repo,
        observability_backend=build_backend(),
    )


def build_notifier_for_cli(ctx: RunContext) -> GmailSmtpNotifier | None:
    """Build a Gmail SMTP notifier when configured, else return `None`."""
    if not ctx.settings.has_gmail_smtp:
        return None
    return GmailSmtpNotifier(settings=ctx.settings)


def build_orchestrator_for_cli(
    ctx: RunContext,
    notifier: Any | None = None,
    observer: RunObserver | None = None,
) -> Orchestrator:
    """Build the composite orchestrator for the CLI's `run` subcommand.

    `observer` is the optional `RunObserver` the deterministic
    steps emit events through (Pillar A). The CLI passes a
    fan-out of `CliRunObserver` + `RecordingRunObserver` so the
    operator sees progress in stderr *and* the run report is
    persisted.
    """
    llm = build_chat_model_with_fallback(ctx.settings)
    from_addr = ctx.settings.gmail_smtp_address
    to_addr = ctx.settings.notify_to_address or from_addr
    researcher_search_backend = (
        ExaSearchBackend(ctx.settings.exa_api_key)
        if ctx.settings.exa_api_key
        else None
    )
    result: Orchestrator = build_orchestrator(
        llm=llm,
        fotocasa_scraper=ctx.scraper,
        idealista_scraper=ctx.idealista_scraper,
        repo=ctx.repo,
        dangerous_repo=ctx.dangerous_repo,
        ranking_repo=ctx.ranking_repo,
        notifier=notifier,
        from_address=from_addr,
        to_address=to_addr,
        weight_distance=ctx.settings.rank_weight_distance,
        weight_pet_policy=ctx.settings.rank_weight_pet_policy,
        weight_furnished=ctx.settings.rank_weight_furnished,
        max_distance_m=ctx.settings.rank_max_distance_m,
        top_n=ctx.settings.rank_top_n,
        researcher_search_backend=researcher_search_backend,
        observer=observer,
    )
    return result


__all__ = [
    "RunContext",
    "_MIGRATIONS_DIR",
    "HaversineDistanceProvider",
    "build_app",
    "build_notifier_for_cli",
    "build_orchestrator_for_cli",
]
