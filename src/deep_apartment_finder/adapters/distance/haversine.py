"""Haversine `DistanceProvider` — pure function, no I/O.

The implementation delegates to `domain.geo.haversine_meters`. The
port is async because the future OSRM provider will be I/O; this one
is async too so the ranker is identical in both cases.
"""

from __future__ import annotations

from deep_apartment_finder.domain.geo import haversine_meters
from deep_apartment_finder.ports.distance_provider import DistanceProvider


class HaversineDistanceProvider(DistanceProvider):
    async def meters_between(
        self, lat1: float, lng1: float, lat2: float, lng2: float
    ) -> float:
        return haversine_meters(lat1, lng1, lat2, lng2)


__all__ = ["HaversineDistanceProvider"]
