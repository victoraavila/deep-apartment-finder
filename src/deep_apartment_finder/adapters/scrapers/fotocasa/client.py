"""HTTP client used by the Fotocasa scraper.

A single `httpx.AsyncClient` with sane headers and timeouts. Polite
delays are applied between requests by the scraper, not the client.
"""

from __future__ import annotations

import httpx


def build_http_client(user_agent: str, *, timeout: float = 20.0) -> httpx.AsyncClient:
    """Build an `httpx.AsyncClient` configured for polite Fotocasa scraping.

    The UA is configurable so the test suite can pin a deterministic one
    and the user can rotate theirs without code changes.
    """
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
    )
