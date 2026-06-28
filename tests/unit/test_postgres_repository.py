"""PostgresApartmentRepository tests.

We can't assume Postgres is available in unit tests, so we test the
*behaviour* of the repository with a recording asyncpg fake. The fake
implements the minimal subset of the asyncpg connection API that
PostgresApartmentRepository uses (`acquire`, `fetchrow`, `fetch`,
`execute`) and lets the test seed the result of `fetchrow` to verify
dedup is detected (no row returned -> Duplicate).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from deep_apartment_finder.adapters.postgres.repository import (
    PostgresApartmentRepository,
    _row_to_apartment,
)
from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.source import Source
from deep_apartment_finder.ports.apartment_repository import Duplicate, Inserted


class _FakeTransaction:
    async def __aenter__(self) -> _FakeTransaction:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, fake_pool: _FakePool) -> None:
        self._pool = fake_pool

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        # Record the call so the test can inspect the parameter shape.
        self._pool.calls.append((sql, args))
        # Return whatever the test queued for the next fetchrow call.
        if self._pool.queued_fetchrow:
            return self._pool.queued_fetchrow.pop(0)
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self._pool.calls.append((sql, args))
        if self._pool.queued_fetch:
            return self._pool.queued_fetch.pop(0)
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        self._pool.calls.append((sql, args))
        return ""


class _FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.queued_fetchrow: list[dict[str, Any] | None] = []
        self.queued_fetch: list[list[dict[str, Any]]] = []

    def acquire(self):
        conn = _FakeConn(self)

        class _Ctx:
            async def __aenter__(self_inner) -> _FakeConn:  # noqa: N805
                return conn

            async def __aexit__(self_inner, *exc: Any) -> None:
                return None

        return _Ctx()


def _make_apartment() -> Apartment:
    return Apartment(
        source=Source.FOTOCASA,
        external_id="abc-1",
        url="https://fotocasa.es/abc-1",
        title="Nice flat",
        price_eur=Decimal("1000.00"),
        rooms=2,
        bathrooms=2,
        size_m2=Decimal("62.5"),
        address="Calle Test 1, Zaragoza",
        description="3-bedroom, 2-bath",
        raw={"soup": "raw payload"},
    )


@pytest.mark.asyncio
async def test_upsert_sends_parameters_in_documented_order():
    pool = _FakePool()
    pool.queued_fetchrow.append({"id": 42})
    repo = PostgresApartmentRepository(pool)

    apt = _make_apartment()
    result = await repo.upsert(apt)
    assert isinstance(result, Inserted)
    assert result.apartment_id == 42

    # The first queued fetchrow call should be the upsert.
    assert len(pool.calls) == 1
    sql, args = pool.calls[0]
    assert "ON CONFLICT (source, external_id) DO NOTHING" in sql
    assert "RETURNING id" in sql
    # Source, external_id, url, title, price_eur, rooms, bathrooms,
    # size_m2, address, lat, lng, description, pet_policy, raw_json, scraped_at
    assert args[0] == "fotocasa"
    assert args[1] == "abc-1"
    assert args[2] == "https://fotocasa.es/abc-1"
    assert args[3] == "Nice flat"
    # price_eur / size_m2 / lat / lng are JSON-safe strings; Postgres
    # parses them into the right column type.
    assert args[4] == "1000.00"
    assert args[5] == 2
    assert args[6] == 2
    assert args[7] == "62.5"
    assert args[8] == "Calle Test 1, Zaragoza"
    assert args[9] is None  # lat
    assert args[10] is None  # lng
    assert args[11] == "3-bedroom, 2-bath"
    assert args[12] is None  # pet_policy
    assert json.loads(args[13]) == {"soup": "raw payload"}
    # scraped_at is converted from the JSON-safe ISO string back to a
    # `datetime` before binding, because asyncpg's timestamptz codec
    # only accepts `datetime` objects.
    from datetime import datetime as _dt

    assert isinstance(args[14], _dt)


@pytest.mark.asyncio
async def test_upsert_returns_duplicate_when_no_row_returned():
    pool = _FakePool()
    pool.queued_fetchrow.append(None)  # ON CONFLICT DO NOTHING returns 0 rows
    repo = PostgresApartmentRepository(pool)

    result = await repo.upsert(_make_apartment())
    assert isinstance(result, Duplicate)
    assert result.external_id == "abc-1"


@pytest.mark.asyncio
async def test_count_uses_count_star():
    pool = _FakePool()
    pool.queued_fetchrow.append({"n": 17})
    repo = PostgresApartmentRepository(pool)
    n = await repo.count()
    assert n == 17
    sql, _ = pool.calls[0]
    assert "count(*)" in sql.lower()


@pytest.mark.asyncio
async def test_duplicate_key_count_counts_extra_rows_per_key():
    pool = _FakePool()
    pool.queued_fetchrow.append({"n": 0})
    repo = PostgresApartmentRepository(pool)
    n = await repo.duplicate_key_count()
    assert n == 0
    sql, _ = pool.calls[0]
    assert "GROUP BY source, external_id" in sql


@pytest.mark.asyncio
async def test_recent_orders_by_scraped_at_desc_and_caps_at_limit():
    pool = _FakePool()
    pool.queued_fetch.append(
        [
            {
                "id": 1,
                "source": "fotocasa",
                "external_id": "x",
                "url": "u",
                "title": None,
                "price_eur": None,
                "rooms": None,
                "bathrooms": None,
                "size_m2": None,
                "address": None,
                "lat": None,
                "lng": None,
                "description": None,
                "pet_policy": None,
                "raw_json": {"k": "v"},
                "scraped_at": datetime(2026, 1, 2, tzinfo=UTC),
            }
        ]
    )
    repo = PostgresApartmentRepository(pool)
    rows = await repo.recent(limit=3)
    assert len(rows) == 1
    assert rows[0].external_id == "x"
    sql, args = pool.calls[0]
    assert "ORDER BY scraped_at DESC" in sql
    assert args == (3,)


@pytest.mark.asyncio
async def test_close_does_not_close_underlying_pool():
    pool = _FakePool()
    repo = PostgresApartmentRepository(pool)
    # Should be a no-op, not a real close.
    await repo.close()


def test_row_to_apartment_decodes_raw_jsonb():
    row = {
        "id": 1,
        "source": "fotocasa",
        "external_id": "x",
        "url": "u",
        "title": "t",
        "price_eur": None,
        "rooms": None,
        "bathrooms": None,
        "size_m2": None,
        "address": None,
        "lat": None,
        "lng": None,
        "description": None,
        "pet_policy": None,
        "raw_json": {"nested": 1},
        "scraped_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    apt = _row_to_apartment(row)
    assert apt.raw == {"nested": 1}
