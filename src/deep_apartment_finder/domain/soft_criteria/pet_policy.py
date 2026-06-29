"""`PetPolicyCriterion` — score based on the LLM-extracted pet policy.

Score semantics (in `domain/soft_criteria/__init__.py:normalize_value`):

    allowed       -> 1.0
    negotiated    -> 0.7
    unknown       -> 0.3
    not_allowed   -> 0.0

The mapping lives in the constructor's `_TABLE` so it's a one-line
change to add a value (e.g. `'deposit_required'`).
"""

from __future__ import annotations

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.soft_criteria import Score, normalize_value


class PetPolicyCriterion:
    """Score in [0, 1]; 1.0 = pets explicitly allowed."""

    name = "pet_policy"

    _TABLE: dict[str, float] = {
        "allowed": 1.0,
        "negotiated": 0.7,
        "unknown": 0.3,
        "not_allowed": 0.0,
    }

    def __init__(self, *, weight: float = 0.3) -> None:
        self._weight = float(weight)

    def score(self, apartment: Apartment) -> Score:
        value = normalize_value(apartment.pet_policy, self._TABLE.keys())
        return Score(
            score=self._TABLE[value],
            weight=self._weight,
            details={"value": value},
        )


__all__ = ["PetPolicyCriterion"]
