"""InMemoryApartmentRepository contract tests.

The in-memory fake must mirror the Postgres adapter's *dedup contract*:
upsert returns Inserted once, Duplicate on every subsequent call with the
same (source, external_id). This is the surface acceptance criterion (3)
relies on, and a fake that returns Inserted twice would mask bugs in
PostgresApartmentRepository.
"""

from __future__ import annotations

import pytest

from deep_apartment_finder.domain.source import Source
from deep_apartment_finder.ports.apartment_repository import Duplicate, Inserted
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
