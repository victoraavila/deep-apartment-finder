"""Repository port for `dangerous_neighborhoods`.

The ranker and the researcher subagent depend on this Protocol only.
Concrete impl: `adapters/postgres/dangerous_neighborhood_repository.py`.
The CLI's `list-dangerous` subcommand is also a consumer.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from deep_apartment_finder.domain.geo import DangerousNeighborhood


@runtime_checkable
class DangerousNeighborhoodRepository(Protocol):
    """Persistence boundary for dangerous-neighborhoods constants.

    The researcher subagent `upserts` on first run; the operator can
    also upsert manually. The ranker only ever calls `list_all()`.
    """

    async def list_all(self) -> list[DangerousNeighborhood]: ...

    async def upsert_many(self, rows: list[DangerousNeighborhood], source: str) -> int:
        """Insert or update by `name`. Returns the number of rows written."""
        ...

    async def count(self) -> int: ...


__all__ = ["DangerousNeighborhoodRepository"]
