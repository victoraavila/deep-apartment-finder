"""`DistanceToDangerousCriterion` — penalizes apartments close to a
neighborhood in `dangerous_neighborhoods`.

Score semantics:
- inside any neighborhood radius: 0.0
- 2 km or more from every center (default): 1.0
- linear interpolation in between

The maximum distance is configurable per-criterion; the ranker passes
it when constructing the criterion. If the dangerous-neighborhoods
table is empty, the criterion returns a neutral 0.5 (and the ranker
logs a warning).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.geo import (
    DangerousNeighborhood,
    nearest_dangerous_boundary_distance_m,
)
from deep_apartment_finder.domain.soft_criteria import Score

logger = logging.getLogger(__name__)


class DistanceToDangerousCriterion:
    """Score in [0, 1]; 1.0 = far from any dangerous neighborhood.

    Args:
        neighborhoods: snapshot of `dangerous_neighborhoods` taken at
            rank time. Treated as immutable for the rank.
        max_distance_m: the distance at which the score saturates at
            1.0. Default 2000m; tune in `config.py`.
        weight: ranker-supplied weight; kept on the `Score` so the
            ranker can use it without re-reading settings.
    """

    name = "distance_to_dangerous"

    def __init__(
        self,
        *,
        neighborhoods: Iterable[DangerousNeighborhood],
        max_distance_m: float = 2000.0,
        weight: float = 0.5,
    ) -> None:
        self._neighborhoods = list(neighborhoods)
        self._max_distance_m = float(max_distance_m)
        self._weight = float(weight)
        if not self._neighborhoods:
            logger.warning(
                "DistanceToDangerousCriterion: dangerous_neighborhoods is empty; "
                "every apartment will score a neutral 0.5"
            )

    def score(self, apartment: Apartment) -> Score:
        if apartment.lat is None or apartment.lng is None:
            return Score(
                score=0.5,
                weight=self._weight,
                details={"reason": "missing lat/lng"},
            )
        if not self._neighborhoods:
            return Score(
                score=0.5,
                weight=self._weight,
                details={"reason": "no dangerous neighborhoods configured"},
            )
        nearest = nearest_dangerous_boundary_distance_m(
            float(apartment.lat), float(apartment.lng), self._neighborhoods
        )
        assert nearest is not None  # guarded by the check above
        if nearest >= self._max_distance_m:
            s = 1.0
        else:
            # Linear ramp: 0m -> 0.0, max_distance_m -> 1.0
            s = max(0.0, min(1.0, nearest / self._max_distance_m))
        return Score(
            score=s,
            weight=self._weight,
            details={
                "nearest_boundary_m": int(nearest),
                "max_distance_m": int(self._max_distance_m),
            },
        )


__all__ = ["DistanceToDangerousCriterion"]
