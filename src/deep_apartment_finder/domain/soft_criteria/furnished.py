"""`FurnishedCriterion` — score based on the LLM-extracted "furnished" flag.

Score semantics:

    true     -> 1.0
    false    -> 0.0
    unknown  -> 0.3

The flag is extracted by the scraper subagent at ingest time and
persisted in the `apartments.furnished` column. The ranker reads it
deterministically — no LLM at rank time.
"""

from __future__ import annotations

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.soft_criteria import Score, normalize_value


class FurnishedCriterion:
    """Score in [0, 1]; 1.0 = furnished."""

    name = "furnished"

    _TABLE: dict[str, float] = {
        "true": 1.0,
        "false": 0.0,
        "unknown": 0.3,
    }

    def __init__(self, *, weight: float = 0.2) -> None:
        self._weight = float(weight)

    def score(self, apartment: Apartment) -> Score:
        value = normalize_value(apartment.furnished, self._TABLE.keys())
        return Score(
            score=self._TABLE[value],
            weight=self._weight,
            details={"value": value},
        )


__all__ = ["FurnishedCriterion"]
