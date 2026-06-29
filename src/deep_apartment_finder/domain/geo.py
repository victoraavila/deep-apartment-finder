"""Geo helpers used by the ranker.

Pure functions, no I/O. Everything is meters-based and the inputs are
decimals / floats — callers are responsible for parsing whatever the
DB returned. The functions are the same ones the unit tests exercise.

Sprint 2 ships only the haversine variant. Sprint 2's "future
sprint" item is to add `OsrmDistanceProvider` behind the same
`DistanceProvider` port (deferred — see `docs/adr/007-*.md`).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class DangerousNeighborhood:
    """A single row from `dangerous_neighborhoods`.

    `center_lat` and `center_lng` are decimal degrees; `radius_m` is
    meters. The shape is what the ranker's `DistanceToDangerousCriterion`
    consumes, and what the Postgres adapter yields. We keep this as a
    domain value object (not a dict) so the ranker is testable without
    a DB.
    """

    name: str
    center_lat: float
    center_lng: float
    radius_m: int


_EARTH_RADIUS_M = 6_371_000.0


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in meters between two WGS-84 points.

    Standard haversine formula. Inputs are decimal degrees, output is
    meters. Identical-point inputs return exactly 0.0.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    c = 2.0 * math.asin(min(1.0, math.sqrt(a)))
    return _EARTH_RADIUS_M * c


def nearest_dangerous_distance_m(
    lat: float, lng: float, neighborhoods: Iterable[DangerousNeighborhood]
) -> float | None:
    """Distance in meters to the *nearest* dangerous neighborhood center.

    Returns `None` if the iterable is empty (caller decides what a
    "no dangerous neighborhoods" score means — the ranker treats that
    as a neutral 0.5 + warning log).
    """
    nearest: float | None = None
    for n in neighborhoods:
        d = haversine_meters(lat, lng, n.center_lat, n.center_lng)
        if nearest is None or d < nearest:
            nearest = d
    return nearest


def nearest_dangerous_boundary_distance_m(
    lat: float, lng: float, neighborhoods: Iterable[DangerousNeighborhood]
) -> float | None:
    """Distance in meters to the nearest dangerous neighborhood boundary.

    Returns 0.0 when the point is inside any neighborhood radius. Returns
    `None` if the iterable is empty.
    """
    nearest: float | None = None
    for n in neighborhoods:
        center_distance = haversine_meters(lat, lng, n.center_lat, n.center_lng)
        boundary_distance = max(0.0, center_distance - float(n.radius_m))
        if nearest is None or boundary_distance < nearest:
            nearest = boundary_distance
    return nearest


def in_dangerous_neighborhood(
    lat: float, lng: float, neighborhood: DangerousNeighborhood
) -> bool:
    """True iff `(lat, lng)` is within `neighborhood.radius_m` of its center.

    Uses haversine as the distance function. A listing exactly on the
    boundary is considered *inside* (`<=`).
    """
    d = haversine_meters(lat, lng, neighborhood.center_lat, neighborhood.center_lng)
    return d <= neighborhood.radius_m


def is_in_dangerous_neighborhood(
    lat: float, lng: float, neighborhood: DangerousNeighborhood
) -> bool:
    """Alias matching the Sprint 2 spec wording."""
    return in_dangerous_neighborhood(lat, lng, neighborhood)


# --- Sprint 3: invalid-coordinate normalisation ----------------------------

# Coarse Zaragoza bounding box used to detect "obviously bogus" coordinates
# a scraper might have left behind. The lat range covers the metropolitan
# area and surrounding pueblos; the lng range covers the province. Anything
# outside is treated as a placeholder the scraper left in (the sea, the
# equator prime-meridian intersection, etc.) and stored as NULL, not 0.
_ZARAGOZA_LAT_RANGE: tuple[float, float] = (41.5, 41.8)
_ZARAGOZA_LNG_RANGE: tuple[float, float] = (-1.05, -0.8)


def is_valid_coordinate(lat: Any, lng: Any) -> bool:
    """Return `True` iff `(lat, lng)` looks like a real Zaragoza coordinate.

    Rejects:

    - `None` for either component
    - `(0, 0)` — the classic scraper placeholder (Gulf of Guinea)
    - Any point outside a coarse Zaragoza bounding box
      `lat ∈ [41.5, 41.8]`, `lng ∈ [-1.05, -0.8]`

    Accepts `float`, `int`, or `Decimal` (the type the apartment value
    object uses for `lat`/`lng`). The function never raises; unparseable
    inputs are treated as invalid.
    """
    if lat is None or lng is None:
        return False
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError):
        return False
    if lat_f == 0.0 and lng_f == 0.0:
        return False
    if not (math.isfinite(lat_f) and math.isfinite(lng_f)):
        return False
    lat_min, lat_max = _ZARAGOZA_LAT_RANGE
    lng_min, lng_max = _ZARAGOZA_LNG_RANGE
    if not (lat_min <= lat_f <= lat_max):
        return False
    if not (lng_min <= lng_f <= lng_max):
        return False
    return True


# --- Sprint 3: cross-portal dedup key --------------------------------------

import hashlib  # noqa: E402  (kept here to keep the module's import surface tidy)


def _normalize_address(address: str | None) -> str:
    """Lowercase + collapse whitespace + strip any embedded 5-digit zipcode.

    Two scrapers frequently write the same physical street with minor
    differences ("Calle Test  1, 50001 Zaragoza" vs "Calle Test, 1,
    zaragoza"). The normalisation:

    - lowercases the whole string,
    - collapses every run of whitespace to a single space,
    - drops any 5-digit token (Spanish postal code) so a zipcode
      present in one portal's address but not the other doesn't
      break the match.

    Punctuation other than spaces is preserved on purpose: "Calle
    Test, 1" and "Calle Test 1" are arguably the same listing, but
    collapsing commas is a step too far for the key to be deterministic
    across portals. The bucket widths on size and price absorb the
    small drift the punctuation causes.
    """
    if not address:
        return ""
    lowered = str(address).lower()
    # Split on any whitespace, drop the 5-digit tokens, re-join with
    # a single space.
    tokens = [t for t in lowered.split() if not (len(t) == 5 and t.isdigit())]
    return " ".join(tokens)


def compute_dedup_key(
    *,
    address: str | None,
    rooms: int | None,
    size_m2: Decimal | float | int | None,
    price_eur: Decimal | float | int | None,
) -> str | None:
    """Return a deterministic dedup key for cross-portal dedup.

    The key is the SHA-1 of `"|".join([normalized_address, rooms,
    size_bucket, price_bucket])` where:

    - `normalized_address` — `address` lowercased + whitespace-collapsed
      + trailing-zipcode-stripped
    - `rooms` — exact integer (no bucket)
    - `size_bucket` — `round(size_m2 / 5) * 5` (±2.5 m² tolerance)
    - `price_bucket` — `round(price_eur / 25) * 25` (±12.5 € tolerance)

    Returns `None` when `address` is empty or `rooms` is unknown — the
    scraper can't have produced a comparable listing without those
    fields, and we'd rather have NULL than a degenerate key like
    `"||0|0"`.

    Per ADR-012 the key is deliberately approximate: two portals
    frequently list the same physical apartment with small field
    drift (e.g. m² = 65 vs 67, price = 950 vs 975). The bucket widths
    absorb that drift while keeping unrelated listings apart.
    """
    norm_addr = _normalize_address(address)
    if not norm_addr or rooms is None or size_m2 is None or price_eur is None:
        return None
    try:
        size_bucket = round(float(size_m2) / 5.0) * 5
        price_bucket = round(float(price_eur) / 25.0) * 25
    except (TypeError, ValueError):
        return None
    raw = "|".join([norm_addr, str(int(rooms)), str(int(size_bucket)), str(int(price_bucket))])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


__all__ = [
    "DangerousNeighborhood",
    "compute_dedup_key",
    "haversine_meters",
    "in_dangerous_neighborhood",
    "is_in_dangerous_neighborhood",
    "is_valid_coordinate",
    "nearest_dangerous_boundary_distance_m",
    "nearest_dangerous_distance_m",
]
