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


__all__ = [
    "DangerousNeighborhood",
    "haversine_meters",
    "in_dangerous_neighborhood",
    "is_in_dangerous_neighborhood",
    "nearest_dangerous_boundary_distance_m",
    "nearest_dangerous_distance_m",
]
