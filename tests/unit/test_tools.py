"""Tool factory tests.

Each test wires the tool factory to an `InMemoryApartmentRepository` or
`FakeScraper` and exercises the resulting tool's `arun` method (or
direct invocation in the LangChain 1.x style).
"""

from __future__ import annotations

import json

import pytest

from deep_apartment_finder.tools.fotocasa.fetch_listing import make_fetch_listing_tool
from deep_apartment_finder.tools.fotocasa.search_listings import make_search_listings_tool
from deep_apartment_finder.tools.ingest import make_ingest_apartment_tool
from tests._fakes import FakeScraper, InMemoryApartmentRepository, make_apartment

# --- ingest_apartment -----------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_apartment_returns_inserted_on_first_call():
    repo = InMemoryApartmentRepository()
    tool = make_ingest_apartment_tool(repo)
    apt = make_apartment(external_id="x1")
    payload = json.dumps(apt.to_ingest_dict())
    out = await tool.arun(payload)
    data = json.loads(out)
    assert data["status"] == "inserted"
    assert data["id"] == 1


@pytest.mark.asyncio
async def test_ingest_apartment_returns_duplicate_on_second_call():
    repo = InMemoryApartmentRepository()
    tool = make_ingest_apartment_tool(repo)
    apt = make_apartment(external_id="x1")
    payload = json.dumps(apt.to_ingest_dict())
    await tool.arun(payload)
    out = await tool.arun(payload)
    data = json.loads(out)
    assert data["status"] == "duplicate"
    assert data["external_id"] == "x1"


@pytest.mark.asyncio
async def test_ingest_apartment_filters_listing_that_fails_hard_filters():
    repo = InMemoryApartmentRepository()
    tool = make_ingest_apartment_tool(repo)
    apt = make_apartment(external_id="too-small", size_m2=40.0)
    payload = json.dumps(apt.to_ingest_dict())

    out = await tool.arun(payload)

    data = json.loads(out)
    assert data["status"] == "filtered"
    assert data["external_id"] == "too-small"
    assert await repo.count() == 0


@pytest.mark.asyncio
async def test_ingest_apartment_returns_error_on_invalid_source():
    repo = InMemoryApartmentRepository()
    tool = make_ingest_apartment_tool(repo)
    payload = json.dumps({"source": "bogus", "external_id": "x", "url": "u"})
    out = await tool.arun(payload)
    data = json.loads(out)
    assert data["status"] == "error"


@pytest.mark.asyncio
async def test_ingest_apartment_returns_error_on_missing_field():
    repo = InMemoryApartmentRepository()
    tool = make_ingest_apartment_tool(repo)
    payload = json.dumps({"source": "fotocasa", "external_id": "x"})  # no url
    out = await tool.arun(payload)
    data = json.loads(out)
    assert data["status"] == "error"


# --- search_listings ------------------------------------------------------


@pytest.mark.asyncio
async def test_search_listings_returns_cards_as_json():
    from deep_apartment_finder.ports.scraper import ListingCard

    cards = [
        ListingCard(external_id="a", url="https://x/a", title="A", price_eur=900.0),
        ListingCard(external_id="b", url="https://x/b", title="B", price_eur=1100.0),
    ]
    scraper = FakeScraper(cards=cards)
    tool = make_search_listings_tool(scraper)
    out = await tool.arun({})  # default filter values
    data = json.loads(out)
    assert data["count"] == 2
    assert [c["external_id"] for c in data["cards"]] == ["a", "b"]


@pytest.mark.asyncio
async def test_search_listings_passes_filters_to_scraper():
    scraper = FakeScraper(cards=[])
    tool = make_search_listings_tool(scraper)
    await tool.arun({"min_rooms": 3, "max_price_eur": 950.0})
    from deep_apartment_finder.domain.filters.hard import HardFilters

    assert len(scraper.search_calls) == 1
    f = scraper.search_calls[0]
    assert f == HardFilters(min_rooms=3, max_price_eur=950.0)


# --- fetch_listing --------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_listing_returns_normalized_apartment_json():
    apt = make_apartment(
        external_id="42",
        url="https://x/42",
        price_eur=950.0,
        rooms=3,
        bathrooms=2,
        size_m2=70.0,
    )
    scraper = FakeScraper(details={"https://x/42": apt})
    tool = make_fetch_listing_tool(scraper)
    out = await tool.arun({"url": "https://x/42"})
    data = json.loads(out)
    assert data["external_id"] == "42"
    assert data["title"] is not None
    assert data["price_eur"] == 950.0
    assert data["rooms"] == 3
    # scraped_at is not part of the output.
    assert "scraped_at" not in data


@pytest.mark.asyncio
async def test_fetch_listing_propagates_scraper_error():
    scraper = FakeScraper()
    tool = make_fetch_listing_tool(scraper)
    with pytest.raises(KeyError, match="missing"):
        await tool.arun({"url": "https://missing"})
