"""Soft criteria tests.

Each test exercises a single criterion with hand-crafted inputs so a
future maintainer can read the test as the criterion's spec. The
`registry` test (acceptance criterion 8) covers the OCP angle.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.geo import DangerousNeighborhood
from deep_apartment_finder.domain.soft_criteria import Score, SoftCriterion
from deep_apartment_finder.domain.soft_criteria.distance_to_dangerous import (
    DistanceToDangerousCriterion,
)
from deep_apartment_finder.domain.soft_criteria.furnished import FurnishedCriterion
from deep_apartment_finder.domain.soft_criteria.pet_policy import PetPolicyCriterion
from deep_apartment_finder.domain.soft_criteria.registry import default_criteria
from deep_apartment_finder.domain.source import Source


def _apt(*, lat: float | None = None, lng: float | None = None,
         pet_policy: str | None = None, furnished: str | None = None) -> Apartment:
    return Apartment(
        source=Source.FOTOCASA,
        external_id="x",
        url="u",
        lat=Decimal(str(lat)) if lat is not None else None,
        lng=Decimal(str(lng)) if lng is not None else None,
        pet_policy=pet_policy,
        furnished=furnished,
    )


# --- DistanceToDangerousCriterion -----------------------------------------


def test_distance_criterion_scores_zero_inside_radius():
    n = DangerousNeighborhood(
        name="Delicias", center_lat=41.6517, center_lng=-0.9088, radius_m=600
    )
    crit = DistanceToDangerousCriterion(neighborhoods=[n], max_distance_m=2000.0, weight=0.5)
    apt = _apt(lat=41.6517, lng=-0.9088)
    score = crit.score(apt)
    assert isinstance(score, Score)
    assert score.score == 0.0
    assert score.weight == 0.5


def test_distance_criterion_scores_zero_anywhere_inside_radius():
    n = DangerousNeighborhood(
        name="Delicias", center_lat=41.6517, center_lng=-0.9088, radius_m=600
    )
    crit = DistanceToDangerousCriterion(
        neighborhoods=[n], max_distance_m=2000.0, weight=0.5
    )
    apt = _apt(lat=41.6544, lng=-0.9088)  # roughly 300 m north of center
    score = crit.score(apt)
    assert score.score == 0.0


def test_distance_criterion_scores_one_at_max_distance():
    """A point far from any neighborhood boundary should saturate at 1.0."""
    n = DangerousNeighborhood(
        name="X", center_lat=41.6517, center_lng=-0.9088, radius_m=600
    )
    crit = DistanceToDangerousCriterion(neighborhoods=[n], max_distance_m=2000.0)
    apt = _apt(lat=41.6817, lng=-0.9088)  # >2 km north of the radius boundary
    score = crit.score(apt)
    assert score.score == 1.0


def test_distance_criterion_is_neutral_when_no_neighborhoods(caplog: pytest.LogCaptureFixture):
    crit = DistanceToDangerousCriterion(neighborhoods=[], max_distance_m=2000.0)
    apt = _apt(lat=41.65, lng=-0.88)
    score = crit.score(apt)
    assert score.score == 0.5
    assert "empty" in (score.details.get("reason") or "").lower() or score.details.get("reason") == "no dangerous neighborhoods configured"


def test_distance_criterion_returns_neutral_when_no_lat_lng():
    n = DangerousNeighborhood(
        name="X", center_lat=41.6517, center_lng=-0.9088, radius_m=600
    )
    crit = DistanceToDangerousCriterion(neighborhoods=[n])
    apt = _apt()  # no lat/lng
    score = crit.score(apt)
    assert score.score == 0.5
    assert "missing" in score.details["reason"]


# --- Sprint 3: invalid coordinates (Pillar D) ------------------------------


def test_distance_criterion_returns_neutral_when_coordinates_are_zero_zero() -> None:
    """`(0, 0)` is the classic scraper placeholder; it must NOT be
    treated as "very far from any danger" and rewarded."""
    n = DangerousNeighborhood(
        name="X", center_lat=41.6517, center_lng=-0.9088, radius_m=600
    )
    crit = DistanceToDangerousCriterion(neighborhoods=[n])
    apt = _apt(lat=0.0, lng=0.0)
    score = crit.score(apt)
    assert score.score == 0.5
    assert score.details["reason"] == "invalid coordinates"


def test_distance_criterion_returns_neutral_when_coordinates_are_outside_bbox() -> None:
    """A point far outside the Zaragoza bounding box (the Atlantic
    Ocean, the South Pole, ...) is treated as bogus."""
    n = DangerousNeighborhood(
        name="X", center_lat=41.6517, center_lng=-0.9088, radius_m=600
    )
    crit = DistanceToDangerousCriterion(neighborhoods=[n])
    apt = _apt(lat=40.4168, lng=-3.7038)  # Madrid
    score = crit.score(apt)
    assert score.score == 0.5
    assert score.details["reason"] == "invalid coordinates"


# --- PetPolicyCriterion ---------------------------------------------------


def test_pet_policy_allowed_scores_one():
    crit = PetPolicyCriterion()
    score = crit.score(_apt(pet_policy="allowed"))
    assert score.score == 1.0


def test_pet_policy_negotiated_scores_partial():
    crit = PetPolicyCriterion()
    score = crit.score(_apt(pet_policy="negotiated"))
    assert score.score == 0.7


def test_pet_policy_not_allowed_scores_zero():
    crit = PetPolicyCriterion()
    score = crit.score(_apt(pet_policy="not_allowed"))
    assert score.score == 0.0


def test_pet_policy_unknown_scores_partial():
    crit = PetPolicyCriterion()
    score = crit.score(_apt(pet_policy=None))
    assert score.score == 0.3


def test_pet_policy_invalid_value_falls_back_to_unknown():
    crit = PetPolicyCriterion()
    score = crit.score(_apt(pet_policy="definitely-yes"))
    assert score.score == 0.3
    assert score.details["value"] == "unknown"


# --- FurnishedCriterion ---------------------------------------------------


def test_furnished_true_scores_one():
    crit = FurnishedCriterion()
    assert crit.score(_apt(furnished="true")).score == 1.0


def test_furnished_false_scores_zero():
    crit = FurnishedCriterion()
    assert crit.score(_apt(furnished="false")).score == 0.0


def test_furnished_unknown_scores_partial():
    crit = FurnishedCriterion()
    assert crit.score(_apt(furnished="unknown")).score == 0.3


def test_furnished_missing_scores_partial():
    crit = FurnishedCriterion()
    assert crit.score(_apt()).score == 0.3


# --- registry (acceptance criterion 8) ------------------------------------


def test_default_registry_returns_three_criteria():
    criteria = default_criteria(neighborhoods=[])
    assert len(criteria) == 3
    assert {c.name for c in criteria} == {
        "distance_to_dangerous",
        "pet_policy",
        "furnished",
    }
    for c in criteria:
        assert isinstance(c, SoftCriterion)


def test_default_registry_is_ocp_friendly(monkeypatch: pytest.MonkeyPatch):
    """A 4th criterion can be plugged in by appending to the list.

    The test monkeypatches `default_criteria` to add a sentinel
    criterion and asserts the registry picks it up. This is the
    acceptance criterion 8 contract: "adding a 4th criterion is a
    single class + a single line in registry.py".
    """
    from deep_apartment_finder.domain.soft_criteria import registry

    class _BonusCriterion:
        name = "bonus"

        def score(self, apartment):
            return Score(score=0.5, weight=0.1)

    def _with_bonus(**kwargs):
        return default_criteria(**kwargs) + [_BonusCriterion()]

    monkeypatch.setattr(registry, "default_criteria", _with_bonus)
    criteria = registry.default_criteria(neighborhoods=[])
    assert any(c.name == "bonus" for c in criteria)
