"""Postgres implementation of the ApartmentRepository port.

The `upsert` operation is the heart of acceptance criterion (3) (no
duplicates on re-run). We use `ON CONFLICT (source, external_id) DO NOTHING
RETURNING id` and detect "nothing was inserted" by a 0-row result, returning
`Duplicate` rather than raising. This is the same shape as the
`InMemoryApartmentRepository`, so tests of the orchestrator can use the
fake and the real one interchangeably.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import asyncpg

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.source import Source
from deep_apartment_finder.ports.apartment_repository import (
    ApartmentRepository,
    Duplicate,
    Inserted,
)

_UPSERT_SQL = """
INSERT INTO apartments (
    source, external_id, url, title, price_eur, rooms, bathrooms,
    size_m2, address, lat, lng, description, pet_policy, furnished,
    raw_json, scraped_at
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
    $15::jsonb, $16
)
ON CONFLICT (source, external_id) DO NOTHING
RETURNING id
"""

_COUNT_SQL = "SELECT count(*) AS n FROM apartments"

_DUPLICATE_KEY_COUNT_SQL = """
SELECT COALESCE(sum(n - 1), 0)::int AS n
FROM (
    SELECT source, external_id, count(*) AS n
    FROM apartments
    GROUP BY source, external_id
    HAVING count(*) > 1
) duplicates
"""

_RECENT_SQL = """
SELECT id, source, external_id, url, title, price_eur, rooms, bathrooms,
       size_m2, address, lat, lng, description, pet_policy, furnished,
       raw_json, scraped_at
FROM apartments
ORDER BY scraped_at DESC
LIMIT $1
"""


def _row_to_apartment(row: asyncpg.Record) -> Apartment:
    raw_json = row["raw_json"]
    # asyncpg returns jsonb as a Python object (dict or list), not a string.
    if isinstance(raw_json, str):
        try:
            raw_json = json.loads(raw_json)
        except json.JSONDecodeError:
            raw_json = {"_raw_text": raw_json}
    return Apartment(
        source=Source(row["source"]),
        external_id=row["external_id"],
        url=row["url"],
        title=row["title"],
        price_eur=row["price_eur"],
        rooms=row["rooms"],
        bathrooms=row["bathrooms"],
        size_m2=row["size_m2"],
        address=row["address"],
        lat=row["lat"],
        lng=row["lng"],
        description=row["description"],
        pet_policy=row["pet_policy"],
        furnished=row["furnished"],
        raw=raw_json or {},
        scraped_at=row["scraped_at"],
    )


class PostgresApartmentRepository(ApartmentRepository):
    """Concrete `ApartmentRepository` backed by an asyncpg pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def upsert(self, apartment: Apartment) -> Inserted | Duplicate:
        d = apartment.to_ingest_dict()
        # `to_ingest_dict` produces JSON-safe scalars (Decimals as str,
        # scraped_at as ISO 8601). asyncpg binds these to the SQL
        # parameters; Postgres parses numeric fields from text. We
        # convert the ISO timestamp back to a `datetime` because
        # asyncpg's `timestamptz` codec only accepts that.
        scraped_at = d["scraped_at"]
        if isinstance(scraped_at, str) and scraped_at:
            scraped_at = datetime.fromisoformat(scraped_at)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                _UPSERT_SQL,
                d["source"],
                d["external_id"],
                d["url"],
                d["title"],
                d["price_eur"],
                d["rooms"],
                d["bathrooms"],
                d["size_m2"],
                d["address"],
                d["lat"],
                d["lng"],
                d["description"],
                d["pet_policy"],
                d["furnished"],
                json.dumps(d["raw_json"], default=_json_default),
                scraped_at,
            )
        if row is None:
            return Duplicate(external_id=apartment.external_id)
        return Inserted(apartment_id=row["id"])

    async def count(self) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_COUNT_SQL)
        return int(row["n"]) if row else 0

    async def duplicate_key_count(self) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_DUPLICATE_KEY_COUNT_SQL)
        return int(row["n"]) if row else 0

    async def recent(self, limit: int = 10) -> list[Apartment]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_RECENT_SQL, limit)
        return [_row_to_apartment(r) for r in rows]

    async def close(self) -> None:
        # We do NOT close the pool here — it's shared with the migration
        # runner and the composition root owns its lifecycle. Closing it
        # would force every other consumer to acquire a fresh pool.
        return None

    async def list_all(
        self, limit: int = 5000
    ) -> list[tuple[int, Apartment]]:
        """Return every stored apartment (capped at `limit`).

        The ranker consumes this. The cap is a safety belt for the
        first few runs; the ranker sorts + takes the top N from
        whatever it gets, so the cap doesn't change correctness.
        """
        sql = (
            "SELECT id, source, external_id, url, title, price_eur, rooms, "
            "bathrooms, size_m2, address, lat, lng, description, pet_policy, "
            "furnished, raw_json, scraped_at "
            "FROM apartments ORDER BY scraped_at DESC LIMIT $1"
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, limit)
        return [(int(r["id"]), _row_to_apartment(r)) for r in rows]


def _json_default(obj: object) -> str:
    """Fallback JSON encoder for Decimal / datetime values that survive
    in `raw` after parsing."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Cannot JSON-encode {type(obj).__name__}")
