"""Postgres implementation of the ApartmentRepository port.

The `upsert` operation is the heart of acceptance criterion (3) (no
duplicates on re-run). Sprint 3 added a third outcome, `Updated`,
returned when an existing row's backfillable soft fields
(`pet_policy`, `furnished`, `lat`, `lng`, `description`) actually
changed; the COALESCE / WHERE clause in `003_sprint3.sql` makes the
rewrite a no-op when nothing changed. The SQL is the same shape as
the `InMemoryApartmentRepository`, so tests of the orchestrator can
use the fake and the real one interchangeably.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import asyncpg

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.geo import is_valid_coordinate
from deep_apartment_finder.domain.source import Source
from deep_apartment_finder.ports.apartment_repository import (
    ApartmentRepository,
    Duplicate,
    Inserted,
    Updated,
)

# Sprint 3: COALESCE on the backfillable soft fields so a re-scrape
# fills in NULLs (e.g. a Sprint 1 row without `pet_policy`); the WHERE
# clause only rewrites when at least one field is *distinct*, so
# the operation is a no-op for true no-change re-scrapes. The
# RETURNING list reports the post-update values alongside the changed
# column names so the adapter can return an `Updated` outcome
# without a second round-trip.
_UPSERT_SQL = """
INSERT INTO apartments (
    source, external_id, url, title, price_eur, rooms, bathrooms,
    size_m2, address, lat, lng, description, pet_policy, furnished,
    raw_json, scraped_at, dedup_key
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
    $15::jsonb, $16, $17
)
ON CONFLICT (source, external_id) DO UPDATE SET
    pet_policy = COALESCE(EXCLUDED.pet_policy, apartments.pet_policy),
    furnished = COALESCE(EXCLUDED.furnished, apartments.furnished),
    lat = COALESCE(EXCLUDED.lat, apartments.lat),
    lng = COALESCE(EXCLUDED.lng, apartments.lng),
    description = COALESCE(EXCLUDED.description, apartments.description),
    raw_json = EXCLUDED.raw_json,
    scraped_at = EXCLUDED.scraped_at,
    dedup_key = COALESCE(EXCLUDED.dedup_key, apartments.dedup_key)
WHERE
    apartments.pet_policy IS DISTINCT FROM EXCLUDED.pet_policy
    OR apartments.furnished IS DISTINCT FROM EXCLUDED.furnished
    OR apartments.lat IS DISTINCT FROM EXCLUDED.lat
    OR apartments.lng IS DISTINCT FROM EXCLUDED.lng
    OR apartments.description IS DISTINCT FROM EXCLUDED.description
RETURNING id,
    (xmax = 0) AS inserted,
    pet_policy, furnished, lat, lng, description
"""

# When the WHERE clause matches no rows, RETURNING yields 0 rows.
# We need to disambiguate: 0 rows = `Duplicate`, 1 row = either
# `Inserted` (xmax = 0) or `Updated` (xmax != 0). The WHERE clause
# ensures that an `Updated` row always reflects a real change.

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

_CROSS_PORTAL_DUP_COUNT_SQL = """
SELECT COALESCE(count(*), 0)::int AS n
FROM (
    SELECT dedup_key
    FROM apartments
    WHERE dedup_key IS NOT NULL
    GROUP BY dedup_key
    HAVING count(*) > 1
) d
"""

_FIELD_COVERAGE_SQL = """
SELECT source, lat, lng, pet_policy, furnished, description
FROM apartments
"""

_RECENT_SQL = """
SELECT id, source, external_id, url, title, price_eur, rooms, bathrooms,
       size_m2, address, lat, lng, description, pet_policy, furnished,
       raw_json, scraped_at, dedup_key
FROM apartments
ORDER BY scraped_at DESC
LIMIT $1
"""

_LIST_BY_DEDUP_KEY_SQL = """
SELECT id, source, external_id, url, title, price_eur, rooms, bathrooms,
       size_m2, address, lat, lng, description, pet_policy, furnished,
       raw_json, scraped_at, dedup_key
FROM apartments
WHERE dedup_key = $1
ORDER BY id ASC
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


# Fields the WHERE clause checks; the adapter uses the same list to
# decide what to put in `Updated.changed_fields`.
_BACKFILL_FIELDS: tuple[str, ...] = (
    "pet_policy",
    "furnished",
    "lat",
    "lng",
    "description",
)


