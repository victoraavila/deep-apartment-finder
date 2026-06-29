"""IdealistaScraper behaviour tests with a fake `curl_cffi` session.

The real `IdealistaScraper` is composed of three pieces:
- `client.build_http_client` — builds a `cf_requests.Session` (we fake it)
- `selectors.search_url` — covered in `test_idealista_parser.py`
- `api.parse_search_page` / `card_to_apartment` — covered in
  `test_idealista_parser.py`

The unit tests here verify the composition: that the scraper
fetches the right URL per page, paginates, applies the polite
delay, dedupes across pages via the seen-ids set, and surfaces
HTTP errors as empty iterations (not raised exceptions).

The fixtures used here are the same offline HTML captures from
`tests/fixtures/idealista/`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deep_apartment_finder.adapters.scrapers.idealista.scraper import IdealistaScraper
from deep_apartment_finder.config import Settings
from deep_apartment_finder.domain.filters.hard import HardFilters

FIXTURES = Path(__file__).parent.parent / "fixtures" / "idealista"


# --- Fake session that mimics cf_requests.Session --------------------------


class _FakeResponse:
    def __init__(self, *, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class _FakeSession:
    """Records every GET and returns canned responses in queue order.

    Mirrors the slice of `cf_requests.Session` the scraper uses: a
    single `get(url, ...)` method that returns an object with
    `status_code` and `text`.
    """

    def __init__(self, responses: list[_FakeResponse] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls: list[str] = []
        self.closed = False

    def queue(self, *responses: _FakeResponse) -> None:
        self._responses.extend(responses)

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(url)
        if not self._responses:
            return _FakeResponse(text="", status_code=200)
        return self._responses.pop(0)

    def close(self) -> None:
        self.closed = True


# --- Helpers ---------------------------------------------------------------


def _settings(**kwargs: Any) -> Settings:
    base = dict(
        scraper_user_agent="test-ua",
        scraper_delay_seconds=0.0,
        idealista_scraper_delay_seconds=0.0,
    )
    base.update(kwargs)
    return Settings(**base)


def _page1_response() -> _FakeResponse:
    return _FakeResponse(text=(FIXTURES / "search_page1.html").read_text(), status_code=200)


def _page2_response() -> _FakeResponse:
    return _FakeResponse(text=(FIXTURES / "search_page2.html").read_text(), status_code=200)


# --- search_listings -------------------------------------------------------


@pytest.mark.asyncio
async def test_search_listings_yields_page1_cards() -> None:
    """The first page should yield all 30 cards with the correct external_ids."""
    session = _FakeSession([_page1_response()])
    scraper = IdealistaScraper(settings=_settings(), session=session)
    cards: list = []
    async for c in scraper.search_listings(HardFilters()):
        cards.append(c)
    assert len(cards) == 30
    assert session.calls[0].endswith("/alquiler-viviendas/zaragoza-provincia/")
    await scraper.close()


@pytest.mark.asyncio
async def test_search_listings_paginates_via_pagina_N_dot_htm() -> None:
    """After exhausting page 1 (< 15 cards), the scraper must stop; with 30
    cards on page 1, it must request page 2 to keep filling."""
    session = _FakeSession([_page1_response(), _page2_response()])
    scraper = IdealistaScraper(settings=_settings(), session=session, max_cards=40)
    cards: list = []
    async for c in scraper.search_listings(HardFilters()):
        cards.append(c)
    assert len(cards) == 40
    assert len(session.calls) == 2
    assert session.calls[0].endswith("/alquiler-viviendas/zaragoza-provincia/")
    assert session.calls[1].endswith("/alquiler-viviendas/zaragoza-provincia/pagina-2.htm")
    await scraper.close()


@pytest.mark.asyncio
async def test_search_listings_stops_when_page_returns_fewer_than_15() -> None:
    """The end-of-results heuristic: a short page means we hit the tail."""
    # Build a short fake page with 5 cards. Reuse page 1's HTML; we just
    # want to confirm the scraper stops after 1 call when the page is
    # short. Easier: hand-craft a tiny page.
    short_html = """
    <html><body>
    <article class="item">
      <a class="item-link" href="/inmueble/1/">one</a>
      <span class="item-price">500 €/mes</span>
      <span class="item-detail">1 hab.</span>
      <span class="item-detail">40 m²</span>
    </article>
    </body></html>
    """
    session = _FakeSession([_FakeResponse(text=short_html)])
    scraper = IdealistaScraper(settings=_settings(), session=session, max_cards=50)
    cards: list = []
    async for c in scraper.search_listings(HardFilters()):
        cards.append(c)
    assert len(cards) == 1
    assert len(session.calls) == 1
    await scraper.close()


@pytest.mark.asyncio
async def test_search_listings_respects_max_cards() -> None:
    session = _FakeSession([_page1_response()])
    scraper = IdealistaScraper(settings=_settings(), session=session, max_cards=5)
    cards: list = []
    async for c in scraper.search_listings(HardFilters()):
        cards.append(c)
    assert len(cards) == 5
    assert len(session.calls) == 1  # one page was enough
    await scraper.close()


@pytest.mark.asyncio
async def test_search_listings_dedupes_across_pages() -> None:
    """If page 1 and page 2 share an external_id, it should appear only once."""
    p1 = _page1_response()
    p2 = _page1_response()  # identical content -> all 30 ids repeat
    session = _FakeSession([p1, p2])
    scraper = IdealistaScraper(settings=_settings(), session=session, max_cards=100)
    seen: list = []
    async for c in scraper.search_listings(HardFilters()):
        seen.append(c.external_id)
    assert len(seen) == len(set(seen))
    await scraper.close()


@pytest.mark.asyncio
async def test_search_listings_swallows_http_error_and_returns_empty() -> None:
    """A 403 on the first page must not raise; the iteration just stops."""

    class _BoomSession:
        def get(self, url: str, **kwargs: Any) -> _FakeResponse:
            return _FakeResponse(text="<html>interstitial</html>", status_code=403)

        def close(self) -> None:
            pass

    scraper = IdealistaScraper(settings=_settings(), session=_BoomSession())
    cards: list = []
    async for c in scraper.search_listings(HardFilters()):
        cards.append(c)
    assert cards == []
    await scraper.close()


@pytest.mark.asyncio
async def test_search_listings_swallows_transport_error() -> None:
    class _ExplodingSession:
        def get(self, url: str, **kwargs: Any) -> _FakeResponse:
            raise RuntimeError("connection reset")

        def close(self) -> None:
            pass

    scraper = IdealistaScraper(settings=_settings(), session=_ExplodingSession())
    cards: list = []
    async for c in scraper.search_listings(HardFilters()):
        cards.append(c)
    assert cards == []
    await scraper.close()


# --- fetch_listing ---------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_listing_finds_id_on_first_page() -> None:
    """The id on page 1 should be returned without paging further."""
    session = _FakeSession([_page1_response()])
    scraper = IdealistaScraper(settings=_settings(), session=session)
    apt = await scraper.fetch_listing("https://www.idealista.com/inmueble/109872751/")
    assert apt.source.value == "idealista"
    assert apt.external_id == "109872751"
    assert apt.title is not None
    assert apt.price_eur is not None
    assert len(session.calls) == 1
    await scraper.close()


@pytest.mark.asyncio
async def test_fetch_listing_walks_pages_to_find_id() -> None:
    """If the id is on page 2, the scraper pages through until it finds it."""
    # page 1 = real page 1 (no id=999999999); page 2 = a tiny HTML with
    # that id; the scraper should call page 1, then page 2.
    tiny_page2 = """
    <html><body>
    <article class="item">
      <a class="item-link" href="/inmueble/999999999/">target</a>
      <span class="item-price">700 €/mes</span>
      <span class="item-detail">1 hab.</span>
      <span class="item-detail">50 m²</span>
      <p class="ellipsis">tiny listing</p>
    </article>
    </body></html>
    """
    session = _FakeSession([_page1_response(), _FakeResponse(text=tiny_page2)])
    scraper = IdealistaScraper(settings=_settings(), session=session)
    apt = await scraper.fetch_listing("https://www.idealista.com/inmueble/999999999/")
    assert apt.external_id == "999999999"
    assert apt.title == "target"
    assert len(session.calls) == 2
    assert session.calls[1].endswith("/pagina-2.htm")
    await scraper.close()


@pytest.mark.asyncio
async def test_fetch_listing_raises_when_id_not_found() -> None:
    """No page contains the id -> clear error."""
    session = _FakeSession([_page1_response()])
    scraper = IdealistaScraper(settings=_settings(), session=session)
    with pytest.raises(RuntimeError, match="not found"):
        await scraper.fetch_listing("https://www.idealista.com/inmueble/999999999/")
    await scraper.close()


@pytest.mark.asyncio
async def test_fetch_listing_raises_on_unparseable_url() -> None:
    session = _FakeSession()
    scraper = IdealistaScraper(settings=_settings(), session=session)
    with pytest.raises(RuntimeError, match="could not extract id"):
        await scraper.fetch_listing("https://www.idealista.com/foo/bar/")
    await scraper.close()


# --- close -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_calls_session_close() -> None:
    session = _FakeSession()
    scraper = IdealistaScraper(settings=_settings(), session=session)
    await scraper.close()
    assert session.closed is True
