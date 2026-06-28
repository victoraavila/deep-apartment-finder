"""Fotocasa search URL and field selectors.

This file is the only place in the project that needs to change when
Fotocasa updates its HTML. It is deliberately small: just a URL builder
and CSS / JSON-LD path constants. The parser does not know about HTML;
it just gets a `select` callable and a string of HTML.

The values here are best-guess for the public Fotocasa search results
page (Zaragoza, alquiler). They are designed to be obvious to a human
maintainer and forgiving in the parser.

If the live site changes, the user can update the constants in this
file without touching parser or scraper logic. The integration test for
the orchestrator (test_orchestrator.py) uses a fake scraper and is
unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from deep_apartment_finder.domain.filters.hard import HardFilters

# Search results page (list). The public URL shape is stable enough
# that we can build a query string and let the parser work from there.
FOTOCASA_SEARCH_BASE = (
    "https://www.fotocasa.es/es/alquiler/viviendas/zaragoza-capital/todas-las-zonas"
)


def build_search_url(filters: HardFilters) -> str:
    """Build a Fotocasa search URL for the given hard filters.

    The site is a SPA; the URL itself is mostly cosmetic — the real
    search runs against a JSON endpoint that the listing parser reads
    out of the page's `__NEXT_DATA__` or `__INITIAL_STATE__` blob. The
    URL is what we'd see in the browser, and is what we want to record
    in the agent's logs / debug snapshots.
    """
    params: dict[str, str] = {}
    if filters.min_rooms is not None:
        params["rooms"] = str(filters.min_rooms)
    if filters.min_bathrooms is not None:
        params["bathrooms"] = str(filters.min_bathrooms)
    if filters.min_size_m2 is not None:
        params["minSize"] = str(int(filters.min_size_m2))
    if filters.max_price_eur is not None:
        params["maxPrice"] = str(int(filters.max_price_eur))
    if params:
        return f"{FOTOCASA_SEARCH_BASE}?{urlencode(params)}"
    return FOTOCASA_SEARCH_BASE


# CSS selectors used to extract listing cards from a server-rendered
# (SSR) search page. We try the most generic container first and
# fall back to per-field selectors if a card is missing data.
SELECTORS = _SELECTORS = {
    "card_container": "[data-testid='result-list-item'], article, .re-SearchResult",
    "card_link": "a[href*='/vivienda/'], a[href*='/inmueble/']",
    "card_title": "[data-testid='title'], h2, .re-SearchResult-title",
    "card_price": "[data-testid='price'], .re-SearchResult-price",
    "detail_title": "h1, [data-testid='detail-title']",
    "detail_price": "[data-testid='detail-price'], .re-DetailPrice",
    "detail_description": "[data-testid='detail-description'], .re-DetailDescription",
    "detail_address": "[data-testid='detail-address'], .re-DetailAddress",
    # JSON-LD is the modern, stable source. Many Fotocasa pages embed
    # the listing as a `Product` or `Apartment` JSON-LD blob.
    "jsonld_script": "script[type='application/ld+json']",
    # The next.js bootstrap. The page renders an empty shell and JS
    # fills it; the bootstrap is the *only* place the data lives on
    # CSR pages, so we read it as a last resort.
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
        container=SELECTORS["card_container"],
        link=SELECTORS["card_link"],
        title=SELECTORS["card_title"],
        price=SELECTORS["card_price"],
    )


def detail_selector() -> DetailSelector:
    return DetailSelector(
        title=SELECTORS["detail_title"],
        price=SELECTORS["detail_price"],
        description=SELECTORS["detail_description"],
        address=SELECTORS["detail_address"],
    )
