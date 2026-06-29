"""Default registry of `SoftCriterion`s.

This is the single place the ranker reads from. Adding a 4th criterion
is one new class + one line in `default_criteria()` (acceptance
criterion 8 of `SPRINT2.md`).

The weights and `max_distance_m` come from the ranker's `Settings`
fields, not from the criterion constructors — that way an operator
can rebalance scoring without code changes.
"""

from __future__ import annotations

from collections.abc import Iterable

from deep_apartment_finder.domain.geo import DangerousNeighborhood
from deep_apartment_finder.domain.soft_criteria import SoftCriterion
from deep_apartment_finder.domain.soft_criteria.distance_to_dangerous import (
    DistanceToDangerousCriterion,
)
from deep_apartment_finder.domain.soft_criteria.furnished import FurnishedCriterion
from deep_apartment_finder.domain.soft_criteria.pet_policy import PetPolicyCriterion


def default_criteria(
    *,
    neighborhoods: Iterable[DangerousNeighborhood] = (),
    weight_distance: float = 0.5,
    weight_pet_policy: float = 0.3,
    weight_furnished: float = 0.2,
    max_distance_m: float = 2000.0,
) -> list[SoftCriterion]:
    """Build the default list of soft criteria for the ranker.

    The order is stable: distance, pet policy, furnished. The ranker
    does not rely on order, but the email body lists criteria in the
    order they're registered.
    """
    return [
        DistanceToDangerousCriterion(
            neighborhoods=neighborhoods,
            max_distance_m=max_distance_m,
            weight=weight_distance,
        ),
        PetPolicyCriterion(weight=weight_pet_policy),
        FurnishedCriterion(weight=weight_furnished),
    ]


__all__ = ["default_criteria"]
