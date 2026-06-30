"""`fetch_listing` tool — Idealista variant.

Identical to the Fotocasa one in behavior: returns the normalized
`Apartment` as JSON, ready to be passed to `ingest_apartment`.

Note: the Idealista scraper's `fetch_listing` attempts the Sprint 4
playwright detail-page enrichment after it has search-card data. When
that succeeds, `bathrooms`, `rooms`, `size_m2`, and the long-form
`description` are populated from the detail page. When the detail path
is disabled or blocked, it falls back to the search-card field set;
`lat`/`lng` are still `None`.
"""

from __future__ import annotations

import json

from langchain_core.tools import BaseTool, tool

from deep_apartment_finder.ports.scraper import ScraperPort
from deep_apartment_finder.tools.listing_payload import apartment_to_agent_payload


def make_fetch_listing_tool(scraper: ScraperPort) -> BaseTool:
    """Build the `fetch_listing` tool bound to a specific scraper."""

    @tool
    async def fetch_listing(url: str) -> str:
        """Fetch a single listing detail page and return a normalized
        apartment as JSON: `source`, `external_id`, `url`, `title`,
        `price_eur`, `rooms`, `bathrooms`, `size_m2`, `address`, `lat`,
        `lng`, `description`, `pet_policy`, and `furnished`."""
        apartment = await scraper.fetch_listing(url)
        return json.dumps(apartment_to_agent_payload(apartment))

    return fetch_listing
