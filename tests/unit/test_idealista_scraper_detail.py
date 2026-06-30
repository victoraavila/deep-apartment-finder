"""`IdealistaScraper.fetch_listing` detail-page enrichment tests.

Sprint 4 (Pillar A) added the playwright-based detail-page fetch.
The happy path: the scraper finds the card on a search page, the
detail client returns the rendered HTML, `parse_detail_page`
extracts `bathrooms` (the canonical Sprint 3 gap), and the
returned `Apartment` carries the long-form `description`.

The fallback paths:
- The detail client is disabled (`IDEALISTA_DETAIL_FETCH=false` or
  `--no-detail-fetch`) — the scraper returns the search-card
  apartment with `bathrooms=None`.
- The detail client returns `None` (per-page transport failure or
  launch failure) — the scraper increments `details_failed` and
  returns the search-card apartment.
- The detail client returns HTML but the page is empty / a
  DataDome interstitial — `parse_detail_page` returns a partial
  dict, the scraper falls back to the card's values, and
  `details_enriched` still increments (the page *was* fetched).

What's covered:
- Happy path: detail fetch + parse → `bathrooms=1`, description
  has the long text.
- Counter updates: `details_enriched` and `details_failed` track
  the per-run counts.
- Fallback: detail client disabled → no fetch, no enrichment.
- Fallback: detail client returns `None` → search-card apartment.
- Detail client `close()` is wired into `scraper.close()`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deep_apartment_finder.adapters.scrapers.idealista.detail_client import (
    IdealistaDetailClient,
)
from deep_apartment_finder.adapters.scrapers.idealista.scraper import IdealistaScraper
from deep_apartment_finder.config import Settings

FIXTURES = Path(__file__).parent.parent / "fixtures" / "idealista"


# --- fakes ---------------------------------------------------------------


class _FakeResponse:
    def __init__(self, *, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class _FakeSession:
    """The `cf_requests.Session` test double used by the
    `IdealistaScraper`. Records every `get` and returns canned
    responses in queue order.
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


def _settings(**kwargs: Any) -> Settings:
    base = dict(
        scraper_user_agent="test-ua",
        scraper_delay_seconds=0.0,
        idealista_scraper_delay_seconds=0.0,
    )
    base.update(kwargs)
    return Settings(**base)


def _page1_response() -> _FakeResponse:
    return _FakeResponse(
        text=(FIXTURES / "search_page1.html").read_text(), status_code=200
    )


def _detail_client(*, enabled: bool = True) -> IdealistaDetailClient:
    return IdealistaDetailClient(enabled=enabled, user_agent="test-ua")


# --- happy path: detail page enriches the apartment ----------------------


@pytest.mark.asyncio
async def test_fetch_listing_enriches_with_detail_block() -> None:
    """Happy path: the search card carries the field set Sprint 3
    already populated; the detail page carries `bathrooms` and the
    long-form `description`. The returned apartment surfaces both.
    """
    detail_html = (FIXTURES / "detail_page1.html").read_text()
    session = _FakeSession([_page1_response()])
    detail = _detail_client(enabled=True)
    detail._context_factory = _factory_returning_html(detail_html)
    scraper = IdealistaScraper(
        settings=_settings(), session=session, detail_client=detail
    )

    apt = await scraper.fetch_listing(
        "https://www.idealista.com/inmueble/109872751/"
    )
    assert apt.bathrooms == 1
    assert apt.description is not None
    assert "Piso luminoso" in apt.description
    # The detail block re-asserts rooms / size when present.
    assert apt.rooms == 2
    assert float(apt.size_m2) == 70.0
    # Counters reflect the success.
    assert scraper.details_enriched == 1
    assert scraper.details_failed == 0
    await scraper.close()


# --- fallback: detail disabled -> no fetch -------------------------------


@pytest.mark.asyncio
async def test_fetch_listing_falls_back_when_detail_disabled() -> None:
    """With `IDEALISTA_DETAIL_FETCH=disabled` (or playwright not
    importable), the scraper skips the detail path entirely and
    returns the search-card apartment. `bathrooms` stays `None`
    (the Sprint 3 behaviour), the `details_failed` counter ticks
    once per call.
    """
    session = _FakeSession([_page1_response()])
    detail = _detail_client(enabled=False)
    scraper = IdealistaScraper(
        settings=_settings(), session=session, detail_client=detail
    )

    apt = await scraper.fetch_listing(
        "https://www.idealista.com/inmueble/109872751/"
    )
    assert apt.bathrooms is None  # Sprint 3 behaviour, preserved
    assert apt.description is not None
    # `description` falls back to the search card's truncated text.
    assert "Piso luminoso" not in (apt.description or "")
    assert scraper.details_enriched == 0
    assert scraper.details_failed == 1
    await scraper.close()


