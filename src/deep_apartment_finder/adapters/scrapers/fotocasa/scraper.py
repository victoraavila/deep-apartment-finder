"""Concrete Fotocasa scraper implementing `ScraperPort`.

Composition:
- `httpx` for normal fetches (`client.py`).
- `selectors.py` for URL building and CSS / JSON-LD paths.
- `listing_parser.py` for HTML -> `ListingCard` / `Apartment`.
- `playwright` for CSR fallback (`base.with_csr_fallback`).

The scraper does not own the polite delay between requests — that's a
property of the *run*, not the scraper. Callers (the subagent) cap
how many cards they want and the scraper itself yields them lazily.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx

from deep_apartment_finder.adapters.scrapers.base import (
    RendersJSPage,
    polite_sleep,
    with_csr_fallback,
)
from deep_apartment_finder.adapters.scrapers.fotocasa.listing_parser import (
    parse_detail_page,
    parse_search_page,
)
from deep_apartment_finder.adapters.scrapers.fotocasa.selectors import build_search_url
from deep_apartment_finder.config import Settings
from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.filters.hard import HardFilters
from deep_apartment_finder.ports.scraper import ListingCard, ScraperPort

logger = logging.getLogger(__name__)


def _build_playwright_renderer() -> RendersJSPage | None:
    """Construct a playwright-backed renderer, or `None` if playwright
    is not available in the environment (browser not installed, etc).

    We import lazily so a Sprint 1 run that never hits a CSR page
    doesn't pay the playwright import cost or fail on missing browsers.
    """
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        logger.warning("playwright not available: %s", exc)
        return None

    class _PlaywrightRenderer:
        async def render(self, url: str) -> str:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                try:
                    page = await browser.new_page()
                    await page.goto(url, wait_until="networkidle", timeout=30_000)
                    content = await page.content()
                    return content
                finally:
                    await browser.close()

    return _PlaywrightRenderer()


class FotocasaScraper(ScraperPort):
    """Concrete scraper for Fotocasa.

    The search iterator is intentionally lazy (async generator) so the
    caller can stop early once it has enough material. The polite delay
    is applied between fetched detail pages — search fetches are
    typically a single page.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
        renderer: RendersJSPage | None = None,
        max_cards: int | None = None,
    ) -> None:
        self._settings = settings
        self._http = http_client or httpx.AsyncClient(
            headers={"User-Agent": settings.scraper_user_agent},
            timeout=20.0,
            follow_redirects=True,
        )
        # Renderer policy: the caller can pass an explicit one (tests do
        # this; production can wire a playwright-backed one). We do NOT
        # auto-build a playwright renderer at construction time, because
        # doing so requires `playwright install chromium` to be present on
        # the host. Sprint 1 keeps the renderer opt-in.
        self._renderer = renderer

        # max_cards caps how many cards a single search yields. `None`
        # means no cap. The CLI / subagent may set this to INGEST_MAX_LISTINGS.
        self._max_cards = max_cards

        # Fetcher closure: a single `(url) -> html` function. Avoids
        # monkey-patching httpx.
        async def _fetch(url: str) -> str:
            response = await self._http.get(url)
            response.raise_for_status()
            return response.text

        self._fetch = _fetch

    async def search_listings(self, filters: HardFilters) -> AsyncIterator[ListingCard]:
        url = build_search_url(filters)
        logger.info("fotocasa search: %s", url)
        try:
            html = await with_csr_fallback(
                url,
                fetcher=self._fetch,
                renderer=self._renderer,
            )
        except httpx.HTTPError as exc:
            logger.warning("fotocasa search failed: %s", exc)
            return
        cards = parse_search_page(html)
        if self._max_cards is not None:
            cards = cards[: self._max_cards]
        for card in cards:
            yield card

    async def fetch_listing(self, url: str) -> Apartment:
        # Polite delay between detail fetches to avoid hammering the site.
        await polite_sleep(self._settings.scraper_delay_seconds)
        # Extract the external_id from the URL path.
        import re

        m = re.search(r"/(?:vivienda|inmueble)/([^/?#]+)", url)
        ext_id = m.group(1) if m else url
        try:
            html = await with_csr_fallback(
                url,
                fetcher=self._fetch,
                renderer=self._renderer,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"fotocasa fetch_listing failed: {exc}") from exc
        apt = parse_detail_page(html, url=url, external_id=ext_id)
        if apt is None:
            raise RuntimeError(f"could not parse Fotocasa detail page: {url}")
        return apt

    async def close(self) -> None:
        await self._http.aclose()
