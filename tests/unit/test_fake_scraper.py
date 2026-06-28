"""FakeScraper tests: search iterator + fetch detail."""

from __future__ import annotations

import pytest

from deep_apartment_finder.domain.filters.hard import HardFilters
from deep_apartment_finder.ports.scraper import ListingCard
from tests._fakes import FakeScraper, make_apartment


@pytest.mark.asyncio
async def test_search_yields_cards_in_order():
    cards = [
        ListingCard(external_id="1", url="https://x/1"),
        ListingCard(external_id="2", url="https://x/2"),
    ]
    scraper = FakeScraper(cards=cards)
    out: list[ListingCard] = []
    async for card in scraper.search_listings(HardFilters()):
        out.append(card)
    assert [c.external_id for c in out] == ["1", "2"]


@pytest.mark.asyncio
async def test_search_records_filters_passed():
    scraper = FakeScraper(cards=[])
    f = HardFilters(min_rooms=3, max_price_eur=900.0)
    async for _ in scraper.search_listings(f):
        pass
    assert scraper.search_calls == [f]


@pytest.mark.asyncio
async def test_fetch_listing_returns_pre_built_apartment():
    apt = make_apartment(external_id="42", url="https://x/42")
    scraper = FakeScraper(details={"https://x/42": apt})
    out = await scraper.fetch_listing("https://x/42")
    assert out is apt
    assert scraper.fetch_calls == ["https://x/42"]


@pytest.mark.asyncio
async def test_fetch_listing_raises_for_unknown_url():
    scraper = FakeScraper()
    with pytest.raises(KeyError):
        await scraper.fetch_listing("https://missing")
