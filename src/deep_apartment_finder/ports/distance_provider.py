"""Distance-provider port.

Sprint 2 ships `HaversineDistanceProvider` as the only implementation.
A future sprint can add `OsrmDistanceProvider` (route-based) behind
the same port without touching the ranker or the soft criterion.

The port is intentionally tiny: the only thing the ranker needs from
it is the meters-based distance between two WGS-84 points. The
`domain.geo` module is the in-process reference implementation; this
port exists to make the *replacement* (OSRM) a small, single-file
addition.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class DistanceProvider(Protocol):
    async def meters_between(self, lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        """Return the distance in meters between two WGS-84 points."""
        ...


__all__ = ["DistanceProvider"]
