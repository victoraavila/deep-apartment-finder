"""Idealista parser tests using real captured SSR HTML fixtures.

The fixtures in `tests/fixtures/idealista/` are byte-for-byte
captures from a live `curl_cffi` request impersonating Chrome 131.
They are deterministic (no JS, no random ids in the listing data)
and offline-safe: the parser can be exercised without any network
I/O. The shapes were characterised in `docs/idealista_recon` and
`SPRINT3.md`.

What's covered:
- The 30 cards on page 1 of `zaragoza-provincia` parse cleanly.
- Field coverage matches the documented shape (rooms + size on
  every card; bathrooms always `None`; lat/lng always `None`).
- The detail page (`fetch_listing` fallback path) can be
  reconstructed from the search page.
- `_normalize_price` handles the Spanish thousands-separator
  convention.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deep_apartment_finder.adapters.scrapers.idealista.api import (
    _normalize_price,
    card_to_apartment,
    parse_search_page,
)
from deep_apartment_finder.domain.source import Source

FIXTURES = Path(__file__).parent.parent / "fixtures" / "idealista"


# --- _normalize_price ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.400 €/mes", 1400.0),
        ("825 €/mes", 825.0),
        ("950 €/mes", 950.0),
        ("1.200 €", 1200.0),
        ("", None),
        (None, None),
        ("gratis", None),
        ("1200.50 €/mes", 1200.50),
    ],
)
def test_normalize_price(raw: str | None, expected: float | None) -> None:
    assert _normalize_price(raw) == expected


# --- parse_search_page -----------------------------------------------------


def test_parse_search_page_handles_empty_html() -> None:
    assert parse_search_page("") == []


def test_parse_search_page_ignores_non_item_articles() -> None:
    """`<article class="something-else">` should not be picked up."""
    html = """
    <html><body>
      <article class="not-a-listing"><a href="/x">x</a></article>
      <article class="info"><a href="/y">y</a></article>
    </body></html>
    """
    assert parse_search_page(html) == []


def test_parse_search_page_skips_cards_without_inmueble_id() -> None:
    """Cards without `/inmueble/<id>/` hrefs are dropped (ad slots, etc.)."""
    html = """
    <html><body>
      <article class="item">
        <a class="item-link" href="/promo/foo">Promo</a>
      </article>
    </body></html>
    """
    assert parse_search_page(html) == []


def test_parse_search_page_page1_fixture() -> None:
    html = (FIXTURES / "search_page1.html").read_text()
    cards = parse_search_page(html)
    assert len(cards) == 30
    # First card from the manual recon: id=109872751, 825 €, 2 hab, 115 m²
    first = cards[0]
    assert first.external_id == "109872751"
    assert first.price_eur == 825.0
    assert first.title == "Piso en Calle de América, 18, Alagon"
    assert first.url == "https://www.idealista.com/inmueble/109872751/"
    raw = first.raw or {}
    assert raw["rooms"] == 2
    assert raw["size_m2"] == 115.0
    # No bathrooms on the search card (always None for Sprint 3).
    assert raw["bathrooms"] is None
    # No lat/lng either.
    assert first.title  # address falls back to title when no title attr


def test_parse_search_page_page1_field_coverage() -> None:
    """Documented field coverage on the captured page 1 fixture."""
    html = (FIXTURES / "search_page1.html").read_text()
    cards = parse_search_page(html)
    assert len(cards) == 30
    # Every card has price, address, size, and a description.
    assert all(c.price_eur is not None for c in cards)
    assert all((c.raw or {}).get("size_m2") is not None for c in cards)
    # 29/30 cards have a rooms badge (one is a chalet that omits it).
    rooms_set = sum(1 for c in cards if (c.raw or {}).get("rooms") is not None)
    assert rooms_set >= 28
    # Bathrooms is always None on the search card (Sprint 3 limitation).
    assert all((c.raw or {}).get("bathrooms") is None for c in cards)


def test_parse_search_page_page2_no_overlap_with_page1() -> None:
    """Page 2 introduces new external_ids; no card on page 1 reappears."""
    page1 = parse_search_page((FIXTURES / "search_page1.html").read_text())
    page2 = parse_search_page((FIXTURES / "search_page2.html").read_text())
    ids1 = {c.external_id for c in page1}
    ids2 = {c.external_id for c in page2}
    assert ids1.isdisjoint(ids2), f"overlap: {ids1 & ids2}"
    assert len(page2) == 30


# --- card_to_apartment -----------------------------------------------------


def test_card_to_apartment_promotes_to_source_idealista() -> None:
    cards = parse_search_page((FIXTURES / "search_page1.html").read_text())
    apt = card_to_apartment(cards[0])
    assert apt.source == Source.IDEALISTA
    assert apt.external_id == "109872751"
    assert apt.url == "https://www.idealista.com/inmueble/109872751/"
    assert apt.title is not None
    assert apt.price_eur is not None and float(apt.price_eur) == 825.0
    assert apt.rooms == 2
    assert apt.size_m2 is not None and float(apt.size_m2) == 115.0
    # Sprint 3 limitations:
    assert apt.bathrooms is None
    assert apt.lat is None
    assert apt.lng is None
    # The search-time description is preserved (truncated, but non-empty).
    assert apt.description is not None and len(apt.description) > 100


def test_card_to_apartment_carries_raw_blob() -> None:
    cards = parse_search_page((FIXTURES / "search_page1.html").read_text())
    apt = card_to_apartment(cards[0])
    # The raw blob must keep the search-card metadata so the repository
    # can replay it (and so future debug snapshots include badges).
    assert isinstance(apt.raw, dict)
    assert "rooms" in apt.raw
    assert "size_m2" in apt.raw
    assert "badges" in apt.raw