# --- fallback: detail returns None --------------------------------------


@pytest.mark.asyncio
async def test_fetch_listing_falls_back_on_detail_transport_failure() -> None:
    """A per-listing transport failure (DataDome block, 404, etc.)
    on the detail page returns `None` from the client. The
    scraper increments `details_failed` and returns the search-card
    apartment. The run continues; the next `fetch_listing` is
    unaffected.
    """
    session = _FakeSession([_page1_response()])
    detail = _detail_client(enabled=True)
    detail._context_factory = _factory_returning_none()
    scraper = IdealistaScraper(
        settings=_settings(), session=session, detail_client=detail
    )

    apt = await scraper.fetch_listing(
        "https://www.idealista.com/inmueble/109872751/"
    )
    assert apt.bathrooms is None
    assert scraper.details_enriched == 0
    assert scraper.details_failed == 1
    await scraper.close()


# --- fallback: detail returns empty / partial HTML -----------------------


@pytest.mark.asyncio
async def test_fetch_listing_falls_back_on_empty_detail_html() -> None:
    """The detail page is a 200 with an empty body (e.g. a soft
    404). `parse_detail_page` returns a partial dict; the
    scraper still counts the call as `details_enriched` (the page
    *was* fetched) and falls back to the search-card values for
    the missing fields.
    """
    session = _FakeSession([_page1_response()])
    detail = _detail_client(enabled=True)
    detail._context_factory = _factory_returning_html(
        "<html><body>Listing removed.</body></html>"
    )
    scraper = IdealistaScraper(
        settings=_settings(), session=session, detail_client=detail
    )

    apt = await scraper.fetch_listing(
        "https://www.idealista.com/inmueble/109872751/"
    )
    assert apt.bathrooms is None
    # Falls back to the card's fields.
    assert apt.rooms == 2
    assert float(apt.size_m2) == 115.0  # from the page 1 fixture
    assert scraper.details_enriched == 1
    assert scraper.details_failed == 0
    await scraper.close()


# --- close wiring -------------------------------------------------------


@pytest.mark.asyncio
async def test_scraper_close_closes_detail_client() -> None:
    """`scraper.close()` closes both the curl_cffi session and the
    detail client (which closes its `BrowserContext`). The
    detail client's `close()` is idempotent — calling it twice is
    safe.
    """
    session = _FakeSession()
    detail = _detail_client(enabled=True)
    closed = {"n": 0}

    async def _factory(*, user_agent: str | None) -> Any:
        class _Ctx:
            async def close(self) -> None:
                closed["n"] += 1

        return _Ctx()

    detail._context_factory = _factory
    scraper = IdealistaScraper(
        settings=_settings(), session=session, detail_client=detail
    )
    # Launch the context.
    await detail.fetch_detail_html("https://x/1")
    await scraper.close()
    assert session.closed is True
    assert closed["n"] == 1


# --- helpers -------------------------------------------------------------


def _factory_returning_html(html: str):
    class _Ctx:
        async def new_page(self) -> Any:
            class _Page:
                async def goto(self, url: str, timeout: int = 0) -> Any:
                    class _Resp:
                        status = 200
                    return _Resp()

                async def content(self) -> str:
                    return html

                async def close(self) -> None:
                    pass

            return _Page()

        async def close(self) -> None:
            pass

    async def _factory(*, user_agent: str | None) -> Any:
        return _Ctx()

    return _factory


def _factory_returning_none():
    async def _factory(*, user_agent: str | None) -> Any:
        class _Ctx:
            async def new_page(self) -> Any:
                class _Page:
                    async def goto(self, url: str, timeout: int = 0) -> Any:
                        class _Resp:
                            status = 403
                        return _Resp()

                    async def content(self) -> str:
                        return ""

                    async def close(self) -> None:
                        pass

                return _Page()

            async def close(self) -> None:
                pass

        return _Ctx()

    return _factory
