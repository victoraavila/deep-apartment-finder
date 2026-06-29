"""Geo helper tests (haversine + dangerous-neighborhood predicates)."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from deep_apartment_finder.domain.geo import (
    DangerousNeighborhood,
    compute_dedup_key,
    haversine_meters,
    in_dangerous_neighborhood,
    is_in_dangerous_neighborhood,
    is_valid_coordinate,
    nearest_dangerous_boundary_distance_m,
    nearest_dangerous_distance_m,
)


def test_haversine_zero_for_same_point():
    assert haversine_meters(41.65, -0.88, 41.65, -0.88) == 0.0


def test_haversine_known_distance_madrid_to_barcelona():
    """Madrid -> Barcelona is ~505 km great-circle. Allow 1% slack."""
    d = haversine_meters(40.4168, -3.7038, 41.3851, 2.1734)
    assert 500_000 < d < 510_000


def test_haversine_is_symmetric():
    a = haversine_meters(41.65, -0.88, 41.66, -0.89)
    b = haversine_meters(41.66, -0.89, 41.65, -0.88)
    assert math.isclose(a, b, rel_tol=1e-9)


def test_in_dangerous_neighborhood_inside():
    n = DangerousNeighborhood(name="Delicias", center_lat=41.6517, center_lng=-0.9088, radius_m=600)
    # 50 m offset from center -> inside.
    assert in_dangerous_neighborhood(41.6521, -0.9088, n) is True


def test_in_dangerous_neighborhood_outside():
    n = DangerousNeighborhood(name="Delicias", center_lat=41.6517, center_lng=-0.9088, radius_m=600)
    # 5 km north -> far outside.
    assert in_dangerous_neighborhood(41.6967, -0.9088, n) is False


def test_in_dangerous_neighborhood_boundary_is_inside():
    n = DangerousNeighborhood(name="X", center_lat=0.0, center_lng=0.0, radius_m=1000)
    # At ~1.1 km, but choose a point such that the haversine distance
    # is exactly 1000 m by going ~0.00899 degrees north.
    # 1 degree latitude ~= 111_111 m -> 1000 m ~= 0.00899 deg.
    assert in_dangerous_neighborhood(0.00899, 0.0, n) is True
    assert in_dangerous_neighborhood(0.0091, 0.0, n) is False


def test_is_in_dangerous_neighborhood_alias_matches_spec_name():
    n = DangerousNeighborhood(name="X", center_lat=0.0, center_lng=0.0, radius_m=1000)
    assert is_in_dangerous_neighborhood(0.00899, 0.0, n) is True


def test_nearest_dangerous_distance_returns_minimum():
    a = DangerousNeighborhood(name="A", center_lat=41.65, center_lng=-0.88, radius_m=500)
    b = DangerousNeighborhood(name="B", center_lat=41.70, center_lng=-0.88, radius_m=500)
    # The point is closer to A.
    d = nearest_dangerous_distance_m(41.66, -0.88, [a, b])
    assert d is not None
    # A is 0.01 deg away -> ~1111 m; B is 0.04 deg away -> ~4444 m.
    # nearest is A, so d should be ~1111 m.
    assert 1000 < d < 1300


def test_nearest_dangerous_distance_returns_none_when_empty():
    assert nearest_dangerous_distance_m(41.65, -0.88, []) is None


def test_nearest_dangerous_boundary_distance_is_zero_inside_radius():
    n = DangerousNeighborhood(name="X", center_lat=0.0, center_lng=0.0, radius_m=1000)
    assert nearest_dangerous_boundary_distance_m(0.004, 0.0, [n]) == 0.0


def test_nearest_dangerous_boundary_distance_subtracts_radius():
    n = DangerousNeighborhood(name="X", center_lat=0.0, center_lng=0.0, radius_m=1000)
    d = nearest_dangerous_boundary_distance_m(0.018, 0.0, [n])
    assert d is not None
    assert 950 < d < 1050


def test_nearest_dangerous_boundary_distance_returns_none_when_empty():
    assert nearest_dangerous_boundary_distance_m(41.65, -0.88, []) is None


# --- Sprint 3: is_valid_coordinate -----------------------------------------


@pytest.mark.parametrize(
    ("lat", "lng"),
    [
        (None, -0.88),
        (41.65, None),
        (None, None),
    ],
)
def test_is_valid_coordinate_rejects_none(lat: object, lng: object) -> None:
    assert is_valid_coordinate(lat, lng) is False


def test_is_valid_coordinate_rejects_zero_zero() -> None:
    assert is_valid_coordinate(0, 0) is False
    assert is_valid_coordinate(0.0, 0.0) is False
    assert is_valid_coordinate(Decimal(0), Decimal(0)) is False


@pytest.mark.parametrize(
    ("lat", "lng"),
    [
        # Outside the latitude range.
        (41.4, -0.88),
        (41.9, -0.88),
        # Outside the longitude range.
        (41.65, -1.10),
        (41.65, -0.70),
        # Madrid, just to be paranoid.
        (40.4168, -3.7038),
        # Atlantic Ocean off Africa.
        (5.0, 0.0),
    ],
)
def test_is_valid_coordinate_rejects_outside_bounding_box(
    lat: float, lng: float
) -> None:
    assert is_valid_coordinate(lat, lng) is False


@pytest.mark.parametrize(
    ("lat", "lng"),
    [
        # Zaragoza centre.
        (41.6561, -0.8873),
        # Huesos (north) — within bbox.
        (41.66, -0.88),
        # Cadrete (south) — within bbox.
        (41.55, -0.95),
        # Outside-city, inside bbox.
        (41.7, -1.0),
    ],
)
def test_is_valid_coordinate_accepts_zaragoza_points(
    lat: float, lng: float
) -> None:
    assert is_valid_coordinate(lat, lng) is True


def test_is_valid_coordinate_accepts_decimal() -> None:
    assert is_valid_coordinate(Decimal("41.65"), Decimal("-0.88")) is True
    assert is_valid_coordinate(Decimal("0"), Decimal("0")) is False


def test_is_valid_coordinate_rejects_nan_and_inf() -> None:
    assert is_valid_coordinate(float("nan"), -0.88) is False
    assert is_valid_coordinate(41.65, float("inf")) is False
    assert is_valid_coordinate(float("-inf"), -0.88) is False


def test_is_valid_coordinate_rejects_unparseable_input() -> None:
    assert is_valid_coordinate("north", "south") is False  # type: ignore[arg-type]


# --- Sprint 3: compute_dedup_key -------------------------------------------


def test_compute_dedup_key_is_stable_across_minor_field_drift() -> None:
    """The same physical apartment on two portals, with small drift in
    size and price, must produce the same key."""
    a = compute_dedup_key(
        address="Calle Test 1, 50001 Zaragoza",
        rooms=2,
        size_m2=65.0,
        price_eur=950.0,
    )
    # 67.0 m² falls in the same 5-m² bucket as 65.0 (round(13.4) = 13).
    # 962.0 € falls in the same 25-€ bucket as 950.0 (round(38.48) = 38).
    b = compute_dedup_key(
        address="calle test 1, zaragoza",  # case + zip differs
        rooms=2,
        size_m2=67.0,
        price_eur=962.0,
    )
    assert a is not None
    assert a == b


def test_compute_dedup_key_is_stable_with_zipcode_present_or_absent() -> None:
    """Spanish portals disagree on whether the zipcode lives in the
    address. The key must collapse both shapes."""
    with_zip = compute_dedup_key(
        address="Calle de América 18, 50001 Zaragoza",
        rooms=3,
        size_m2=85.0,
        price_eur=1100.0,
    )
    without_zip = compute_dedup_key(
        address="Calle de América 18, Zaragoza",
        rooms=3,
        size_m2=85.0,
        price_eur=1100.0,
    )
    assert with_zip is not None
    assert with_zip == without_zip


def test_compute_dedup_key_differs_for_different_rooms() -> None:
    a = compute_dedup_key(
        address="Calle X, Zaragoza", rooms=2, size_m2=60.0, price_eur=900.0
    )
    b = compute_dedup_key(
        address="Calle X, Zaragoza", rooms=3, size_m2=60.0, price_eur=900.0
    )
    assert a is not None and b is not None
    assert a != b


def test_compute_dedup_key_differs_for_different_address() -> None:
    a = compute_dedup_key(
        address="Calle A, Zaragoza", rooms=2, size_m2=60.0, price_eur=900.0
    )
    b = compute_dedup_key(
        address="Calle B, Zaragoza", rooms=2, size_m2=60.0, price_eur=900.0
    )
    assert a is not None and b is not None
    assert a != b


@pytest.mark.parametrize(
    ("address", "rooms", "size_m2", "price_eur"),
    [
        (None, 2, 60.0, 900.0),  # no address
        ("", 2, 60.0, 900.0),  # empty address
        ("Calle X, Zaragoza", None, 60.0, 900.0),  # no rooms
        ("Calle X, Zaragoza", 2, None, 900.0),  # no size
        ("Calle X, Zaragoza", 2, 60.0, None),  # no price
    ],
)
def test_compute_dedup_key_returns_none_when_inputs_are_incomplete(
    address: str | None,
    rooms: int | None,
    size_m2: float | None,
    price_eur: float | None,
) -> None:
    assert compute_dedup_key(
        address=address,
        rooms=rooms,
        size_m2=size_m2,
        price_eur=price_eur,
    ) is None


def test_compute_dedup_key_accepts_decimals() -> None:
    a = compute_dedup_key(
        address="Calle X, Zaragoza",
        rooms=2,
        size_m2=Decimal("60.00"),
        price_eur=Decimal("900.00"),
    )
    b = compute_dedup_key(
        address="Calle X, Zaragoza",
        rooms=2,
        size_m2=60.0,
        price_eur=900.0,
    )
    assert a is not None
    assert a == b
