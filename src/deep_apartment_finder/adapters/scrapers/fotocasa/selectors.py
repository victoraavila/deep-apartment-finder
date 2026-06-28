"""Fotocasa search API constants and request body builder.

Background
----------
The Fotocasa search portal is a single-page app; the public HTML page
(`/es/alquiler/viviendas/...`) is a CSR shell that 404s when fetched
without a real browser session. The actual data lives in a JSON API:

    POST https://web.gw.fotocasa.es/v1/search/ads
    POST https://search.gw.fotocasa.es/v2/suggest         (autocomplete)
    GET  https://web.gw.fotocasa.es/v2/propertysearch/urllocationsegments

This module is the only place in the project that needs to know those
endpoint shapes, the enum values for transaction/property type, and the
`combinedLocationIds` tuples for the cities we support.

If the live site changes the API contract, update the constants and
`build_search_request_body` here — no other file should need to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from deep_apartment_finder.domain.filters.hard import HardFilters

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

FOTOCASA_SEARCH_URL = "https://web.gw.fotocasa.es/v1/search/ads"
FOTOCASA_SUGGEST_URL = "https://search.gw.fotocasa.es/v2/suggest"
FOTOCASA_PAGE_BASE = "https://www.fotocasa.es"

# Base headers required for the gateway not to 403 us. Browser-like UA
# and Origin/Referer pointing at the public site. Set by the scraper.
FOTOCASA_REQUEST_HEADERS: dict[str, str] = {
    "Origin": FOTOCASA_PAGE_BASE,
    "Referer": f"{FOTOCASA_PAGE_BASE}/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
}

# ---------------------------------------------------------------------------
# Enum mappings (Fotocasa internal ids -> our domain)
# ---------------------------------------------------------------------------

# `transactionType` (string) in API; we also support the integer form.
# 1 = rent? sale? 2 = sale? 3 = rent (observed in HAR). The string form
# is unambiguous; the int form is the historical id we expose to the
# caller via `HardFilters`.
TRANSACTION_TYPE_RENT = 3
TRANSACTION_TYPE_SALE = 2

# `propertyType` (string). 2 = home / vivienda (the only one we need
# for Sprint 1; the next sprint will widen the filter).
PROPERTY_TYPE_HOME = 2

# Page size. The API caps at 30; we use 30 because that matches the
# official UI's per-page count.
DEFAULT_PAGE_SIZE = 30

# ---------------------------------------------------------------------------
# Location table
# ---------------------------------------------------------------------------
#
# The search endpoint requires a `combinedLocations` array of strings of
# the form "country,level1,level2,level3,level4,level5,level6,level7,level8"
# plus the lat/lng of the centroid of the search. We could resolve these
# on the fly from the autocomplete API, but the live autocomplete is
# protected by Imperva and the few cities we care about for Sprint 1 are
# stable, so we keep a hardcoded table. To add a city, drop another row
# here; the resolver will fall back to the suggest API if a city isn't
# in the table (best-effort, may 403).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocationSpec:
    """Resolved location parameters for the search API."""

    slug: str  # human slug, e.g. "zaragoza-capital"
    combined_location_ids: str  # the 9-level tuple as a string
    latitude: float
    longitude: float
    property_subtype_ids: tuple[int, ...] = ()  # 1,2,3,5,6,7,8,9,52,54 = viviendas


# Order matters: the resolver matches the city case-insensitively, so
# more-specific slugs (e.g. "zaragoza-capital") are matched first.
_LOCATION_TABLE: dict[str, LocationSpec] = {
    "zaragoza-capital": LocationSpec(
        slug="zaragoza-capital",
        combined_location_ids="724,2,50,208,300,50297,0,0,0",
        latitude=41.6566,
        longitude=-0.8773,
        property_subtype_ids=(1, 2, 3, 5, 6, 7, 8, 9, 52, 54),
    ),
    "zaragoza": LocationSpec(
        slug="zaragoza-capital",
        combined_location_ids="724,2,50,208,300,50297,0,0,0",
        latitude=41.6566,
        longitude=-0.8773,
        property_subtype_ids=(1, 2, 3, 5, 6, 7, 8, 9, 52, 54),
    ),
    "zaragoza-provincia": LocationSpec(
        slug="zaragoza-provincia",
        combined_location_ids="724,2,50,0,0,0,0,0,0",
        latitude=41.657,
        longitude=-0.879672,
        property_subtype_ids=(1, 2, 3, 5, 6, 7, 8, 9, 52, 54),
    ),
}


def resolve_location(city: str) -> LocationSpec:
    """Resolve a free-form city name (e.g. ``"Zaragoza"``) to a `LocationSpec`.

    The matching is case-insensitive, slug-friendly (``"Zaragoza Capital"``
    and ``"zaragoza-capital"`` both work). Raises `KeyError` if the city
    is not in the table; the caller is expected to have a curated city
    list and the tests pin this behaviour.
    """
    if not city:
        raise KeyError("empty city")
    key = city.strip().lower().replace(" ", "-")
    if key in _LOCATION_TABLE:
        return _LOCATION_TABLE[key]
    # Try a stripped (no accents) fuzzy match by attempting asciifying.
    import unicodedata

    def _strip_accents(s: str) -> str:
        return "".join(
            c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
        )

    key = _strip_accents(key)
    for k, v in _LOCATION_TABLE.items():
        if _strip_accents(k) == key:
            return v
    raise KeyError(f"unknown city: {city!r}")


# ---------------------------------------------------------------------------
# Request body builder
# ---------------------------------------------------------------------------


def build_search_request_body(
    filters: HardFilters,
    *,
    page_number: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    location: LocationSpec | None = None,
    sort_by_price: bool = False,
) -> dict[str, Any]:
    """Build the JSON body for ``POST /v1/search/ads``.

    The Fotocasa gateway accepts the *full* location/sort/pagination
    inputs in this body but it does **not** accept server-side
    filtering via the `contracts` array (any non-empty `contracts`
    triggers a 400). Filtering against the hard filter set happens
    *client-side* in the scraper, which is fine because the API
    returns ~200 items per city and the subagent caps the run at
    `ingest_max_listings` (default 50) anyway.

    Args:
        filters: the hard filter set. The values are NOT sent to the
            server (see the note above); they are used to filter the
            response client-side.
        page_number: 1-indexed page.
        page_size: rows per page. The API accepts up to 100.
        location: pre-resolved location; defaults to `resolve_location(filters.city)`.
        sort_by_price: when True, the response comes back sorted by
            ascending price. Useful to surface affordable listings
            first; the scraper uses this when a `max_price_eur` cap
            is set.
    """
    # The argument is part of the public API (callers may pass
    # `HardFilters` for context even though we don't send it).
    del filters
    location = location or resolve_location("Zaragoza")

    body: dict[str, Any] = {
        "combinedLocations": [location.combined_location_ids],
        "contracts": [],
        "culture": "es-ES",
        "hrefLangCultures": "es-ES",
        "includePurchaseTypeFacets": True,
        "isMap": False,
        "latitude": location.latitude,
        "longitude": location.longitude,
        "pageNumber": int(page_number),
        "propertyType": PROPERTY_TYPE_HOME,
        "sortOrderDesc": not sort_by_price,
        "sortType": "price" if sort_by_price else "scoring",
        "transactionType": TRANSACTION_TYPE_RENT,
        "userId": None,
        "size": int(page_size),
    }
    if location.property_subtype_ids:
        body["propertySubtypeIds"] = ";".join(
            str(s) for s in location.property_subtype_ids
        )
    return body


def detail_url_from_item(item: dict[str, Any]) -> str:
    """Build the absolute public URL for a single listing from a search
    response item (uses the Spanish slug for human-friendliness)."""
    for u in item.get("uris", []):
        if u.get("language") == "es_ES":
            path = u["value"]
            return f"{FOTOCASA_PAGE_BASE}{path}"
    # Fallback: construct from propertyId.
    pid = item.get("propertyId") or item.get("id", "")
    return f"{FOTOCASA_PAGE_BASE}/es/alquiler/vivienda/{pid}/d"


# ---------------------------------------------------------------------------
# Legacy CSS-selector helpers.
#
# The HTML detail page is still a valid (if heavy) fallback for
# `fetch_listing(url)` calls that arrive with a propertyId we have
# not seen in a search. `listing_parser.py` uses these to extract
# cards / detail fields from a rendered HTML page. We keep them here
# so the parser doesn't have to know about CSS.
# ---------------------------------------------------------------------------


_LEGACY_SELECTORS: dict[str, str] = {
    "card_container": "[data-testid='result-list-item'], article, .re-SearchResult",
    "card_link": "a[href*='/vivienda/'], a[href*='/inmueble/']",
    "card_title": "[data-testid='title'], h2, .re-SearchResult-title",
    "card_price": "[data-testid='price'], .re-SearchResult-price",
    "detail_title": "h1, [data-testid='detail-title']",
    "detail_price": "[data-testid='detail-price'], .re-DetailPrice",
    "detail_description": "[data-testid='detail-description'], .re-DetailDescription",
    "detail_address": "[data-testid='detail-address'], .re-DetailAddress",
    "jsonld_script": "script[type='application/ld+json']",
    "next_data_script": "script#__NEXT_DATA__",
}


@dataclass(frozen=True, slots=True)
class CardSelector:
    container: str
    link: str
    title: str
    price: str


@dataclass(frozen=True, slots=True)
class DetailSelector:
    title: str
    price: str
    description: str
    address: str


def card_selector() -> CardSelector:
    return CardSelector(
        container=_LEGACY_SELECTORS["card_container"],
        link=_LEGACY_SELECTORS["card_link"],
        title=_LEGACY_SELECTORS["card_title"],
        price=_LEGACY_SELECTORS["card_price"],
    )


def detail_selector() -> DetailSelector:
    return DetailSelector(
        title=_LEGACY_SELECTORS["detail_title"],
        price=_LEGACY_SELECTORS["detail_price"],
        description=_LEGACY_SELECTORS["detail_description"],
        address=_LEGACY_SELECTORS["detail_address"],
    )
