"""Shared behaviour for portal scrapers.

Right now it just owns the polite-delay helper and a CSR fallback
hook. Concrete scrapers (only `FotocasaScraper` in Sprint 1) use these
primitives so we don't repeat ourselves when a second adapter is added
in Sprint 3.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

logger = logging.getLogger(__name__)


class RendersJSPage(Protocol):
    """Anything that knows how to render a CSR page with playwright."""

    async def render(self, url: str) -> str: ...


async def polite_sleep(seconds: float) -> None:
    """Sleep `seconds` without blocking the event loop. Negative is a no-op."""
    if seconds > 0:
        await asyncio.sleep(seconds)


def is_csr_shell(html: str, *, min_text_length: int = 200) -> bool:
    """Heuristic: did the server hand us an empty SPA shell?

    Returns `True` if the page has no useful text content (lots of script
    tags but the body is empty / placeholder). The scraper falls back to
    playwright when this is the case.
    """
    text = _strip_tags(html)
    return len(text) < min_text_length


def _strip_tags(html: str) -> str:
    """Cheap HTML-to-text: just drop anything between < and >. Good enough
    for the CSR heuristic — we don't care about exact whitespace."""
    out: list[str] = []
    inside = False
    for ch in html:
        if ch == "<":
            inside = True
        elif ch == ">":
            inside = False
        elif not inside:
            out.append(ch)
    return "".join(out)


async def with_csr_fallback(
    url: str,
    *,
    fetcher: Callable[[str], Awaitable[str]],
    renderer: RendersJSPage | None,
) -> str:
    """Fetch `url`; if the result looks like a CSR shell and a `renderer`
    is configured, render via JS and return the rendered HTML.

    `fetcher` is the plain httpx call. `renderer` is the optional
    playwright-backed path. The whole thing is a no-op for SSR pages.
    """
    html = await fetcher(url)
    if renderer is not None and is_csr_shell(html):
        logger.info("page looks like a CSR shell; rendering with playwright: %s", url)
        try:
            return await renderer.render(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("playwright render failed for %s: %s", url, exc)
    return html
