"""Bypass-LLM ingestion script.

Populates the `apartments` table directly from the Fotocasa API
without going through the orchestrator / subagent / LLM. Useful for:

- Smoke-testing the scraper end-to-end against the real DB
- Seeding rows when the LLM is rate-limited / down
- Running a "headless" ingestion that doesn't pay LLM cost

Use the orchestrator (`uv run python -m deep_apartment_finder run`)
for the full agent flow; this is the lower-fidelity path.

Run with:
    uv run python scripts/ingest_fotocasa_headless.py --cap 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from deep_apartment_finder.adapters.postgres.connection import get_pool
from deep_apartment_finder.adapters.postgres.migrations import apply_migrations
from deep_apartment_finder.adapters.postgres.repository import PostgresApartmentRepository
from deep_apartment_finder.adapters.scrapers.fotocasa.scraper import FotocasaScraper
from deep_apartment_finder.config import Settings
from deep_apartment_finder.domain.filters.hard import HardFilters

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ingest_fotocasa_headless")

_MIGRATIONS_DIR = (
    Path(__file__).parent.parent
    / "src"
    / "deep_apartment_finder"
    / "adapters"
    / "postgres"
    / "migrations"
)

# Hard filters mirror the Sprint 1 default brief from the orchestrator's
# `run` command. We apply the same filter set here so the data we
# ingest matches what the agent would have ingested.
DEFAULT_FILTERS = HardFilters(
    city="Zaragoza",
    min_rooms=2,
    min_bathrooms=2,
    min_size_m2=50.0,
    max_price_eur=1200.0,
)


async def main(cap: int) -> None:
    settings = Settings()
    log.info(
        "starting headless ingest: cap=%d filters=%s",
        cap,
        DEFAULT_FILTERS,
    )

    pool = await get_pool(settings)
    try:
        # Apply pending migrations (idempotent).
        applied = await apply_migrations(pool, _MIGRATIONS_DIR)
        if applied:
            log.info("applied %d migrations: %s", len(applied), [m.version for m in applied])

        scraper = FotocasaScraper(
            settings=settings,
            max_cards=cap,
            page_size=30,
        )
        repo = PostgresApartmentRepository(pool)

        seen: set[str] = set()
        inspected = 0
        inserted = 0
        duplicates = 0
        try:
            async for card in scraper.search_listings(DEFAULT_FILTERS):
                inspected += 1
                if card.external_id in seen:
                    continue
                seen.add(card.external_id)
                apt = await scraper.fetch_listing(card.url)
                if apt is None:
                    log.warning("could not build apartment for %s", card.url)
                    continue
                result = await repo.upsert(apt)
                if hasattr(result, "apartment_id"):
                    inserted += 1
                    log.info(
                        "inserted id=%d ext=%s %s€ %shab %sbaths %sm²",
                        result.apartment_id,
                        apt.external_id,
                        apt.price_eur,
                        apt.rooms,
                        apt.bathrooms,
                        apt.size_m2,
                    )
                else:
                    duplicates += 1
                    log.info("duplicate ext=%s", apt.external_id)
        finally:
            await scraper.close()

        summary = {
            "inspected": inspected,
            "inserted": inserted,
            "duplicates": duplicates,
        }
        log.info("done: %s", summary)
        print(json.dumps(summary))
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cap",
        type=int,
        default=int(os.environ.get("INGEST_MAX_LISTINGS", "20")),
        help="max listings to ingest",
    )
    args = parser.parse_args()
    asyncio.run(main(args.cap))
