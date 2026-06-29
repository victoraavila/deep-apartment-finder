"""Idealista search-URL helpers.

The Idealista search portal is a server-rendered HTML site (no JSON
endpoint exposed to non-browser clients; the obvious
`/locationsSuggestHit/...` call is 403'd by DataDome). Pagination is
a path suffix `/pagina-N.htm`. Each result page carries 30 cards in
`<article class="item ...">` markup; the field coverage is excellent
on the card except for `lat`/`lng` (only on the detail page, which
is 403'd for non-browser clients — see `client.py`).

This module is the only place that knows the search-URL shape and the
province/city slugs. If Idealista changes the URL contract, update
the constants here — no other file should need to change.
"""

from __future__ import annotations

IDEALISTA_PAGE_BASE = "https://www.idealista.com"

# Province-level search: covers the city of Zaragoza and the surrounding
# municipalities. The user-facing `city="Zaragoza"` in `HardFilters`
# resolves to this province slug. We could add a narrower city-only
# variant (`/alquiler-viviendas/zaragoza/`) later if we want to skip
# pueblos; for now the province gives the operator broader inventory.
IDEALISTA_PROVINCE_PATH = "/alquiler-viviendas/zaragoza-provincia/"


def search_url(city: str, *, page: int = 1) -> str:
    """Build the absolute search URL for `city` at page `page`.

    `city` is currently unused: we only support Zaragoza. Kept in the
    signature so adding a second province later is a 3-line change.

    `page=1` returns the base path; `page=2` returns
    `/alquiler-viviendas/zaragoza-provincia/pagina-2.htm`.
    """
    del city  # only Zaragoza is wired up
    base = f"{IDEALISTA_PAGE_BASE}{IDEALISTA_PROVINCE_PATH}"
    if page <= 1:
        return base
    return f"{base}pagina-{page}.htm"


def detail_url(slug: str, external_id: str) -> str:
    """Build the absolute public URL for a single listing."""
    return f"{IDEALISTA_PAGE_BASE}/inmueble/{external_id}/"


__all__ = [
    "IDEALISTA_PAGE_BASE",
    "IDEALISTA_PROVINCE_PATH",
    "search_url",
    "detail_url",
]
