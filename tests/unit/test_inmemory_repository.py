"""InMemoryApartmentRepository contract tests.

The in-memory fake must mirror the Postgres adapter's *dedup contract*:
upsert returns Inserted once, Updated when soft fields are backfilled,
and Duplicate on a no-op re-upsert. This is the surface acceptance
criterion (3) relies on, and a fake that returns Inserted twice
would mask bugs in PostgresApartmentRepository.
"""

from __future__ import annotations

import pytest

from deep_apartment_finder.domain.source import Source
from deep_apartment_finder.ports.apartment_repository import (
    Duplicate,
    Inserted,
    Updated,
)
from tests._fakes import InMemoryApartmentRepository, make_apartment


@pytest.mark.asyncio
async def test_first_upsert_returns_inserted_with_id_one():
    repo = InMemoryApartmentRepository()
    apt = make_apartment(external_id="a1")
    result = await repo.upsert(apt)
    assert isinstance(result, Inserted)
    assert result.apartment_id == 1


@pytest.mark.asyncio
async def test_second_upsert_of_same_external_id_returns_duplicate():
    repo = InMemoryApartmentRepository()
    await repo.upsert(make_apartment(external_id="a1"))
    result = await repo.upsert(make_apartment(external_id="a1", price_eur=999.0))
    assert isinstance(result, Duplicate)
    assert result.external_id == "a1"


@pytest.mark.asyncio
async def test_count_tracks_distinct_external_ids():
    repo = InMemoryApartmentRepository()
    assert await repo.count() == 0
    await repo.upsert(make_apartment(external_id="a1"))
    await repo.upsert(make_apartment(external_id="a2"))
    await repo.upsert(make_apartment(external_id="a1"))  # dup
    assert await repo.count() == 2


@pytest.mark.asyncio
async def test_duplicate_key_count_is_zero_because_duplicates_are_not_stored():
    repo = InMemoryApartmentRepository()
    await repo.upsert(make_apartment(external_id="a1"))
    await repo.upsert(make_apartment(external_id="a1"))
    assert await repo.duplicate_key_count() == 0


@pytest.mark.asyncio
async def test_recent_returns_newest_first_up_to_limit():
    repo = InMemoryApartmentRepository()
    a1 = make_apartment(external_id="a1")
    a2 = make_apartment(external_id="a2")
    a3 = make_apartment(external_id="a3")
    await repo.upsert(a1)
    await repo.upsert(a2)
    await repo.upsert(a3)
    recent = await repo.recent(limit=2)
    assert [a.external_id for a in recent] == ["a3", "a2"]


@pytest.mark.asyncio
async def test_dedup_keys_are_scoped_per_source():
    repo = InMemoryApartmentRepository()
    apt_a = make_apartment(source=Source.FOTOCASA, external_id="1")
    await repo.upsert(apt_a)
    # Same external_id, different source — must NOT be treated as a duplicate.
    apt_b = make_apartment(source=Source.IDEALISTA, external_id="1")
    result = await repo.upsert(apt_b)
    assert isinstance(result, Inserted)


# --- Sprint 3: backfill (`Updated`) contract ------------------------------


@pytest.mark.asyncio
async def test_upsert_returns_updated_when_pet_policy_backfills() -> None:
    """Sprint 1 row with NULL `pet_policy`; a re-scrape populates
    it; the repo must return `Updated` and the row must reflect the
    new value."""
    repo = InMemoryApartmentRepository()
    first = make_apartment(external_id="a1", pet_policy=None)
    inserted = await repo.upsert(first)
    assert isinstance(inserted, Inserted)

    second = make_apartment(external_id="a1", pet_policy="allowed")
    result = await repo.upsert(second)
    assert isinstance(result, Updated)
    assert result.apartment_id == inserted.apartment_id
    assert "pet_policy" in result.changed_fields

    rows = await repo.list_all()
    assert len(rows) == 1
    assert rows[0][1].pet_policy == "allowed"


