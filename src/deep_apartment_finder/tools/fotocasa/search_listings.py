"""`search_listings` tool.

Calls the injected `ScraperPort.search_listings(...)` and returns the
cards as a JSON array. The subagent iterates the result, decides which
cards are worth fetching in detail, and uses `ingest_apartment` on the
full listings.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from langchain_core.tools import BaseTool, tool

from deep_apartment_finder.domain.filters.hard import HardFilters
from deep_apartment_finder.ports.scraper import ListingCard, ScraperPort


def make_search_listings_tool(scraper: ScraperPort) -> BaseTool:
    """Build the `search_listings` tool bound to a specific scraper."""

    @tool
    async def search_listings(
        min_rooms: int | None = 2,
        min_bathrooms: int | None = 2,
        min_size_m2: float | None = 50.0,
        max_price_eur: float | None = 1200.0,
        city: str = "Zaragoza",
    ) -> str:
        """Search the configured portal for rental listings matching the
        hard filters. Returns a JSON array of cards: each card has
        `external_id`, `url`, `title`, `price_eur`, and `raw`."""
        filters = HardFilters(
            city=city,
            min_rooms=min_rooms,
            min_bathrooms=min_bathrooms,
            min_size_m2=min_size_m2,
            max_price_eur=max_price_eur,
        )

        async def _collect(it: AsyncIterator[ListingCard]) -> list[dict[str, object]]:
            out: list[dict[str, object]] = []
            async for card in it:
                out.append(
                    {
                        "external_id": card.external_id,
                        "url": card.url,
                        "title": card.title,
                        "price_eur": card.price_eur,
                    }
                )
            return out

        cards = await _collect(scraper.search_listings(filters))
        return json.dumps({"count": len(cards), "cards": cards})

    return search_listings
