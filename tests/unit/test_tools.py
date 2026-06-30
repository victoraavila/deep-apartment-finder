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
from deep_apartment_finder.tools.listing_payload import (
    AGENT_LISTING_FIELDS,
    apartment_to_agent_payload,
)
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


@pytest.mark.asyncio
async def test_ingest_apartment_normalizes_llm_soft_enum_values():
    repo = InMemoryApartmentRepository()
    tool = make_ingest_apartment_tool(repo)
    apt = make_apartment(external_id="x1")
    payload = apt.to_ingest_dict()
    payload["pet_policy"] = "Not Allowed"
    payload["furnished"] = True

    out = await tool.arun(json.dumps(payload))

    data = json.loads(out)
    assert data["status"] == "inserted"
    rows = await repo.list_all()
    stored = rows[0][1]
    assert stored.pet_policy == "not_allowed"
    assert stored.furnished == "true"


@pytest.mark.asyncio
async def test_ingest_apartment_normalizes_unknown_soft_enum_values():
    repo = InMemoryApartmentRepository()
    tool = make_ingest_apartment_tool(repo)
    apt = make_apartment(external_id="x1")
    payload = apt.to_ingest_dict()
    payload["pet_policy"] = "maybe"
    payload["furnished"] = "MAYBE"

    out = await tool.arun(json.dumps(payload))

    data = json.loads(out)
    assert data["status"] == "inserted"
    rows = await repo.list_all()
    stored = rows[0][1]
    assert stored.pet_policy == "unknown"
    assert stored.furnished == "unknown"


# --- Sprint 3: backfill (`updated`) + dedup_key + coord normalization -----


@pytest.mark.asyncio
async def test_ingest_apartment_returns_updated_on_backfill():
    """Sprint 1 row with `pet_policy=None`; the second ingest backfills
    the field and the tool returns `updated`."""
    repo = InMemoryApartmentRepository()
    tool = make_ingest_apartment_tool(repo)
    apt = make_apartment(external_id="x1", pet_policy=None)
    payload = json.dumps(apt.to_ingest_dict())
    await tool.arun(payload)

    apt2 = make_apartment(external_id="x1", pet_policy="allowed")
    payload2 = json.dumps(apt2.to_ingest_dict())
    out = await tool.arun(payload2)
    data = json.loads(out)
    assert data["status"] == "updated"
    assert data["id"] == 1
    assert "pet_policy" in data["changed_fields"]


@pytest.mark.asyncio
async def test_ingest_apartment_drops_invalid_zero_zero_coordinates():
    """A listing with `lat=0, lng=0` must be stored with `None`."""

    repo = InMemoryApartmentRepository()
    tool = make_ingest_apartment_tool(repo)
    apt = make_apartment(external_id="x1", lat=0.0, lng=0.0)
    payload = json.dumps(apt.to_ingest_dict())
    await tool.arun(payload)
    rows = await repo.list_all()
    assert rows[0][1].lat is None
    assert rows[0][1].lng is None


@pytest.mark.asyncio
async def test_ingest_apartment_drops_out_of_bbox_coordinates():
    repo = InMemoryApartmentRepository()
    tool = make_ingest_apartment_tool(repo)
    apt = make_apartment(external_id="x1", lat=40.4168, lng=-3.7038)  # Madrid
    payload = json.dumps(apt.to_ingest_dict())
    await tool.arun(payload)
    rows = await repo.list_all()
    assert rows[0][1].lat is None
    assert rows[0][1].lng is None


@pytest.mark.asyncio
async def test_ingest_apartment_stamps_dedup_key_on_raw():
    """The tool computes the dedup_key and stores it in `raw`. Two
    different sources with the same physical apartment must end up
    with the same key (read via list_by_dedup_key)."""
    from deep_apartment_finder.domain.source import Source

    repo = InMemoryApartmentRepository()
    tool = make_ingest_apartment_tool(repo)
    apt_f = make_apartment(
        source=Source.FOTOCASA,
        external_id="f1",
        address="Calle X, Zaragoza",
        price_eur=950.0,
        rooms=2,
        size_m2=60.0,
    )
    apt_i = make_apartment(
        source=Source.IDEALISTA,
        external_id="i1",
        address="Calle X, Zaragoza",
        price_eur=950.0,
        rooms=2,
        size_m2=60.0,
    )
    await tool.arun(json.dumps(apt_f.to_ingest_dict()))
    await tool.arun(json.dumps(apt_i.to_ingest_dict()))

    rows = await repo.list_all()
    keys = {a.raw.get("dedup_key") for _, a in rows}
    assert None not in keys
    assert len(keys) == 1  # both rows share the same key


@pytest.mark.asyncio
async def test_ingest_apartment_dedup_key_is_stable_across_minor_drift():
    repo = InMemoryApartmentRepository()
    tool = make_ingest_apartment_tool(repo)
    apt1 = make_apartment(
        external_id="f1",
        address="Calle X, 50001 Zaragoza",
        price_eur=950.0,
        rooms=2,
        size_m2=65.0,
    )
    apt2 = make_apartment(
        external_id="f2",  # different external_id
        address="calle x, zaragoza",
        price_eur=962.0,  # same bucket
        rooms=2,
        size_m2=67.0,  # same bucket
    )
    await tool.arun(json.dumps(apt1.to_ingest_dict()))
    await tool.arun(json.dumps(apt2.to_ingest_dict()))
    rows = await repo.list_all()
    keys = {a.raw.get("dedup_key") for _, a in rows}
    assert len(keys) == 1


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


def test_apartment_to_agent_payload_includes_only_normalized_fields():
    apt = make_apartment(
        external_id="42",
        url="https://x/42",
        price_eur=950.0,
        rooms=3,
        bathrooms=2,
        size_m2=70.0,
        lat=41.65,
        lng=-0.88,
        description="Full description for soft-field extraction.",
        pet_policy="allowed",
        furnished="true",
        raw={"huge_raw_marker": "x" * 10_000},
    )

    data = apartment_to_agent_payload(apt)

    assert tuple(data) == AGENT_LISTING_FIELDS
    assert data["source"] == "fotocasa"
    assert data["external_id"] == "42"
    assert data["price_eur"] == 950.0
    assert data["size_m2"] == 70.0
    assert data["lat"] == 41.65
    assert data["lng"] == -0.88
    assert data["description"] == "Full description for soft-field extraction."
    assert "raw_json" not in data
    assert "raw" not in data
    assert "scraped_at" not in data


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
    # Persistence/debug fields are not part of the model-visible output.
    assert "raw_json" not in data
    assert "raw" not in data
    assert "scraped_at" not in data


@pytest.mark.asyncio
async def test_fetch_listing_does_not_expose_huge_raw_payload_to_model():
    apt = make_apartment(
        external_id="huge",
        url="https://x/huge",
        description="Keep the full description visible to the LLM.",
        raw={
            "huge_raw_marker": "this must never be model-visible",
            "blob": "x" * 100_000,
        },
    )
    scraper = FakeScraper(details={"https://x/huge": apt})
    tool = make_fetch_listing_tool(scraper)

    out = await tool.arun({"url": "https://x/huge"})
    data = json.loads(out)

    assert len(out) < 2_000
    assert "huge_raw_marker" not in out
    assert "raw_json" not in data
    assert "raw" not in data
    assert data["description"] == "Keep the full description visible to the LLM."


@pytest.mark.asyncio
async def test_fetch_listing_propagates_scraper_error():
    scraper = FakeScraper()
    tool = make_fetch_listing_tool(scraper)
    with pytest.raises(KeyError, match="missing"):
        await tool.arun({"url": "https://missing"})
