"""Concrete Idealista scraper implementing `ScraperPort`.

Strategy
--------
1. **Search** (`search_listings`): hit the SSR search URL with
   `curl_cffi` impersonating Chrome 131, paginate via
   `/pagina-N.htm`, and yield `ListingCard`s. The card carries
   title, price, address, rooms, m², partial description, and photo.
   Polite delay between pages (configurable, default 2.0s).
2. **Detail** (`fetch_listing`): Sprint 4 upgrade — a single
   `playwright.async_api.BrowserContext`
   (`adapters/scrapers/idealista/detail_client.py`) hits the
   `/inmueble/<id>/` page and renders it, accumulating real-browser
   signals (mouse movement, JS execution) that a `curl_cffi` session
   cannot. The detail page carries the `bathrooms` field that the
   search card never does, plus a long-form `description`. We
   enrich the search-card apartment with the detail block via
   `apply_detail_enrichment(...)`. When the detail path is disabled
   (`IDEALISTA_DETAIL_FETCH=false`) or the browser fails to launch,
   `fetch_listing` falls back to the search-card walk; the
   returned apartment has `bathrooms=None` (the Sprint 3 behaviour).

The detail page is gated by:
- `IDEALISTA_DETAIL_FETCH=false` (CLI flag `--no-detail-fetch` or
  env var) → no browser launch, no detail fetch, fallback path.
- `playwright` is not importable → no detail fetch, fallback path.
- The browser fails to launch (e.g. Chromium binary missing) →
  the detail client disables itself for the rest of the run and
  falls back.

The `IdealistaScraper` keeps two counters for the run report:
- `details_enriched` — how many detail pages we successfully
  rendered and parsed.
- `details_failed` — how many `fetch_listing` calls fell back to
  the search-card path because the detail page could not be
  fetched. Reset to 0 on construction so a CLI run that creates
  the scraper for each run sees the right count.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator

from curl_cffi import requests as cf_requests

from deep_apartment_finder.adapters.scrapers.base import polite_sleep
from deep_apartment_finder.adapters.scrapers.idealista.api import (
    apply_detail_enrichment,
    card_to_apartment,
    parse_detail_page,
    parse_search_page,
)
from deep_apartment_finder.adapters.scrapers.idealista.client import (
    build_http_client,
    request_with_timeout,
)
from deep_apartment_finder.adapters.scrapers.idealista.detail_client import (
    IdealistaDetailClient,
    playwright_importable,
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
        detail_client: IdealistaDetailClient | None = None,
    ) -> None:
        self._settings = settings
        if session is not None:
            self._session = session
        else:
            self._session = build_http_client(
                impersonate=impersonate or settings.idealista_impersonate
            )
        self._max_cards = max_cards
        # Sprint 4 detail-page enrichment. The client is gated on
        # both the env setting AND playwright being importable. The
        # `is_enabled` property on the client captures the disabled
        # state (so a one-shot browser-launch failure also disables
        # the rest of the run).
        if detail_client is not None:
            self._detail = detail_client
        else:
            enabled = bool(
                getattr(settings, "idealista_detail_fetch", True)
                and playwright_importable()
            )
            self._detail = IdealistaDetailClient(
                enabled=enabled,
                user_agent=settings.scraper_user_agent,
            )
        self._details_enriched = 0
        self._details_failed = 0

    @property
    def details_enriched(self) -> int:
        return self._details_enriched

    @property
    def details_failed(self) -> int:
        return self._details_failed

    @property
    def detail_fetch_enabled(self) -> bool:
        return self._detail.is_enabled

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

        Three-step path (Sprint 4):

        1. Walk the search pages until we find a card with the requested
           id. This is the same walk Sprint 3 used, but it now
           *captures* the last fetched page's HTML so the detail
           parser can re-use it as the "soft 404" fallback.
        2. If the detail client is enabled, fetch `/inmueble/<id>/`
           via the shared playwright `BrowserContext` and parse
           `parse_detail_page(html, url=url)`. On success, the
           returned apartment has `bathrooms` populated and the
           long-form `description`. On failure (`None` from the
           client), we fall back to the search-card fields.
        3. Apply the enrichment via `apply_detail_enrichment(...)`
           and return the apartment. The two counters
           (`details_enriched`, `details_failed`) are updated for the
           run report.
        """
        m = re.search(r"/inmueble/(\d+)/?", url)
        if not m:
            raise RuntimeError(f"idealista: could not extract id from {url!r}")
        external_id = m.group(1)

        # Walk search pages to find the card. The walk is a side
        # effect: we return the matching card's raw fields, and on
        # the "right" page we hand the HTML to the detail parser
        # (so the soft-404 case still has SOMETHING to parse).
        last_html: str | None = None
        card: ListingCard | None = None
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
            last_html = response.text
            cards = parse_search_page(last_html)
            for c in cards:
                if c.external_id == external_id:
                    card = c
                    break
            if card is not None:
                break
            if not cards or len(cards) < 15:
                raise RuntimeError(
                    f"idealista fetch_listing: id {external_id} not found in any page"
                )
            page += 1
            await polite_sleep(self._settings.idealista_scraper_delay_seconds)

        # Now enrich. The detail client is opt-in; if it's disabled
        # (or the launch fails) we return the search-card apartment
        # unchanged. The two counters track which path we took.
        if not self._detail.is_enabled:
            self._details_failed += 1
            return card_to_apartment(card)

        detail_html = await self._detail.fetch_detail_html(url)
        if detail_html is None:
            self._details_failed += 1
            return card_to_apartment(card)

        try:
            detail = parse_detail_page(detail_html, url=url)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "idealista fetch_listing: detail parse failed for %s: %s",
                url,
                exc,
            )
            self._details_failed += 1
            return card_to_apartment(card)

        self._details_enriched += 1
        return apply_detail_enrichment(card, detail)

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
        # Close the playwright BrowserContext too. The detail client
        # makes `close()` idempotent.
        await self._detail.close()


__all__ = ["IdealistaScraper"]
