"""Fotocasa parser tests using fixture HTML.

We don't need a real network connection to test parsing — just
hand-crafted HTML/JSON-LD blobs that exercise each code path.
"""

from __future__ import annotations

import json

import pytest

from deep_apartment_finder.adapters.scrapers.fotocasa.listing_parser import (
    _normalize_price,
    parse_detail_page,
    parse_search_page,
)

# --- _normalize_price -----------------------------------------------------


def test_normalize_price_handles_thousands_separator():
    assert _normalize_price("1.200 €") == 1200.0
    assert _normalize_price("1.200,50 €") == 1200.50
    assert _normalize_price("950") == 950.0
    assert _normalize_price("") is None
    assert _normalize_price(None) is None
    assert _normalize_price("gratis") is None


# --- parse_search_page ----------------------------------------------------


def test_parse_search_page_prefers_jsonld():
    html = """
    <html><head>
      <script type="application/ld+json">
      [
        {"@type": "Apartment", "url": "https://fotocasa.es/vivienda/abc", "name": "Flat A", "offers": {"price": 950}},
        {"@type": "Apartment", "url": "https://fotocasa.es/vivienda/def", "name": "Flat B", "offers": {"price": 1100}}
      ]
      </script>
    </head><body></body></html>
    """
    cards = parse_search_page(html)
    assert [c.external_id for c in cards] == ["abc", "def"]
    assert cards[0].title == "Flat A"
    assert cards[0].price_eur == 950.0


def test_parse_search_page_falls_back_to_css_when_no_jsonld():
    html = """
    <html><body>
      <article>
        <a href="/vivienda/xyz">link</a>
        <h2>Nice flat</h2>
        <span class="re-SearchResult-price">900 €/mes</span>
      </article>
      <article>
        <a href="/inmueble/123">link</a>
        <h2>Cosy flat</h2>
        <span class="re-SearchResult-price">1.050 €/mes</span>
      </article>
    </body></html>
    """
    cards = parse_search_page(html)
    assert [c.external_id for c in cards] == ["xyz", "123"]
    assert cards[1].title == "Cosy flat"
    assert cards[1].price_eur == 1050.0


def test_parse_search_page_ignores_non_residence_jsonld():
    html = """
    <html><head>
      <script type="application/ld+json">
      {"@type": "WebSite", "name": "fotocasa"}
      </script>
    </head><body></body></html>
    """
    assert parse_search_page(html) == []


def test_parse_search_page_handles_empty_html():
    assert parse_search_page("") == []


def test_parse_search_page_collapses_duplicates_in_jsonld():
    html = """
    <html><head>
      <script type="application/ld+json">
      {"@type": "Apartment", "url": "https://x/vivienda/dup", "name": "A"}
      </script>
      <script type="application/ld+json">
      {"@type": "Apartment", "url": "https://x/vivienda/dup", "name": "A2"}
      </script>
    </head><body></body></html>
    """
    cards = parse_search_page(html)
    assert len(cards) == 1


# --- parse_detail_page ----------------------------------------------------


def test_parse_detail_page_from_jsonld_full():
    payload = {
        "@type": "Apartment",
        "url": "https://fotocasa.es/vivienda/abc",
        "name": "Piso en Delicias, 3 hab, 2 baños, 80 m²",
        "description": "Bonito piso reformado.",
        "address": {
            "streetAddress": "Calle Test 1",
            "addressLocality": "Zaragoza",
            "postalCode": "50001",
        },
        "geo": {"latitude": 41.6488, "longitude": -0.8891},
        "offers": {"price": 1100},
    }
    html = (
        "<html><head>"
        '<script type="application/ld+json">'
        + json.dumps(payload)
        + "</script>"
        + "</head><body></body></html>"
    )
    apt = parse_detail_page(html, url=payload["url"], external_id="abc")
    assert apt is not None
    assert apt.title == "Piso en Delicias, 3 hab, 2 baños, 80 m²"
    assert apt.price_eur is not None and float(apt.price_eur) == 1100.0
    assert apt.rooms == 3
    assert apt.bathrooms == 2
    assert apt.size_m2 is not None and float(apt.size_m2) == 80.0
    assert apt.address is not None and "Calle Test 1" in apt.address
    assert apt.lat is not None and float(apt.lat) == pytest.approx(41.6488)


def test_parse_detail_page_from_next_data():
    payload = {
        "props": {
            "pageProps": {
                "listing": {
                    "@type": "Apartment",
                    "url": "https://x/vivienda/zzz",
                    "name": "Flat Z",
                    "offers": {"price": 800},
                }
            }
        }
    }
    html = (
        "<html><head>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
        + "</head><body></body></html>"
    )
    apt = parse_detail_page(html, url="https://x/vivienda/zzz", external_id="zzz")
    assert apt is not None
    assert apt.title == "Flat Z"


def test_parse_detail_page_falls_back_to_css():
    html = """
    <html><body>
      <h1>Beautiful flat in Zaragoza, 3 hab, 72 m²</h1>
      <div data-testid="detail-price">1.100 €/mes</div>
      <div data-testid="detail-address">Calle Test 5, Zaragoza</div>
      <div data-testid="detail-description">A spacious flat with 2 baños.</div>
    </body></html>
    """
    apt = parse_detail_page(html, url="https://x/vivienda/css", external_id="css")
    assert apt is not None
    assert apt.title == "Beautiful flat in Zaragoza, 3 hab, 72 m²"
    assert apt.price_eur is not None and float(apt.price_eur) == 1100.0
    assert apt.rooms == 3
    assert apt.bathrooms == 2
    assert apt.size_m2 is not None and float(apt.size_m2) == 72.0


def test_parse_detail_page_returns_none_for_empty_page():
    assert parse_detail_page("<html><body></body></html>", url="x", external_id="x") is None
