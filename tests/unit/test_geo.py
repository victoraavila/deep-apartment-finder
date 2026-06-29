"""Geo helper tests (haversine + dangerous-neighborhood predicates)."""

from __future__ import annotations

import math

from deep_apartment_finder.domain.geo import (
    DangerousNeighborhood,
    haversine_meters,
    in_dangerous_neighborhood,
    is_in_dangerous_neighborhood,
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
