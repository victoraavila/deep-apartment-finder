"""Apartment value object tests."""

from __future__ import annotations

from decimal import Decimal

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.source import Source


def test_from_raw_dict_tolerates_missing_fields():
    apt = Apartment.from_raw_dict(
        Source.FOTOCASA,
        "42",
        "https://fotocasa.es/42",
        {"title": "Nice flat", "rooms": "2"},  # price etc missing
    )
    assert apt.title == "Nice flat"
    assert apt.rooms == 2
    assert apt.price_eur is None
    assert apt.size_m2 is None


def test_from_raw_dict_coerces_strings_to_numbers():
    apt = Apartment.from_raw_dict(
        Source.FOTOCASA,
        "1",
        "https://x",
        {"price_eur": "1.200,50", "size_m2": "62", "rooms": "3"},
    )
    # Decimal('1.200,50') is invalid (comma) -> None. The int() path works.
    assert apt.price_eur is None
    assert apt.size_m2 == Decimal("62")
    assert apt.rooms == 3


def test_to_ingest_dict_uses_db_column_names():
    apt = Apartment(source=Source.FOTOCASA, external_id="1", url="https://x")
    d = apt.to_ingest_dict()
    assert d["source"] == "fotocasa"
    assert d["external_id"] == "1"
    assert "raw_json" in d
    assert "scraped_at" in d


def test_value_object_is_frozen():
    apt = Apartment(source=Source.FOTOCASA, external_id="1", url="https://x")
    try:
        apt.title = "no"  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001
        assert "frozen" in str(exc).lower() or "FrozenInstanceError" in type(exc).__name__
    else:
        raise AssertionError("Apartment should be frozen")
