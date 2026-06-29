"""HTTP client used by the Idealista scraper.

The Idealista search portal is protected by **DataDome** (a bot manager
that inspects TLS fingerprint, HTTP/2 settings, header order, and
session signals). A plain `httpx` client gets a 403 + a captcha
interstitial on the very first request. `curl_cffi` impersonates a
real Chrome 131 client and passes the fingerprint check; the response
is the SSR'd search HTML.

Detail pages (`/inmueble/<id>/`) are blocked for the same client
because DataDome trust-scores session cookies that have accumulated
real-browser signals (mouse movement, JS execution, time on page).
We accept that limitation in Sprint 3 and let `fetch_listing` return
the listing data we already harvested from the search card — title,
price, address, rooms, m², description, photo. `lat`/`lng` and
`bathrooms` will be `None` until a later sprint adds a playwright
upgrade for the detail path.
"""

from __future__ import annotations

from typing import Any, cast

from curl_cffi import requests as cf_requests

_DEFAULT_TIMEOUT: float = 20.0
# `curl_cffi.Session`'s `impersonate` parameter has a tight Literal type
# we don't want to import just to type-annotate; we narrow at the call
# site.
_DEFAULT_IMPERSONATE: str = "chrome131"


def build_http_client(
    *, impersonate: str = _DEFAULT_IMPERSONATE, timeout: float = _DEFAULT_TIMEOUT
) -> cf_requests.Session:
    """Build a `curl_cffi` Session configured to look like a real Chrome.

    `impersonate` is a `curl_cffi` browser profile name (e.g.
    `chrome131`, `chrome124`). Tested profiles that pass DataDome on
    the Idealista search endpoint: `chrome131`, `chrome124`. Profiles
    that 403: `chrome142` and newer (the ciphersuite / ALPN profile
    is not yet on DataDome's known-good list).

    We use `cf_requests.Session` rather than the raw `requests.get(...)`
    shortcut so the session reuses TLS keys, the DataDome cookie, and
    `cf_clearance` cookies across paged fetches within one
    `search_listings` call. Without session reuse, page 2 onwards gets
    403'd because DataDome has not yet trust-scored a fresh TLS key.
    """
    session: cf_requests.Session = cf_requests.Session(impersonate=cast(Any, impersonate))
    session.headers.update(
        {
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }
    )
    # The timeout is enforced per-request below; `cf_requests.Session`
    # doesn't have a top-level timeout constructor.
    session._default_timeout = timeout  # type: ignore[attr-defined]
    return session


def request_with_timeout(
    session: cf_requests.Session, url: str, **kwargs: Any
) -> cf_requests.Response:
    """GET `url` with the session's default timeout, unless overridden.

    Centralises the timeout so callers don't have to remember it.
    """
    kwargs.setdefault("timeout", getattr(session, "_default_timeout", _DEFAULT_TIMEOUT))
    kwargs.setdefault("allow_redirects", True)
    result: cf_requests.Response = session.get(url, **kwargs)
    return result


__all__ = ["build_http_client", "request_with_timeout"]
