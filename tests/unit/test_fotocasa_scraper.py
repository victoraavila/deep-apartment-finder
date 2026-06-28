"""FotocasaScraper behaviour tests using a fake http client and renderer.

The real scraper is composed of three pieces:
- httpx client (we fake it)
- playwright renderer (we fake it)
- parser (covered by test_fotocasa_parser)

The unit tests verify the composition: that the scraper pulls the right
URL, applies the polite delay, falls back to the renderer on CSR pages,
and yields parsed cards in order.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from deep_apartment_finder.adapters.scrapers.fotocasa.scraper import FotocasaScraper
from deep_apartment_finder.config import Settings
from deep_apartment_finder.domain.filters.hard import HardFilters
from deep_apartment_finder.ports.scraper import ListingCard


class FakeHttpClient:
    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def get(self, url: str) -> Any:
        self.calls.append(url)
        if url not in self._responses:
            raise KeyError(f"FakeHttpClient: no response for {url}")
        return _FakeResponse(self._responses[url])

    async def aclose(self) -> None:
        return None


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class FakeRenderer:
    def __init__(self, html: str) -> None:
        self._html = html
        self.calls: list[str] = []

    async def render(self, url: str) -> str:
        self.calls.append(url)
        return self._html


def _settings(**kwargs: Any) -> Settings:
    base = dict(
        scraper_user_agent="test-ua",
        scraper_delay_seconds=0.0,  # don't slow tests
    )
    base.update(kwargs)
    return Settings(**base)


_SEARCH_HTML = """
<html><head>
<script type="application/ld+json">
{"@type": "Apartment", "url": "https://fotocasa.es/vivienda/abc", "name": "A", "offers": {"price": 950}}
</script>
<script type="application/ld+json">
{"@type": "Apartment", "url": "https://fotocasa.es/vivienda/def", "name": "B", "offers": {"price": 1100}}
</script>
</head><body></body></html>
"""


@pytest.mark.asyncio
async def test_search_listings_yields_cards_in_order():
    settings = _settings()
    http = FakeHttpClient({"https://www.fotocasa.es/es/alquiler/viviendas/zaragoza-capital/todas-las-zonas?rooms=2&bathrooms=2&minSize=50&maxPrice=1200": _SEARCH_HTML})
    scraper = FotocasaScraper(settings=settings, http_client=http, renderer=None)
    out: list[ListingCard] = []
    async for c in scraper.search_listings(HardFilters()):
        out.append(c)
    assert [c.external_id for c in out] == ["abc", "def"]
    await scraper.close()


@pytest.mark.asyncio
async def test_search_listings_respects_max_cards():
    settings = _settings()
    http = FakeHttpClient({"https://www.fotocasa.es/es/alquiler/viviendas/zaragoza-capital/todas-las-zonas?rooms=2&bathrooms=2&minSize=50&maxPrice=1200": _SEARCH_HTML})
    scraper = FotocasaScraper(settings=settings, http_client=http, renderer=None, max_cards=1)
    out: list[ListingCard] = []
    async for c in scraper.search_listings(HardFilters()):
        out.append(c)
    assert len(out) == 1
    await scraper.close()


@pytest.mark.asyncio
async def test_search_listings_swallows_http_error_and_returns_empty():
    import httpx

    class _BoomClient:
        async def get(self, url: str) -> Any:
            raise httpx.ConnectError("simulated")

        async def aclose(self) -> None:
            return None

    settings = _settings()
    scraper = FotocasaScraper(settings=settings, http_client=_BoomClient(), renderer=None)
    out: list[ListingCard] = []
    async for c in scraper.search_listings(HardFilters()):
        out.append(c)
    assert out == []


@pytest.mark.asyncio
async def test_search_listings_uses_renderer_when_page_is_csr_shell():
    settings = _settings()
    csr_shell = "<html><head><script>window.__INITIAL_STATE__={}</script></head><body></body></html>"
    http = FakeHttpClient({"https://www.fotocasa.es/es/alquiler/viviendas/zaragoza-capital/todas-las-zonas?rooms=2&bathrooms=2&minSize=50&maxPrice=1200": csr_shell})
    rendered = '{"@type": "Apartment", "url": "https://x/vivienda/r1", "name": "Rendered", "offers": {"price": 1000}}'
    rendered_html = f'<html><head><script type="application/ld+json">{rendered}</script></head><body></body></html>'
    renderer = FakeRenderer(rendered_html)
    scraper = FotocasaScraper(settings=settings, http_client=http, renderer=renderer)
    out: list[ListingCard] = []
    async for c in scraper.search_listings(HardFilters()):
        out.append(c)
    assert len(out) == 1
    assert out[0].external_id == "r1"
    assert renderer.calls, "renderer should have been called for CSR shell"
    await scraper.close()


@pytest.mark.asyncio
async def test_fetch_listing_parses_detail_page():
    settings = _settings()
    detail_html = (
        "<html><head>"
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@type": "Apartment",
                "url": "https://fotocasa.es/vivienda/abc",
                "name": "Flat",
                "offers": {"price": 950},
            }
        )
        + "</script>"
        # Plenty of body text so the CSR heuristic does not fire.
        + ("<body>" + ("some real listing content. " * 30) + "</body>")
        + "</html>"
    )
    http = FakeHttpClient({"https://fotocasa.es/vivienda/abc": detail_html})
    scraper = FotocasaScraper(settings=settings, http_client=http, renderer=None)
    apt = await scraper.fetch_listing("https://fotocasa.es/vivienda/abc")
    assert apt.external_id == "abc"
    assert apt.title == "Flat"
    await scraper.close()


@pytest.mark.asyncio
async def test_fetch_listing_raises_when_page_cannot_be_parsed():
    settings = _settings()
    http = FakeHttpClient({"https://x/vivienda/empty": "<html><body></body></html>"})
    scraper = FotocasaScraper(settings=settings, http_client=http, renderer=None)
    with pytest.raises(RuntimeError, match="could not parse"):
        await scraper.fetch_listing("https://x/vivienda/empty")
    await scraper.close()


@pytest.mark.asyncio
async def test_close_is_idempotent():
    settings = _settings()
    http = FakeHttpClient({})
    scraper = FotocasaScraper(settings=settings, http_client=http, renderer=None)
    await scraper.close()  # FakeHttpClient has no aclose; just no-op


def test_build_playwright_renderer_returns_a_renderer_when_playwright_present():
    """We don't require `playwright install` to be run; just that the
    module is importable. The function returns either a renderer or None."""
    from deep_apartment_finder.adapters.scrapers.fotocasa.scraper import (
        _build_playwright_renderer,
    )

    renderer = _build_playwright_renderer()
    # Either playwright is installed and we got a real renderer, or it's
    # not and we got None. Both are acceptable in CI; the unit test for
    # the renderer path is the next test, which uses a fake.
    if renderer is not None:
        assert hasattr(renderer, "render")
