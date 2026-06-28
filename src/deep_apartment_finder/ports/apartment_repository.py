"""Repository port for apartments.

The agent and tools depend on this Protocol only. The concrete
`PostgresApartmentRepository` lives in `adapters/postgres/`. Tests use
`InMemoryApartmentRepository` from `tests/unit/_fakes.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from deep_apartment_finder.domain.apartment import Apartment


@dataclass(frozen=True, slots=True)
class Inserted:
    apartment_id: int


@dataclass(frozen=True, slots=True)
class Duplicate:
    external_id: str


IngestResult = Inserted | Duplicate


@runtime_checkable
class ApartmentRepository(Protocol):
    """Persistence boundary for `Apartment` aggregates.

    Implementations must be safe to call from async contexts. The `upsert`
    contract is *exactly*: insert if `(source, external_id)` is new, return
    `Duplicate` otherwise — never raise on the dedup case (this is how
    acceptance criterion (3) is satisfied).
    """

    async def upsert(self, apartment: Apartment) -> IngestResult: ...

    async def count(self) -> int: ...

    async def duplicate_key_count(self) -> int: ...

    async def recent(self, limit: int = 10) -> list[Apartment]: ...

    async def close(self) -> None: ...
