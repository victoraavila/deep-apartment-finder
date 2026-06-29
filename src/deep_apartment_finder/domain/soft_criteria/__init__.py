"""Soft-criteria abstractions.

A `SoftCriterion` is a deterministic function from an `Apartment` (and
some context — e.g. the list of dangerous neighborhoods) to a `Score`
in `[0, 1]` where 1.0 is "best" and 0.0 is "worst". The ranker
combines them with weights into a final score.

Why a Protocol and not a plain class: the ranker registry can accept
both module-level implementations and test fakes; and the `details`
field carries whatever the criterion needs to surface in the email
body and the `apartment_scores` table (e.g. the distance to the
nearest dangerous neighborhood).

Adding a 4th criterion is a single new class + a one-line addition
in `registry.py` (acceptance criterion 8).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from deep_apartment_finder.domain.apartment import Apartment


@dataclass(frozen=True, slots=True)
class Score:
    """A single criterion's output for a single apartment.

    `score` is in [0, 1]. `weight` is the ranker-supplied weight for
    this criterion. `details` is arbitrary JSON-serializable debug
    info that ends up in the email body and the
    `apartment_scores.details` jsonb column.
    """

    score: float
    weight: float
    details: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SoftCriterion(Protocol):
    """A pluggable soft-criterion.

    The ranker instantiates the criterion with whatever context it
    needs (e.g. the list of dangerous neighborhoods), then calls
    `score(apartment)` for each apartment in the rank batch.
    """

    name: str

    def score(self, apartment: Apartment) -> Score:
        """Return this criterion's [0, 1] score for `apartment`."""
        ...


def normalize_value(value: Any, allowed: Iterable[str]) -> str:
    """Coerce a value from a DB column to a known enum string.

    Returns one of `allowed`, or `'unknown'` if the value is missing
    or not in `allowed`. Lower-cases and strips whitespace so the
    scraper subagent's LLM-extracted values land on a canonical form.
    """
    if value is None:
        return "unknown"
    s = str(value).strip().lower()
    if s in allowed:
        return s
    return "unknown"


def to_decimal(value: Any) -> Decimal | None:
    """Best-effort Decimal coercion for numeric fields."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


__all__ = ["Score", "SoftCriterion", "normalize_value", "to_decimal"]
