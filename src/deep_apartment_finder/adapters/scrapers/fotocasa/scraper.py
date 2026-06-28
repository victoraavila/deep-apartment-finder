"""Concrete Fotocasa scraper implementing `ScraperPort`.

The scraper talks to Fotocasa's internal JSON API rather than scraping
the public HTML, because the public search page is a CSR shell that
404s without a real browser session. The relevant endpoint is:

    POST https://web.gw.fotocasa.es/v1/search/ads

with a body that encodes the hard filters, the location, the page
number, and the page size. The response carries the same fields the
detail page would have (rooms, baths, surface, description, photos,
geo, agency, uris, transaction, features) so we don't need a
secondary round-trip per listing.

Composition:
- `httpx` for the API call (`client.py`).
- `selectors.py` for the request body shape and the location table.
- `api.py` for response -> `ListingCard` / `Apartment`.

The search iterator is lazy (async generator) so the subagent can
stop early once it has enough material. The polite delay is applied
between paged fetches, not between cards.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx

from deep_apartment_finder.adapters.scrapers.base import polite_sleep
from deep_apartment_finder.adapters.scrapers.fotocasa.api import (
    decode_response_body,
    item_to_apartment,
    item_to_card,
)
from deep_apartment_finder.adapters.scrapers.fotocasa.listing_parser import (
    parse_detail_page,
)
from deep_apartment_finder.adapters.scrapers.fotocasa.selectors import (
    FOTOCASA_PAGE_BASE,
    FOTOCASA_REQUEST_HEADERS,
    FOTOCASA_SEARCH_URL,
    build_search_request_body,
    resolve_location,
)
from deep_apartment_finder.config import Settings
from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.filters.hard import HardFilters
from deep_apartment_finder.ports.scraper import ListingCard, ScraperPort

logger = logging.getLogger(__name__)


class FotocasaScraper(ScraperPort):
    """Concrete scraper for Fotocasa, driven by the JSON search API.

    The scraper resolves the city -> `combinedLocationIds` once per
    `search_listings` call (cheap dict lookup), then loops over pages
    of `POST /v1/search/ads` until it has either:
    - yielded `max_cards` cards (caller-set cap), or
    - exhausted the result set (`pageNumber` past `totalItems`).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
        max_cards: int | None = None,
        page_size: int | None = None,
    ) -> None:
        self._settings = settings
        # Build a client with the API-required headers. We let the user
        # inject a fake one (tests do this); otherwise we use the
        # production client from `client.py` and merge our headers on
        # top, so the production UA / Accept-Language are preserved.
        if http_client is not None:
            self._http = http_client
        else:
            from deep_apartment_finder.adapters.scrapers.fotocasa.client import (
                build_http_client,
            )

            self._http = build_http_client(settings.scraper_user_agent)
            for k, v in FOTOCASA_REQUEST_HEADERS.items():
                self._http.headers[k] = v

        self._max_cards = max_cards
        self._page_size = page_size or 30
        # Cache of propertyId -> raw search-item, so `fetch_listing`
        # can hand back the same data the search already gave us.
        self._item_cache: dict[str, dict[str, object]] = {}

    async def search_listings(self, filters: HardFilters) -> AsyncIterator[ListingCard]:
        """Yield `ListingCard`s that pass the hard filters.

        We issue one POST per page of `page_size` results. Between pages
        we apply the configured polite delay. We swallow per-page HTTP
        errors and stop early (after logging) — we'd rather yield what
        we have than blow up the whole run.

        Filtering note: the Fotocasa `/v1/search/ads` endpoint does not
        accept server-side filter contracts (any non-empty `contracts`
        array triggers a 400), so we filter the response client-side
        using `HardFilters.passes()`. That is fine because a single
        city has ~200 listings at most and the subagent caps the run
        at `ingest_max_listings` (default 50) anyway.
        """
        try:
            location = resolve_location(filters.city)
        except KeyError as exc:
            logger.warning("fotocasa: %s", exc)
            return

        yielded = 0
        inspected = 0
        page_number = 1
        while True:
            if self._max_cards is not None and yielded >= self._max_cards:
                logger.info(
                    "fotocasa search/ads: cap reached, yielded=%d inspected=%d",
                    yielded,
                    inspected,
                )
                return

            body = build_search_request_body(
                filters,
                page_number=page_number,
                page_size=self._page_size,
                location=location,
                sort_by_price=filters.max_price_eur is not None,
            )
            logger.info(
                "fotocasa search/ads: page=%d size=%d location=%s",
                page_number,
                self._page_size,
                location.slug,
            )
            try:
                response = await self._http.post(FOTOCASA_SEARCH_URL, json=body)
                response.raise_for_status()
                payload = decode_response_body(response.text)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("fotocasa search/ads failed: %s", exc)
                return

            total = int(payload.get("totalItems", 0) or 0)
            items = payload.get("items") or []
            logger.info(
                "fotocasa search/ads: totalItems=%d pageItems=%d",
                total,
                len(items),
            )

            for raw in items:
                if not isinstance(raw, dict):
                    continue
                card = item_to_card(raw, base_url=FOTOCASA_PAGE_BASE)
                if card is None:
                    continue
                # Client-side filter against the hard filter set.
                # We build a transient Apartment for the predicate;
                # the full apartment (with description, etc.) is
                # available later via `fetch_listing(url)`.
                if not _card_passes_hard_filters(card, filters):
                    continue
                # Cache the raw item for `fetch_listing`.
                self._item_cache[card.external_id] = raw
                yield card
                yielded += 1
                if self._max_cards is not None and yielded >= self._max_cards:
                    return
                inspected += 1

            # End-of-results: empty page, page number past total, or the
            # last page returned fewer items than `size` (Fotocasa's
            # signal that there are no more rows).
            page_info = payload.get("page") or {}
            returned_size = page_info.get("size") or self._page_size
            if not items:
                return
            if total and page_number * returned_size >= total:
                return
            if len(items) < self._page_size:
                return
            page_number += 1
            await polite_sleep(self._settings.scraper_delay_seconds)

    async def fetch_listing(self, url: str) -> Apartment:
        """Return a normalized `Apartment` for the given detail-page URL.

        The search API response is rich enough that we don't need to
        fetch the detail HTML at all — the same fields (rooms, baths,
        surface, description, geo, agency, photos, features, uris,
        transaction) are present in every search-result item. We:

        1. Try the in-memory cache populated by `search_listings`.
        2. If the URL mentions a `propertyId`, try to re-issue a
           `search/ads` request scoped to that id (Fotocasa doesn't
           expose a per-id endpoint we can call directly).
        3. As a last resort, fall back to the old HTML detail-page
           parser. This keeps `fetch_listing` working in isolation
           (tests, ad-hoc calls).
        """
        prop_id = _extract_property_id(url)
        if prop_id and prop_id in self._item_cache:
            raw = self._item_cache[prop_id]
            apt = item_to_apartment(raw, base_url=FOTOCASA_PAGE_BASE)  # type: ignore[arg-type]
            if apt is not None:
                return apt

        # Fallback: try the HTML detail page. Best-effort; the public
        # page is a CSR shell and the playwright fallback may not be
        # available, so we propagate a clear error if it fails.
        ext_id = prop_id or url
        try:
            response = await self._http.get(url)
            response.raise_for_status()
            html = response.text
        except httpx.HTTPError as exc:
            raise RuntimeError(f"fotocasa fetch_listing failed: {exc}") from exc
        apt = parse_detail_page(html, url=url, external_id=ext_id)
        if apt is None:
            raise RuntimeError(f"could not parse Fotocasa detail page: {url}")
        return apt

    async def close(self) -> None:
        await self._http.aclose()


