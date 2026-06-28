"""Apartment value object.

Mirrors the columns of the `apartments` table. The `Apartment` is an
immutable view that the parser produces and the repository persists. New
fields (pet_policy, embedding) are nullable on purpose — they are populated
in later sprints without breaking this object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from deep_apartment_finder.domain.source import Source


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Apartment:
    """A normalized rental listing.

    `raw` carries the parser's full original payload for replay/debug, per
    `SPRINT1.md` line 79. The `scraped_at` defaults to "now" at construction
    time so the parser doesn't have to set it on every yield.
    """

    source: Source
    external_id: str
    url: str
    title: str | None = None
    price_eur: Decimal | None = None
    rooms: int | None = None
    bathrooms: int | None = None
    size_m2: Decimal | None = None
    address: str | None = None
    lat: Decimal | None = None
    lng: Decimal | None = None
    description: str | None = None
    pet_policy: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    scraped_at: datetime = field(default_factory=_utcnow)

    def to_ingest_dict(self) -> dict[str, Any]:
        """Shape consumed by the repository's `upsert`.

        `raw` is persisted as `raw_json`; we keep the keys separate so the
        repository never has to know the column-vs-field-name mapping.

        `Decimal` columns (`price_eur`, `size_m2`, `lat`, `lng`) and the
        `scraped_at` timestamp are coerced to JSON-safe scalars (`str`)
        so the dict is round-trippable through `json.dumps`. Postgres
        parses numeric fields from text natively; the repository
        converts the ISO 8601 timestamp back to a `datetime` before
        binding it to the `timestamptz` column (asyncpg does not
        accept ISO strings directly).
        """
        return {
            "source": self.source.value,
            "external_id": self.external_id,
            "url": self.url,
            "title": self.title,
            "price_eur": str(self.price_eur) if self.price_eur is not None else None,
            "rooms": self.rooms,
            "bathrooms": self.bathrooms,
            "size_m2": str(self.size_m2) if self.size_m2 is not None else None,
            "address": self.address,
            "lat": str(self.lat) if self.lat is not None else None,
            "lng": str(self.lng) if self.lng is not None else None,
            "description": self.description,
            "pet_policy": self.pet_policy,
            "raw_json": self.raw,
            "scraped_at": self.scraped_at.isoformat() if self.scraped_at else None,
        }

    @classmethod
    def from_raw_dict(
        cls, source: Source, external_id: str, url: str, data: dict[str, Any]
    ) -> Apartment:
        """Build an `Apartment` from a parser dict, tolerating missing fields.

        Decimal fields are constructed from the right type (int / float / str)
        so the parser can pass strings from CSS-attribute parsing without
        casting.
        """
        return cls(
            source=source,
            external_id=external_id,
            url=url,
            title=_maybe_str(data.get("title")),
            price_eur=_maybe_decimal(data.get("price_eur")),
            rooms=_maybe_int(data.get("rooms")),
            bathrooms=_maybe_int(data.get("bathrooms")),
            size_m2=_maybe_decimal(data.get("size_m2")),
            address=_maybe_str(data.get("address")),
            lat=_maybe_decimal(data.get("lat")),
            lng=_maybe_decimal(data.get("lng")),
            description=_maybe_str(data.get("description")),
            pet_policy=_maybe_str(data.get("pet_policy")),
            raw=data.get("raw", data),
            scraped_at=_utcnow(),
        )


def _maybe_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _maybe_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def _maybe_decimal(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None
