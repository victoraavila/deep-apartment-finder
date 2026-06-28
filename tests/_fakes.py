"""In-memory implementations of the ports, used by unit + integration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.filters.hard import HardFilters
from deep_apartment_finder.domain.source import Source
from deep_apartment_finder.ports.apartment_repository import (
    ApartmentRepository,
    Duplicate,
    Inserted,
)
from deep_apartment_finder.ports.scraper import ListingCard, ScraperPort


class InMemoryApartmentRepository(ApartmentRepository):
    """ApartmentRepository that lives in a dict, with the same dedup contract
    as the Postgres adapter (Inserted vs Duplicate, never raises on dup)."""

    def __init__(self) -> None:
        self._by_id: dict[int, Apartment] = {}
        self._by_source_ext: dict[tuple[str, str], int] = {}
        self._next_id = 1

    async def upsert(self, apartment: Apartment) -> Inserted | Duplicate:
        key = (apartment.source.value, apartment.external_id)
        if key in self._by_source_ext:
            return Duplicate(external_id=apartment.external_id)
        new_id = self._next_id
        self._next_id += 1
        self._by_id[new_id] = apartment
        self._by_source_ext[key] = new_id
        return Inserted(apartment_id=new_id)

    async def count(self) -> int:
        return len(self._by_id)

    async def duplicate_key_count(self) -> int:
        return 0

    async def recent(self, limit: int = 10) -> list[Apartment]:
        items = list(self._by_id.values())
        items.sort(key=lambda a: a.scraped_at, reverse=True)
        return items[:limit]

    async def close(self) -> None:
        return None


class FakeScraper(ScraperPort):
    """A scraper that yields a fixed list of cards, then maps each card to a
    detail `Apartment` from a fixture dict. No I/O."""

    def __init__(
        self,
        cards: list[ListingCard] | None = None,
        details: dict[str, Apartment] | None = None,
    ) -> None:
        self._cards = cards or []
        self._details = details or {}
        self.search_calls: list[HardFilters] = []
        self.fetch_calls: list[str] = []

    async def search_listings(self, filters: HardFilters) -> AsyncIterator[ListingCard]:
        self.search_calls.append(filters)
        for card in self._cards:
            yield card

    async def fetch_listing(self, url: str) -> Apartment:
        self.fetch_calls.append(url)
        if url not in self._details:
            raise KeyError(f"FakeScraper has no detail for {url}")
        return self._details[url]

    async def close(self) -> None:
        return None


def make_apartment(
    *,
    source: Source = Source.FOTOCASA,
    external_id: str = "1",
    url: str = "https://example.com/1",
    price_eur: float | None = 1000.0,
    rooms: int | None = 2,
    bathrooms: int | None = 2,
    size_m2: float | None = 60.0,
    address: str | None = "Calle Test 1, Zaragoza",
    description: str | None = "Test listing",
    **kwargs: Any,
) -> Apartment:
    from decimal import Decimal

    return Apartment(
        source=source,
        external_id=external_id,
        url=url,
        title=kwargs.get("title", f"Apt {external_id}"),
        price_eur=Decimal(str(price_eur)) if price_eur is not None else None,
        rooms=rooms,
        bathrooms=bathrooms,
        size_m2=Decimal(str(size_m2)) if size_m2 is not None else None,
        address=address,
        lat=kwargs.get("lat"),
        lng=kwargs.get("lng"),
        description=description,
        pet_policy=kwargs.get("pet_policy"),
        raw=kwargs.get("raw", {}),
    )