def _extract_property_id(url: str) -> str | None:
    """Pull the `propertyId` (numeric) out of a Fotocasa detail URL.

    Slug shape: ``/es/alquiler/vivienda/<city>/<features>/<id>/d``.
    The id is the second-to-last non-empty path segment.
    """
    import re

    m = re.search(r"/(?:vivienda|inmueble)/[^/]+(?:/[^/]+)?/(\d+)/d", url)
    if m:
        return m.group(1)
    return None


def _card_passes_hard_filters(card: ListingCard, filters: HardFilters) -> bool:
    """Apply `HardFilters.passes()` to a `ListingCard`.

    The search-response item has the same numeric fields the filter
    needs (rooms, baths, surface, transaction.price). The card's
    `ListingCard.raw` is the full item; we read from there.
    """
    raw = card.raw or {}
    rooms = raw.get("rooms")
    baths = raw.get("baths")
    surface = raw.get("surface")
    transaction = raw.get("transaction") or {}
    price = transaction.get("price")
    if filters.min_rooms is not None and (rooms is None or rooms < filters.min_rooms):
        return False
    if filters.min_bathrooms is not None and (
        baths is None or baths < filters.min_bathrooms
    ):
        return False
    if filters.min_size_m2 is not None and (
        surface is None or float(surface) < float(filters.min_size_m2)
    ):
        return False
    if filters.max_price_eur is not None and (
        price is None or float(price) > float(filters.max_price_eur)
    ):
        return False
    return True
