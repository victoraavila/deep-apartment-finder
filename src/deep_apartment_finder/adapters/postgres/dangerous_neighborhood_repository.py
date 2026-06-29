"""Postgres impl of `DangerousNeighborhoodRepository`.

The `upsert_many` call uses `INSERT ... ON CONFLICT (name) DO UPDATE`
so the operator can run it repeatedly and re-runs of the researcher
are idempotent. The `source` argument is the tag we want to attribute
the upsert to (e.g. `'researcher:web:elpais-2024-...'` or
`'operator:cli'`).
"""

from __future__ import annotations

from decimal import Decimal

import asyncpg

from deep_apartment_finder.domain.geo import DangerousNeighborhood
from deep_apartment_finder.ports.dangerous_neighborhood_repository import (
    DangerousNeighborhoodRepository,
)


class PostgresDangerousNeighborhoodRepository(DangerousNeighborhoodRepository):
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def list_all(self) -> list[DangerousNeighborhood]:
        sql = (
            "SELECT name, center_lat, center_lng, radius_m "
            "FROM dangerous_neighborhoods "
            "ORDER BY name"
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql)
        out: list[DangerousNeighborhood] = []
        for r in rows:
            out.append(
                DangerousNeighborhood(
                    name=r["name"],
                    center_lat=float(r["center_lat"]),
                    center_lng=float(r["center_lng"]),
                    radius_m=int(r["radius_m"]),
                )
            )
        return out

    async def count(self) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT count(*) AS n FROM dangerous_neighborhoods")
        return int(row["n"]) if row else 0

    async def upsert_many(
        self, rows: list[DangerousNeighborhood], source: str
    ) -> int:
        if not rows:
            return 0
        sql = """
            INSERT INTO dangerous_neighborhoods
                (name, center_lat, center_lng, radius_m, source)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (name) DO UPDATE SET
                center_lat = EXCLUDED.center_lat,
                center_lng = EXCLUDED.center_lng,
                radius_m   = EXCLUDED.radius_m,
                source     = EXCLUDED.source
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for n in rows:
                    await conn.execute(
                        sql,
                        n.name,
                        Decimal(str(n.center_lat)),
                        Decimal(str(n.center_lng)),
                        n.radius_m,
                        source,
                    )
        return len(rows)


__all__ = ["PostgresDangerousNeighborhoodRepository"]