def _derive_changed_fields(
    row: asyncpg.Record, new_apartment: Apartment
) -> tuple[str, ...]:
    """Return the list of backfillable fields the new apartment
    actually set (i.e. that are non-None).

    The Postgres `WHERE ... IS DISTINCT FROM` guarantees that at
    least one of these fields actually changed. The adapter does
    not know *which* one(s) without a second round-trip; instead, we
    report every backfillable field the new apartment is providing
    a non-None value for, which is what the operator wants to see
    ("this scrape backfilled these fields"). A field is included iff
    the new value is non-None (the COALESCE preserves None, so a
    None new value cannot be a backfill).
    """
    changed: list[str] = []
    for f in _BACKFILL_FIELDS:
        new_v = getattr(new_apartment, f)
        if new_v is None:
            continue
        changed.append(f)
    return tuple(changed)


class PostgresApartmentRepository(ApartmentRepository):
    """Concrete `ApartmentRepository` backed by an asyncpg pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def upsert(
        self, apartment: Apartment
    ) -> Inserted | Updated | Duplicate:
        d = apartment.to_ingest_dict()
        # `to_ingest_dict` produces JSON-safe scalars (Decimals as str,
        # scraped_at as ISO 8601). asyncpg binds these to the SQL
        # parameters; Postgres parses numeric fields from text. We
        # convert the ISO timestamp back to a `datetime` because
        # asyncpg's `timestamptz` codec only accepts that.
        scraped_at = d["scraped_at"]
        if isinstance(scraped_at, str) and scraped_at:
            scraped_at = datetime.fromisoformat(scraped_at)
        dedup_key = (apartment.raw or {}).get("dedup_key")
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
                dedup_key,
            )
        if row is None:
            return Duplicate(external_id=apartment.external_id)
        if row["inserted"]:
            return Inserted(apartment_id=int(row["id"]))
        changed = _derive_changed_fields(row, apartment)
        if not changed:
            # Defensive: should not happen (WHERE guarantees at least
            # one distinct value), but if it does, return Duplicate.
            return Duplicate(external_id=apartment.external_id)
        return Updated(apartment_id=int(row["id"]), changed_fields=changed)

    async def count(self) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_COUNT_SQL)
        return int(row["n"]) if row else 0

    async def duplicate_key_count(self) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_DUPLICATE_KEY_COUNT_SQL)
        return int(row["n"]) if row else 0

    async def cross_portal_dup_count(self) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_CROSS_PORTAL_DUP_COUNT_SQL)
        return int(row["n"]) if row else 0

    async def field_coverage(self) -> dict[str, dict[str, dict[str, float]]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_FIELD_COVERAGE_SQL)
        per_source: dict[str, list[Any]] = {}
        for r in rows:
            per_source.setdefault(r["source"], []).append(r)
        out: dict[str, dict[str, dict[str, float]]] = {}
        for source, source_rows in per_source.items():
            n = len(source_rows)
            if n == 0:
                continue
            per_field: dict[str, dict[str, float]] = {}
            for f in ("lat", "lng", "pet_policy", "furnished", "description"):
                non_null = sum(1 for r in source_rows if r[f] is not None)
                per_field[f] = {
                    "non_null_rate": non_null / n,
                    "non_null_count": float(non_null),
                    "n": float(n),
                }
            invalid = 0
            for r in source_rows:
                if r["lat"] is None or r["lng"] is None:
                    continue
                if not is_valid_coordinate(r["lat"], r["lng"]):
                    invalid += 1
            per_field["invalid_coordinates"] = {
                "count": float(invalid),
                "n": float(n),
            }
            out[source] = per_field
        return out

    async def recent(self, limit: int = 10) -> list[Apartment]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_RECENT_SQL, limit)
        return [_row_to_apartment(r) for r in rows]

    async def list_by_dedup_key(
        self, dedup_key: str
    ) -> list[tuple[int, Apartment]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_LIST_BY_DEDUP_KEY_SQL, dedup_key)
        return [(int(r["id"]), _row_to_apartment(r)) for r in rows]

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
            "furnished, raw_json, scraped_at, dedup_key "
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
