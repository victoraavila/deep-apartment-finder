"""Scraper port — the boundary between the agent and any concrete portal.

Sprint 1 only has Fotocasa. Sprint 3 will add a second adapter following the
same Protocol. The orchestrator must not import concrete scrapers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.filters.hard import HardFilters


@dataclass(frozen=True, slots=True)
class ListingCard:
    """A search-result card. The detail page (`fetch_listing`) is what gets
    ingested; the card is just enough to render progress and decide whether
    to fetch the detail."""

    external_id: str
    url: str
    title: str | None = None
    price_eur: float | None = None
    raw: dict[str, Any] | None = None


@runtime_checkable
class ScraperPort(Protocol):
    """Portal boundary.

    `search_listings` is an async iterator so the subagent can stream cards
    one at a time, validate, and short-circuit when it has enough material
    to feed the LLM. `fetch_listing` is one-shot per URL.
    """

    def search_listings(self, filters: HardFilters) -> AsyncIterator[ListingCard]: ...

    async def fetch_listing(self, url: str) -> Apartment: ...

    async def close(self) -> None: ...
