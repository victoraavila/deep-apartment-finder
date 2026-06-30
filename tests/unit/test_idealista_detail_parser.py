"""`parse_detail_page` tests for the Sprint 4 Idealista detail upgrade.

The Idealista detail page carries a stable, machine-readable
`<div class="details-property_features"><ul>...</ul></div>` block
with the bathroom count, rooms, and m². The page also carries a
long-form `<div class="details-property_description">` block with
the apartment's full description (much longer than the search
card's truncated `<p class="ellipsis">`).

Sprint 4 closed the Sprint 3 gap: `bathrooms` is no longer
structurally absent on every Idealista row.

What's covered:
- The captured detail-page fixture (`detail_page1.html`) parses
  cleanly: 70 m², 2 habitaciones, 1 baño.
- Singular vs plural Spanish bathroom forms (`1 baño`, `2 baños`).
- The Spanish decimal-comma `70,5 m²` value.
- A page with no `details-property_features` block (the
  "soft 404" / delisted case) returns a partial dict; the
  caller decides what to do with the missing fields.
- A page with no description block returns `description=None`;
  the caller falls back to the search-card description.
"""

from __future__ import annotations

from pathlib import Path

from deep_apartment_finder.adapters.scrapers.idealista.api import (
    apply_detail_enrichment,
    parse_detail_page,
)
from deep_apartment_finder.ports.scraper import ListingCard

FIXTURES = Path(__file__).parent.parent / "fixtures" / "idealista"


# --- parse_detail_page ----------------------------------------------------


def test_parse_detail_page_fixture_extracts_rooms_bathrooms_size() -> None:
    """The captured detail-page HTML carries the canonical block
    `<div class="details-property_features"><ul>...</ul></div>` with
    70 m², 2 habitaciones, 1 baño. The parser must surface all three
    fields and the long-form description.
    """
    html = (FIXTURES / "detail_page1.html").read_text()
    parsed = parse_detail_page(html, url="https://www.idealista.com/inmueble/12345/")
    assert parsed["bathrooms"] == 1
    assert parsed["rooms"] == 2
    assert parsed["size_m2"] == 70.0
    assert parsed["description"] is not None
    assert "Piso luminoso" in parsed["description"]


def test_parse_detail_page_handles_plural_banos() -> None:
    """`2 baños` (plural) parses as `2` — the regex tolerates both
    forms. `1 baño` (singular) parses as `1`. The detail block in
    real-world listings mixes singular and plural.
    """
    html = """
    <div class="details-property_features">
      <ul>
        <li>120 m² construidos</li>
        <li>3 habitaciones</li>
        <li>2 baños</li>
      </ul>
    </div>
    """
    parsed = parse_detail_page(html, url="x")
    assert parsed["bathrooms"] == 2
    assert parsed["rooms"] == 3
    assert parsed["size_m2"] == 120.0


def test_parse_detail_page_handles_singular_bano() -> None:
    html = """
    <div class="details-property_features">
      <ul>
        <li>50 m²</li>
        <li>1 hab.</li>
        <li>1 baño</li>
      </ul>
    </div>
    """
    parsed = parse_detail_page(html, url="x")
    assert parsed["bathrooms"] == 1


def test_parse_detail_page_handles_spanish_decimal_comma_size() -> None:
    """Spanish `70,5 m²` parses to `70.5` (decimal-comma). The size
    field is the one the ranker uses for the size-related soft
    criterion; an un-parsed `70,5` would round to 70 and silently
    shift the size bucket the ranker matches against.
    """
    html = """
    <div class="details-property_features">
      <ul>
        <li>70,5 m² construidos</li>
        <li>2 habitaciones</li>
        <li>1 baño</li>
      </ul>
    </div>
    """
    parsed = parse_detail_page(html, url="x")
    assert parsed["size_m2"] == 70.5


def test_parse_detail_page_handles_missing_block() -> None:
    """A delisted listing's URL still resolves to a 200 with a body
    that lacks the features block. The parser must return a
    well-formed dict with `None` for the missing fields rather
    than raise. The caller (the scraper) falls back to the
    search-card values for any `None`.
    """
    html = "<html><body><p>This listing has been removed.</p></body></html>"
    parsed = parse_detail_page(html, url="x")
    assert parsed == {
        "bathrooms": None,
        "rooms": None,
        "size_m2": None,
        "description": None,
    }


