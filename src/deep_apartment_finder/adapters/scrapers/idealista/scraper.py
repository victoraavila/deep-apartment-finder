"""Concrete Idealista scraper implementing `ScraperPort`.

Strategy
--------
1. **Search** (`search_listings`): hit the SSR search URL with
   `curl_cffi` impersonating Chrome 131, paginate via
   `/pagina-N.htm`, and yield `ListingCard`s. The card carries
   title, price, address, rooms, m², partial description, and photo.
   Polite delay between pages (configurable, default 2.0s).
2. **Detail** (`fetch_listing`): does NOT hit Idealista's
   `/inmueble/<id>/` endpoint — DataDome trust-scores session
   cookies against real-browser signals (mouse movement, JS
   execution), and a `curl_cffi` session can never accumulate
   enough trust. Instead, we return the card data the search
   already gave us; the `Apartment` will have `lat`/`lng` and
   sometimes `bathrooms` as `None`. ADR-011 documents the
   planned playwright upgrade for the detail path.

This is the "least invasive" approach the SPRINT3 doc calls for.
If the search results prove insufficient (e.g. every card has
`lat`/`None` so the distance criterion scores neutral 0.5 for
every Idealista row), the fallback is to swap this adapter for
Pisos.com (or whatever easier portal survives) — the same
`ScraperPort`, no orchestrator change.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator

from curl_cffi import requests as cf_requests

from deep_apartment_finder.adapters.scrapers.base import polite_sleep
from deep_apartment_finder.adapters.scrapers.idealista.api import (
    card_to_apartment,
    parse_search_page,
)
from deep_apartment_finder.adapters.scrapers.idealista.client import (
    build_http_client,
    request_with_timeout,
)
from deep_apartment_finder.adapters.scrapers.idealista.selectors import search_url
from deep_apartment_finder.config import Settings
from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.filters.hard import HardFilters
from deep_apartment_finder.ports.scraper import ListingCard, ScraperPort

logger = logging.getLogger(__name__)


class IdealistaScraper(ScraperPort):
    """Concrete scraper for Idealista, driven by the SSR search HTML."""

    def __init__(
        self,
        *,
        settings: Settings,
        session: cf_requests.Session | None = None,
        max_cards: int | None = None,
        impersonate: str | None = None,
    ) -> None:
        self._settings = settings
        if session is not None:
            self._session = session
        else:
            self._session = build_http_client(
                impersonate=impersonate or settings.idealista_impersonate
            )
        self._max_cards = max_cards

    async def search_listings(self, filters: HardFilters) -> AsyncIterator[ListingCard]:
        """Yield `ListingCard`s that pass the hard filters.

        We issue one GET per page of 30 results. Between pages we
        apply the configured polite delay. Per-page HTTP errors are
        logged and the iteration stops — we'd rather yield what we
        have than blow up the whole run.

        The `city` field of `HardFilters` is accepted for protocol
        symmetry with Fotocasa but only `"Zaragoza"` is wired up.
        Other values are mapped to the Zaragoza province URL.
        """
        del filters  # we filter client-side after the page parse
        yielded = 0
        inspected = 0
        page = 1
        seen_external_ids: set[str] = set()
        while True:
            if self._max_cards is not None and yielded >= self._max_cards:
                logger.info(
                    "idealista search: cap reached, yielded=%d inspected=%d",
                    yielded,
                    inspected,
                )
                return

            url = search_url("Zaragoza", page=page)
            logger.info("idealista search: page=%d url=%s", page, url)
            try:
                response = request_with_timeout(self._session, url)
                if response.status_code != 200:
                    logger.warning(
                        "idealista search: page=%d status=%d (stopping)",
                        page,
                        response.status_code,
                    )
                    return
                html = response.text
            except Exception as exc:  # noqa: BLE001
                logger.warning("idealista search: page=%d failed: %s", page, exc)
                return

            try:
                cards = parse_search_page(html)
            except Exception as exc:  # noqa: BLE001
                logger.warning("idealista search: page=%d parse failed: %s", page, exc)
                return

            inspected += len(cards)
            logger.info(
                "idealista search: page=%d pageItems=%d cumulativeInspected=%d yielded=%d",
                page,
                len(cards),
                inspected,
                yielded,
            )

            if not cards:
                # Empty page means we've hit the end of the result set.
                return

            for card in cards:
                if card.external_id in seen_external_ids:
                    continue
                seen_external_ids.add(card.external_id)
                yield card
                yielded += 1
                if self._max_cards is not None and yielded >= self._max_cards:
                    return

            # End-of-results heuristic: 30 is the typical full page; if
            # we got fewer than 15 there are probably no more pages.
            if len(cards) < 15:
                return

            page += 1
            await polite_sleep(self._settings.idealista_scraper_delay_seconds)

    async def fetch_listing(self, url: str) -> Apartment:
        """Return a normalized `Apartment` for the given detail-page URL.

        This method does NOT issue a second HTTP call to Idealista. The
        DataDome bot manager trust-scores session cookies against
        real-browser signals, and a `curl_cffi` session can never
        accumulate enough trust to access `/inmueble/<id>/` — every
        request gets 403'd.

        Instead, we re-fetch the search page and find the matching
        card in the page-1 cache. If the listing is on a different
        page, we keep paginating until we find it. If we never see
        the card (because it has scrolled off the visible pages), we
        raise — the caller's caller (the subagent) will surface the
        error and continue.

        The returned `Apartment` has the card's field set:
        `title`, `price_eur`, `rooms`, `size_m2`, `address`,
        partial `description`. `bathrooms` may be `None`. `lat`/`lng`
        are always `None` (only on detail pages).
        """
        m = re.search(r"/inmueble/(\d+)/?", url)
        if not m:
            raise RuntimeError(f"idealista: could not extract id from {url!r}")
        external_id = m.group(1)

        # Walk pages until we find a card with the requested id, or run
        # out of pages.
        page = 1
        while True:
            page_url = search_url("Zaragoza", page=page)
            try:
                response = request_with_timeout(self._session, page_url)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"idealista fetch_listing: page {page} request failed: {exc}"
                ) from exc
            if response.status_code != 200:
                raise RuntimeError(
                    f"idealista fetch_listing: page {page} returned {response.status_code}"
                )
            cards = parse_search_page(response.text)
            for card in cards:
                if card.external_id == external_id:
                    return card_to_apartment(card)
            if not cards or len(cards) < 15:
                raise RuntimeError(
                    f"idealista fetch_listing: id {external_id} not found in any page"
                )
            page += 1
            await polite_sleep(self._settings.idealista_scraper_delay_seconds)

    async def close(self) -> None:
        # `curl_cffi` sessions expose `close()`. Wrap in try/except so
        # the scraper is robust to alternate test doubles.
        close = getattr(self._session, "close", None)
        if callable(close):
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:  # noqa: BLE001
                pass


__all__ = ["IdealistaScraper"]
