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


@dataclass(frozen=True, slots=True)
class Updated:
    """Sprint 3: existing row, at least one soft field changed.

    The repository distinguishes three outcomes of `upsert`:

    - `Inserted` — new row.
    - `Updated` — existing row, at least one of the backfillable
      soft fields (`pet_policy`, `furnished`, `lat`, `lng`,
      `description`) changed; the row was rewritten with the
      `COALESCE(EXCLUDED.x, apartments.x)` semantics in
      `003_sprint3.sql`.
    - `Duplicate` — existing row, nothing changed (the `WHERE`
      clause matched zero rows).

    Carries the `apartment_id` so the `ingest_apartment` tool can
    surface a meaningful handoff summary, and the `changed_fields`
    list so the operator can see which columns moved.
    """

    apartment_id: int
    changed_fields: tuple[str, ...] = ()


IngestResult = Inserted | Updated | Duplicate


@runtime_checkable
class ApartmentRepository(Protocol):
    """Persistence boundary for `Apartment` aggregates.

    Implementations must be safe to call from async contexts. The `upsert`
    contract is *exactly*: insert if `(source, external_id)` is new, return
    `Updated` if the row existed and a backfillable field changed, or
    `Duplicate` otherwise — never raise on the dedup case (this is how
    acceptance criterion (3) is satisfied, including the Sprint 3
    duplicate-backfill path).
    """

    async def upsert(self, apartment: Apartment) -> IngestResult: ...

    async def count(self) -> int: ...

    async def duplicate_key_count(self) -> int: ...

    async def cross_portal_dup_count(self) -> int:
        """Return the number of cross-portal dedup-key collisions.

        Sprint 3 Pillar F: counts the number of distinct `dedup_key`
        values that map to more than one apartment row. A value of 0
        means no two portals have produced the same key (no
        cross-portal overlap to report); a value of N means N
        distinct keys each map to two or more rows. The default
        in-memory implementation returns 0.
        """
        ...

    async def field_coverage(self) -> dict[str, dict[str, dict[str, float]]]:
        """Return per-source, per-field null-rate + invalid-coordinate count.

        The result is a nested dict shaped as
        `{source: {field: {non_null_rate, invalid_coord_count}}}` so
        the `validate-quality` CLI can pretty-print it. The default
        in-memory implementation returns `{}`.
        """
        ...

    async def recent(self, limit: int = 10) -> list[Apartment]: ...

    async def list_all(
        self, limit: int = 5000
    ) -> list[tuple[int, Apartment]]:
        """Return every stored apartment, capped at `limit`.

        The result is a list of `(db_id, Apartment)` tuples so the
        ranker can write `apartment_scores.apartment_id` rows that
        satisfy the FK. The cap is a safety belt for the first few
        runs; the ranker sorts + takes the top N from whatever it
        gets, so the cap doesn't change correctness.
        """
        ...

    async def list_by_dedup_key(
        self, dedup_key: str
    ) -> list[tuple[int, Apartment]]:
        """Return every apartment whose `dedup_key` matches.

        Used by `compute_ranking` to drop the lower-scoring sibling
        from the top-N. Returns `[]` when no row has the key (the
        common case for Sprint 1/2 rows that don't have a
        `dedup_key` yet).
        """
        ...

    async def close(self) -> None: ...