def test_parse_detail_page_handles_malformed_html() -> None:
    """A page that is mostly empty / a DataDome interstitial must
    not raise; the parser returns a partial dict the caller can
    surface to the operator.
    """
    parsed = parse_detail_page("", url="x")
    assert parsed["bathrooms"] is None
    assert parsed["rooms"] is None
    assert parsed["size_m2"] is None


def test_parse_detail_page_handles_missing_description() -> None:
    """A page with only the features block (no description) returns
    a populated features dict and a `None` description. The caller
    falls back to the search-card description.
    """
    html = """
    <div class="details-property_features">
      <ul>
        <li>70 m² construidos</li>
        <li>2 hab.</li>
        <li>1 baño</li>
      </ul>
    </div>
    """
    parsed = parse_detail_page(html, url="x")
    assert parsed["bathrooms"] == 1
    assert parsed["rooms"] == 2
    assert parsed["size_m2"] == 70.0
    assert parsed["description"] is None


def test_parse_detail_page_tolerates_partial_block() -> None:
    """A page where only the bathroom badge is in the features
    block: the parser fills the matching field and leaves the
    others `None`. The scraper then falls back to the search card
    for the missing rooms / size.
    """
    html = """
    <div class="details-property_features">
      <ul>
        <li>1 baño</li>
      </ul>
    </div>
    """
    parsed = parse_detail_page(html, url="x")
    assert parsed["bathrooms"] == 1
    assert parsed["rooms"] is None
    assert parsed["size_m2"] is None


# --- apply_detail_enrichment ----------------------------------------------


def _card(
    *,
    external_id: str = "111886330",
    address: str = "Piso en Calle Test 1, Alagon",
    rooms: int | None = 2,
    size_m2: float | None = 70.0,
    description: str = "search card description (short)",
) -> ListingCard:
    return ListingCard(
        external_id=external_id,
        url=f"https://www.idealista.com/inmueble/{external_id}/",
        title="Piso en Calle Test 1, Alagon",
        price_eur=825.0,
        raw={
            "address": address,
            "rooms": rooms,
            "size_m2": size_m2,
            "description": description,
        },
    )


def test_apply_detail_enrichment_uses_detail_fields_when_present() -> None:
    """When the detail block is present, the apartment carries the
    detail's `bathrooms`, the corroborating `rooms` and `size_m2`
    if the detail has them, and the long-form `description`.
    """
    apt = apply_detail_enrichment(
        _card(),
        {
            "bathrooms": 2,
            "rooms": 2,
            "size_m2": 71.0,
            "description": "long detail description",
        },
    )
    assert apt.bathrooms == 2
    assert apt.rooms == 2
    assert float(apt.size_m2) == 71.0
    assert apt.description == "long detail description"


def test_apply_detail_enrichment_falls_back_to_card_on_partial_detail() -> None:
    """When the detail block is partial (e.g. no rooms / size
    because the page omitted them), the apartment falls back to
    the card's values. The caller still wants the bathroom
    enrichment, which the detail page uniquely carries.
    """
    apt = apply_detail_enrichment(
        _card(rooms=2, size_m2=70.0),
        {
            "bathrooms": 1,
            "rooms": None,
            "size_m2": None,
            "description": "long detail description",
        },
    )
    assert apt.bathrooms == 1
    # Falls back to the card for the missing fields.
    assert apt.rooms == 2
    assert float(apt.size_m2) == 70.0
    assert apt.description == "long detail description"


def test_apply_detail_enrichment_falls_back_to_card_on_empty_detail() -> None:
    """When the detail block is empty (the soft-404 / delisted
    case), the apartment carries the card's values verbatim —
    this is the scraper's `details_failed` path.
    """
    apt = apply_detail_enrichment(
        _card(),
        {
            "bathrooms": None,
            "rooms": None,
            "size_m2": None,
            "description": None,
        },
    )
    assert apt.bathrooms is None
    assert apt.rooms == 2
    assert float(apt.size_m2) == 70.0
    assert apt.description == "search card description (short)"
