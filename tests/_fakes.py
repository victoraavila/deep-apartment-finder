"""In-memory implementations of the ports, used by unit + integration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from typing import Any
from uuid import UUID

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.filters.hard import HardFilters
from deep_apartment_finder.domain.geo import DangerousNeighborhood
from deep_apartment_finder.domain.source import Source
from deep_apartment_finder.ports.apartment_repository import (
    ApartmentRepository,
    Duplicate,
    Inserted,
    Updated,
)
from deep_apartment_finder.ports.dangerous_neighborhood_repository import (
    DangerousNeighborhoodRepository,
)
from deep_apartment_finder.ports.ranking_repository import (
    NotificationAlreadySent,
    RankingRepository,
    ScoreRow,
)
from deep_apartment_finder.ports.scraper import ListingCard, ScraperPort

# Sprint 3: fields whose update should trigger an `Updated` result
# (mirrors the COALESCE list in `003_sprint3.sql`).
_BACKFILL_FIELDS: tuple[str, ...] = (
    "pet_policy",
    "furnished",
    "lat",
    "lng",
    "description",
)


def _diff_backfill_fields(existing: Apartment, new: Apartment) -> tuple[str, ...]:
    """Return the list of backfillable fields that differ between two apartments.

    `None` is treated as "older" — a real value supersedes a missing
    one (the COALESCE semantics in `003_sprint3.sql`).
    """
    changed: list[str] = []
    for field_name in _BACKFILL_FIELDS:
        old = getattr(existing, field_name)
        new_v = getattr(new, field_name)
        if new_v is None:
            continue
        if old != new_v:
            changed.append(field_name)
    return tuple(changed)


class InMemoryApartmentRepository(ApartmentRepository):
    """ApartmentRepository that lives in a dict, with the same dedup contract
    as the Postgres adapter (Inserted vs Duplicate, never raises on dup).

    Sprint 3: also returns `Updated` when an existing row is rewritten
    with new backfillable soft fields (`pet_policy`, `furnished`,
    `lat`, `lng`, `description`). The fake implements the same
    COALESCE semantics as the migration: a non-None new value
    supersedes an existing None, and a different non-None value
    supersedes the old one.
    """

    def __init__(self) -> None:
        self._by_id: dict[int, Apartment] = {}
        self._by_source_ext: dict[tuple[str, str], int] = {}
        self._next_id = 1

    async def upsert(
        self, apartment: Apartment
    ) -> Inserted | Updated | Duplicate:
        key = (apartment.source.value, apartment.external_id)
        existing_id = self._by_source_ext.get(key)
        if existing_id is None:
            new_id = self._next_id
            self._next_id += 1
            self._by_id[new_id] = apartment
            self._by_source_ext[key] = new_id
            return Inserted(apartment_id=new_id)

        existing = self._by_id[existing_id]
        changed = _diff_backfill_fields(existing, apartment)
        if not changed:
            return Duplicate(external_id=apartment.external_id)
        # COALESCE semantics: a non-None new value supersedes the old.
        merged: dict[str, Any] = {}
        for f in _BACKFILL_FIELDS:
            new_v = getattr(apartment, f)
            old_v = getattr(existing, f)
            merged[f] = new_v if new_v is not None else old_v
        # `raw_json` is always replaced (Sprint 3 spec).
        merged_raw = apartment.raw
        # Replace the stored apartment in-place with the merged one.
        merged_apartment = Apartment(
            source=apartment.source,
            external_id=apartment.external_id,
            url=apartment.url,
            title=apartment.title,
            price_eur=apartment.price_eur,
            rooms=apartment.rooms,
            bathrooms=apartment.bathrooms,
            size_m2=apartment.size_m2,
            address=apartment.address,
            lat=merged["lat"],
            lng=merged["lng"],
            description=merged["description"],
            pet_policy=merged["pet_policy"],
            furnished=merged["furnished"],
            raw=merged_raw,
            scraped_at=apartment.scraped_at,
        )
        self._by_id[existing_id] = merged_apartment
        return Updated(apartment_id=existing_id, changed_fields=changed)

    async def count(self) -> int:
        return len(self._by_id)

    async def duplicate_key_count(self) -> int:
        return 0

    async def cross_portal_dup_count(self) -> int:
        """Count distinct dedup_keys that map to 2+ rows."""
        groups: dict[str, int] = {}
        for apt in self._by_id.values():
            key = apt.raw.get("dedup_key") if isinstance(apt.raw, dict) else None
            if not key:
                continue
            groups[key] = groups.get(key, 0) + 1
        return sum(1 for n in groups.values() if n > 1)

    async def field_coverage(self) -> dict[str, dict[str, dict[str, float]]]:
        """Per-source, per-field null rate + invalid-coordinate count.

        The fake reads `dedup_key` from the raw blob and treats
        `lat`/`lng` as the apartment's typed fields.
        """
        coverage: dict[str, dict[str, dict[str, float]]] = {}
        # Group apartments by source
        per_source: dict[str, list[Apartment]] = {}
        for apt in self._by_id.values():
            per_source.setdefault(apt.source.value, []).append(apt)
        for source, apts in per_source.items():
            n = len(apts)
            if n == 0:
                continue
            per_field: dict[str, dict[str, float]] = {}
            for f in ("lat", "lng", "pet_policy", "furnished", "description"):
                non_null = sum(1 for a in apts if getattr(a, f) is not None)
                per_field[f] = {"non_null_rate": non_null / n, "n": float(n)}
            # Invalid-coordinate count
            invalid = 0
            for a in apts:
                if a.lat is None or a.lng is None:
                    continue
                try:
                    if float(a.lat) == 0.0 and float(a.lng) == 0.0:
                        invalid += 1
                except (TypeError, ValueError):
                    invalid += 1
            per_field["invalid_coordinates"] = {"count": float(invalid), "n": float(n)}
            coverage[source] = per_field
        return coverage

    async def recent(self, limit: int = 10) -> list[Apartment]:
        items = list(self._by_id.values())
        items.sort(key=lambda a: a.scraped_at, reverse=True)
        return items[:limit]

    async def list_all(self, limit: int = 5000) -> list[tuple[int, Apartment]]:
        items = sorted(
            self._by_id.items(), key=lambda kv: kv[1].scraped_at, reverse=True
        )
        return [(db_id, apt) for db_id, apt in items[:limit]]

    async def list_by_dedup_key(
        self, dedup_key: str
    ) -> list[tuple[int, Apartment]]:
        out: list[tuple[int, Apartment]] = []
        for db_id, apt in self._by_id.items():
            raw = apt.raw if isinstance(apt.raw, dict) else {}
            if raw.get("dedup_key") == dedup_key:
                out.append((db_id, apt))
        return out

    async def close(self) -> None:
        return None


class FakeScraper(ScraperPort):
    """A scraper that yields a fixed list of cards, then maps each card to a
    detail `Apartment` from a fixture dict. No I/O."""

    def __init__(
        self,
        cards: list[ListingCard] | None = None,
        details: dict[str, Apartment] | None = None,
    ) -> None:
        self._cards = cards or []
        self._details = details or {}
        self.search_calls: list[HardFilters] = []
        self.fetch_calls: list[str] = []

    async def search_listings(self, filters: HardFilters) -> AsyncIterator[ListingCard]:
        self.search_calls.append(filters)
        for card in self._cards:
            yield card

    async def fetch_listing(self, url: str) -> Apartment:
        self.fetch_calls.append(url)
        if url not in self._details:
            raise KeyError(f"FakeScraper has no detail for {url}")
        return self._details[url]

    async def close(self) -> None:
        return None


class InMemoryDangerousNeighborhoodRepository(DangerousNeighborhoodRepository):
    """In-memory dangerous-neighborhoods store for tests.

    Mirrors the Postgres adapter's behavior: `upsert_many` matches on
    `name` (UNIQUE) and overwrites the row when present.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, DangerousNeighborhood] = {}

    async def list_all(self) -> list[DangerousNeighborhood]:
        return sorted(self._by_name.values(), key=lambda n: n.name)

    async def count(self) -> int:
        return len(self._by_name)

    async def upsert_many(
        self, rows: list[DangerousNeighborhood], source: str
    ) -> int:
        for n in rows:
            self._by_name[n.name] = n
        return len(rows)


