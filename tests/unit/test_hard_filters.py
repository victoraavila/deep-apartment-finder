"""Hard filter predicate tests."""

from __future__ import annotations

from decimal import Decimal

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.filters.hard import HardFilters
from deep_apartment_finder.domain.source import Source


def _apt(**kwargs):
    defaults = dict(
        source=Source.FOTOCASA,
        external_id="x",
        url="https://x",
        title="t",
        price_eur=Decimal("1000"),
        rooms=2,
        bathrooms=2,
        size_m2=Decimal("60"),
    )
    defaults.update(kwargs)
    return Apartment(**defaults)


def test_default_filters_pass_a_listing_that_meets_every_constraint():
    f = HardFilters()
    assert f.passes(_apt()) is True


def test_price_above_cap_is_rejected():
    f = HardFilters()
    assert f.passes(_apt(price_eur=Decimal("1300"))) is False


def test_size_below_minimum_is_rejected():
    f = HardFilters()
    assert f.passes(_apt(size_m2=Decimal("40"))) is False


def test_rooms_below_minimum_is_rejected():
    f = HardFilters()
    assert f.passes(_apt(rooms=1)) is False


def test_bathrooms_below_minimum_is_rejected():
    f = HardFilters()
    assert f.passes(_apt(bathrooms=1)) is False


def test_missing_room_count_treated_as_zero():
    f = HardFilters()
    assert f.passes(_apt(rooms=None)) is False


def test_missing_price_is_rejected_when_price_cap_is_active():
    f = HardFilters()
    assert f.passes(_apt(price_eur=None)) is False


def test_relaxing_a_filter_lets_a_listing_through():
    f = HardFilters(max_price_eur=None)
    assert f.passes(_apt(price_eur=Decimal("9999"))) is True