@pytest.mark.asyncio
async def test_upsert_returns_updated_for_corrected_coordinates() -> None:
    """A listing whose `lat` was `(0, 0)` is corrected to a real
    Zaragoza coordinate; the repo must report the change."""
    from decimal import Decimal

    repo = InMemoryApartmentRepository()
    bad = make_apartment(external_id="a1", lat=0.0, lng=0.0)
    await repo.upsert(bad)

    good = make_apartment(
        external_id="a1", lat=Decimal("41.6561"), lng=Decimal("-0.8873")
    )
    result = await repo.upsert(good)
    assert isinstance(result, Updated)
    assert "lat" in result.changed_fields
    assert "lng" in result.changed_fields

    rows = await repo.list_all()
    assert rows[0][1].lat == Decimal("41.6561")


@pytest.mark.asyncio
async def test_upsert_returns_duplicate_when_nothing_changed() -> None:
    """Re-upserting the same payload is a no-op (Duplicate)."""
    repo = InMemoryApartmentRepository()
    apt = make_apartment(
        external_id="a1", pet_policy="allowed", furnished="true"
    )
    await repo.upsert(apt)
    result = await repo.upsert(apt)
    assert isinstance(result, Duplicate)


@pytest.mark.asyncio
async def test_upsert_does_not_overwrite_existing_value_with_none() -> None:
    """`COALESCE(EXCLUDED.x, apartments.x)`: a re-scrape that has
    `pet_policy=None` must NOT clobber an existing value."""
    repo = InMemoryApartmentRepository()
    await repo.upsert(make_apartment(external_id="a1", pet_policy="allowed"))
    # Second scrape forgot to set the field.
    result = await repo.upsert(make_apartment(external_id="a1", pet_policy=None))
    assert isinstance(result, Duplicate)
    rows = await repo.list_all()
    assert rows[0][1].pet_policy == "allowed"


# --- Sprint 3: cross-portal dedup + field coverage -------------------------


@pytest.mark.asyncio
async def test_cross_portal_dup_count_counts_dedup_key_collisions() -> None:
    repo = InMemoryApartmentRepository()
    # Two apartments on different sources that share a dedup_key.
    apt_f = make_apartment(
        source=Source.FOTOCASA, external_id="f1", raw={"dedup_key": "abc"}
    )
    apt_i = make_apartment(
        source=Source.IDEALISTA, external_id="i1", raw={"dedup_key": "abc"}
    )
    await repo.upsert(apt_f)
    await repo.upsert(apt_i)
    assert await repo.cross_portal_dup_count() == 1


@pytest.mark.asyncio
async def test_cross_portal_dup_count_ignores_unique_keys() -> None:
    repo = InMemoryApartmentRepository()
    await repo.upsert(make_apartment(external_id="a1", raw={"dedup_key": "k1"}))
    await repo.upsert(make_apartment(external_id="a2", raw={"dedup_key": "k2"}))
    assert await repo.cross_portal_dup_count() == 0


@pytest.mark.asyncio
async def test_field_coverage_reports_non_null_rate_per_source() -> None:
    repo = InMemoryApartmentRepository()
    await repo.upsert(
        make_apartment(external_id="a1", pet_policy="allowed", furnished="true")
    )
    await repo.upsert(make_apartment(external_id="a2", pet_policy=None))
    cov = await repo.field_coverage()
    assert "fotocasa" in cov
    assert cov["fotocasa"]["pet_policy"]["non_null_rate"] == 0.5
    assert cov["fotocasa"]["furnished"]["non_null_rate"] == 0.5


@pytest.mark.asyncio
async def test_field_coverage_counts_invalid_zero_zero_coordinates() -> None:

    repo = InMemoryApartmentRepository()
    await repo.upsert(make_apartment(external_id="a1", lat=0.0, lng=0.0))
    cov = await repo.field_coverage()
    assert cov["fotocasa"]["invalid_coordinates"]["count"] == 1


@pytest.mark.asyncio
async def test_list_by_dedup_key_returns_matching_apartments() -> None:
    repo = InMemoryApartmentRepository()
    await repo.upsert(make_apartment(external_id="a1", raw={"dedup_key": "k1"}))
    await repo.upsert(make_apartment(external_id="a2", raw={"dedup_key": "k1"}))
    await repo.upsert(make_apartment(external_id="a3", raw={"dedup_key": "k2"}))
    matches = await repo.list_by_dedup_key("k1")
    assert {a.external_id for _, a in matches} == {"a1", "a2"}
    assert await repo.list_by_dedup_key("nonexistent") == []
