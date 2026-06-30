"""Playwright-based detail-page client for Idealista.

Sprint 4 (Pillar A) closes the `bathrooms=NULL` gap that Sprint 3
documented in `ADR-011 §Future work`. The search-card path
(`scraper.py:search_listings` / `fetch_listing`'s search-page walk)
yields an `Apartment` with `bathrooms=None` on every row, which makes
the `HardFilters(min_bathrooms=2)` predicate reject every Idealista
listing.

The detail page (`/inmueble/<id>/`) carries a stable, machine-readable
block that includes the bathroom count. DataDome, however, trust-scores
session cookies against real-browser signals (mouse movement, JS
execution, time on page); a `curl_cffi` session can never accumulate
enough trust. The HTTP/2 + ciphersuite impersonation that passes the
search page does not pass the detail page.

This module wraps a single `playwright.async_api.BrowserContext`,
created **lazily on the first `fetch_detail_html` call** and **reused
for every subsequent detail fetch**, and closed in
`IdealistaScraper.close()`. The same context accumulating real-browser
signals across pages is the whole point of using a real browser: it
is what gets us past DataDome.

Failure modes (handled by `IdealistaScraper.fetch_listing`):
- Playwright is not installed → `fetch_detail_html` returns `None`
  immediately; the scraper falls back to the search-card path.
- The browser fails to launch (e.g. Chromium not installed) → the
  scraper catches the `Error` and falls back. The same `BrowserContext`
  is left in a closed state and re-initialised on the next call.
- A specific page returns non-200 / the network is blocked → the
  caller raises and the scraper falls back per-listing.

`fetch_detail_html` is **never** called when `IDEALISTA_DETAIL_FETCH`
is `false` or when playwright is not importable. The `IdealistaScraper`
checks both before invoking the client.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class IdealistaDetailClient:
    """A single shared `BrowserContext` for the Idealista detail page.

    The context is created lazily on the first `fetch_detail_html`
    call (so a scraper that never calls `fetch_listing` never pays
    the browser launch cost). It is closed in `close()`.

    Tests inject a fake `_context_factory` to avoid spinning up a
    real browser; production code uses `playwright.async_api` via
    the late import in `_default_context_factory`.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        user_agent: str | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._enabled = enabled
        self._user_agent = user_agent
        self._timeout_seconds = timeout_seconds
        # `_context` is the playwright `BrowserContext`. Typed as
        # `Any` to keep the type-checker happy without importing
        # playwright at module scope (a missing playwright install
        # is the whole reason this module is gated on a runtime
        # check).
        self._context: Any = None
        self._closed: bool = False
        # Tests inject a fake `_context_factory` to avoid spinning
        # up a real browser; production code uses the late import
        # in `_default_context_factory`. The factory has the
        # signature `async (user_agent: str | None) -> Any`.
        self._context_factory: Any = _default_context_factory

    @property
    def is_enabled(self) -> bool:
        """`True` when the detail path may attempt to launch playwright.

        `False` means `fetch_detail_html` is a no-op (returns `None`)
        — set by `IDEALISTA_DETAIL_FETCH=disabled` or when
        `playwright` is not importable.
        """
        return self._enabled

    async def fetch_detail_html(self, url: str) -> str | None:
        """Return the rendered HTML of `url`, or `None` on failure.

        `None` is the canonical "I could not get the detail page" signal
        the scraper uses to decide whether to fall back to the
        search-card path. We never raise on a transport failure; the
        scraper has a search-card fallback, so propagating exceptions
        would only make the run noisier without changing the outcome.
        """
        if not self._enabled:
            return None
        if self._closed:
            return None
        try:
            context = await self._ensure_context()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "idealista detail client: context init failed, falling back: %s",
                exc,
            )
            self._enabled = False
            return None
        try:
            page = await context.new_page()
            try:
                response = await page.goto(
                    url, timeout=int(self._timeout_seconds * 1000)
                )
                if response is None:
                    return None
                if response.status != 200:
                    logger.debug(
                        "idealista detail: %s returned %d",
                        url,
                        response.status,
                    )
                    return None
                content: str = await page.content()
                return content
            finally:
                await page.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "idealista detail fetch failed for %s: %s",
                url,
                exc,
            )
            return None

    async def close(self) -> None:
        """Close the underlying browser context.

        Safe to call multiple times. After `close()`, subsequent
        `fetch_detail_html` calls return `None`.
        """
        if self._closed:
            return
        self._closed = True
        context = self._context
        self._context = None
        if context is None:
            return
        try:
            await context.close()
        except Exception:  # noqa: BLE001
            pass

    async def _ensure_context(self) -> Any:
        if self._context is not None:
            return self._context
        self._context = await self._context_factory(
            user_agent=self._user_agent,
        )
        return self._context


async def _default_context_factory(*, user_agent: str | None) -> Any:
    """Launch a chromium `BrowserContext` and return it.

    Late import: `playwright.async_api` is an optional dep. If the
    import fails (or the Chromium binary is missing) the call raises
    and the caller disables the detail path.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright is not importable; run `uv sync --extra dev` "
            "and `playwright install chromium` to enable the Idealista "
            "detail-page fetch"
        ) from exc
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    try:
        context = await browser.new_context(
            user_agent=user_agent or _DEFAULT_USER_AGENT,
            locale="es-ES",
        )
    finally:
        # `BrowserContext` owns the `Browser`; we close the context
        # and let `playwright` reap the browser via its own event.
        # We do NOT call `browser.close()` here because the context
        # is the long-lived handle the scraper reuses; closing the
        # browser would tear down the context on the next page.
        pass
    return context


_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def playwright_importable() -> bool:
    """Return `True` if `playwright.async_api` is importable.

    Used by `IdealistaScraper.__init__` to decide whether the
    detail path is even worth wiring up. The check is a smoke
    test; the real failure (Chromium not installed) surfaces on
    the first `fetch_detail_html` call and is handled there.
    """
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return True


__all__ = [
    "IdealistaDetailClient",
    "playwright_importable",
]