class InMemoryRankingRepository(RankingRepository):
    """In-memory ranking repo: trace rows + one notification per day."""

    def __init__(self) -> None:
        self.scores: list[tuple[UUID, ScoreRow]] = []
        self.notifications: list[tuple[UUID, date, list[int]]] = []
        self._sent_on: dict[date, UUID] = {}

    async def write_scores(
        self, ranking_run_id: UUID, rows: list[ScoreRow]
    ) -> int:
        for r in rows:
            self.scores.append((ranking_run_id, r))
        return len(rows)

    async def record_send(
        self,
        *,
        ranking_run_id: UUID,
        sent_on: date,
        apartment_ids: list[int],
    ) -> int:
        if self._sent_on.get(sent_on) is not None:
            raise NotificationAlreadySent(sent_on=sent_on)
        self._sent_on[sent_on] = ranking_run_id
        self.notifications.append((ranking_run_id, sent_on, list(apartment_ids)))
        return len(self.notifications)

    async def top_for_run(
        self, ranking_run_id: UUID, top_n: int
    ) -> list[dict[str, Any]]:
        per: dict[int, tuple[float, float]] = {}
        for run_id, row in self.scores:
            if run_id != ranking_run_id:
                continue
            cur = per.get(row.apartment_id, (0.0, 0.0))
            per[row.apartment_id] = (cur[0] + row.score * row.weight, cur[1] + row.weight)
        scored: list[tuple[int, float]] = []
        for apt_id, (num, den) in per.items():
            final = (num / den) if den else 0.0
            scored.append((apt_id, final))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [
            {"apartment_id": apt_id, "score": round(score, 4)}
            for apt_id, score in scored[:top_n]
        ]

    async def delete_send_for_date(self, sent_on: date) -> int:
        before = len(self.notifications)
        self.notifications = [
            n for n in self.notifications if n[1] != sent_on
        ]
        self._sent_on.pop(sent_on, None)
        return before - len(self.notifications)


def make_apartment(
    *,
    source: Source = Source.FOTOCASA,
    external_id: str = "1",
    url: str = "https://example.com/1",
    price_eur: float | None = 1000.0,
    rooms: int | None = 2,
    bathrooms: int | None = 2,
    size_m2: float | None = 60.0,
    address: str | None = "Calle Test 1, Zaragoza",
    description: str | None = "Test listing",
    **kwargs: Any,
) -> Apartment:
    from decimal import Decimal

    return Apartment(
        source=source,
        external_id=external_id,
        url=url,
        title=kwargs.get("title", f"Apt {external_id}"),
        price_eur=Decimal(str(price_eur)) if price_eur is not None else None,
        rooms=rooms,
        bathrooms=bathrooms,
        size_m2=Decimal(str(size_m2)) if size_m2 is not None else None,
        address=address,
        lat=kwargs.get("lat"),
        lng=kwargs.get("lng"),
        description=description,
        pet_policy=kwargs.get("pet_policy"),
        furnished=kwargs.get("furnished"),
        raw=kwargs.get("raw", {}),
    )
