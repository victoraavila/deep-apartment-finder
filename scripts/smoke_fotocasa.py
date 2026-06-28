"""Live smoke test against the real Fotocasa /v1/search/ads endpoint.

Run with: ``uv run python scripts/smoke_fotocasa.py``
This is a *developer* smoke test, not a pytest test (it hits the
network). It runs the full `FotocasaScraper.search_listings` pipeline
end-to-end with the default Sprint 1 filters and prints the cards
that pass the hard filters.
"""

from __future__ import annotations

import asyncio

from deep_apartment_finder.adapters.scrapers.fotocasa.scraper import FotocasaScraper
from deep_apartment_finder.config import Settings
from deep_apartment_finder.domain.filters.hard import HardFilters


async def main() -> None:
    settings = Settings(scraper_delay_seconds=0.0)
    scraper = FotocasaScraper(settings=settings, max_cards=10)
    print(">>> Filters:", HardFilters())
    print(">>> Calling Fotocasa /v1/search/ads (cap=10)...")
    cards = []
    async for c in scraper.search_listings(HardFilters()):
        cards.append(c)
    await scraper.close()
    print(f">>> Returned {len(cards)} cards passing the hard filters")
    for c in cards:
        raw = c.raw or {}
        rooms = raw.get("rooms")
        baths = raw.get("baths")
        surface = raw.get("surface")
        txn = raw.get("transaction") or {}
        price = txn.get("price")
        print(
            f"   - id={c.external_id} "
            f"price={price}€ "
            f"rooms={rooms} "
            f"baths={baths} "
            f"surface={surface}m² "
            f"url={c.url}"
        )


if __name__ == "__main__":
    asyncio.run(main())
