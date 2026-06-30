"""`IdealistaDetailClient` unit tests.

The client owns a single shared `playwright.async_api.BrowserContext`,
created lazily on the first `fetch_detail_html` call and reused for
every subsequent call. The test double here replaces the launch
factory so we can assert the lifecycle (lazy init, single context,
close idempotency, fallback to `None` on transport failure) without
spinning up a real browser.

What's covered:
- The context is created lazily on the first fetch (no launch
  before that).
- The same context is reused for every subsequent fetch.
- A launch failure disables the client (`is_enabled` becomes
  `False`) and `fetch_detail_html` returns `None` for the rest
  of the run.
- A per-page transport failure returns `None` (not an exception).
- `close()` is idempotent and shuts the context down cleanly.
- A `disabled` client never launches a context.
"""

from __future__ import annotations

from typing import Any

import pytest

from deep_apartment_finder.adapters.scrapers.idealista.detail_client import (
    IdealistaDetailClient,
    _BrowserResources,
)


class _FakeResponse:
    def __init__(self, *, status: int = 200) -> None:
        self.status = status


class _FakePage:
    """Records `goto` and returns canned content; the test owns the
    page's `content` and `status` per URL via `_pages[url]`.
    """

    def __init__(self, owner: _FakeContext) -> None:
        self._owner = owner
        self.closed = False
        self.goto_calls: list[str] = []

    async def goto(self, url: str, timeout: int = 0) -> _FakeResponse:
        self.goto_calls.append(url)
        entry = self._owner.responses.get(url, _FakeResponse(status=200))
        self._next_url = url
        self._next_status = entry.status
        return entry

    async def content(self) -> str:
        url = getattr(self, "_next_url", "")
        return self._owner.contents.get(url, "<html></html>")

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    """A `playwright.async_api.BrowserContext` test double.

    The class records every `new_page()` call and exposes
    `contents` / `responses` for the test to control the per-URL
    behaviour. `close()` records the call so the test can assert
    idempotency.
    """

    def __init__(self) -> None:
        self.new_page_calls = 0
        self.closed = False
        self.contents: dict[str, str] = {}
        self.responses: dict[str, _FakeResponse] = {}
        self.pages: list[_FakePage] = []

    async def new_page(self) -> _FakePage:
        self.new_page_calls += 1
        page = _FakePage(self)
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakePlaywright:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def _factory_returning(ctx: _FakeContext):
    """Build a closure that returns `ctx` when awaited."""

    async def _factory(*, user_agent: str | None) -> _FakeContext:
        ctx._user_agent = user_agent
        return ctx

    return _factory


def _factory_returning_resources(
    ctx: _FakeContext,
    browser: _FakeBrowser,
    playwright: _FakePlaywright,
):
    async def _factory(*, user_agent: str | None) -> _BrowserResources:
        ctx._user_agent = user_agent
        return _BrowserResources(
            playwright=playwright,
            browser=browser,
            context=ctx,
        )

    return _factory


def _factory_raising(exc: BaseException):
    async def _factory(*, user_agent: str | None) -> Any:
        raise exc

    return _factory


# --- lazy init / single shared context ------------------------------------


@pytest.mark.asyncio
async def test_context_is_created_lazily_on_first_fetch() -> None:
    """No launch before the first `fetch_detail_html`."""
    ctx = _FakeContext()
    client = IdealistaDetailClient(enabled=True)
    client._context_factory = _factory_returning(ctx)  # type: ignore[attr-defined]
    # No fetch yet: no context.
    assert ctx.new_page_calls == 0
    # First fetch: launch.
    html = await client.fetch_detail_html("https://x/1")
    assert html is not None
    assert ctx.new_page_calls == 1
    await client.close()


@pytest.mark.asyncio
async def test_same_context_is_reused_across_fetches() -> None:
    """The same `BrowserContext` is reused for every detail fetch in
    the run. This is the whole point of using a real browser: the
    context accumulates DataDome-trust-scoring signals (cookies,
    JS-execution evidence) over time. Re-launching per fetch
    would defeat the purpose.
    """
    ctx = _FakeContext()
    client = IdealistaDetailClient(enabled=True)
    client._context_factory = _factory_returning(ctx)  # type: ignore[attr-defined]
    for url in ("https://x/1", "https://x/2", "https://x/3"):
        ctx.contents[url] = f"<html>{url}</html>"
        await client.fetch_detail_html(url)
    # One context was created, three pages were opened on it.
    assert ctx.new_page_calls == 3
    await client.close()


@pytest.mark.asyncio
async def test_concurrent_fetches_share_one_context_init() -> None:
    """Parallel tool calls must not launch one browser per listing."""
    import asyncio

    ctx = _FakeContext()
    launches = {"n": 0}

    async def _factory(*, user_agent: str | None) -> _FakeContext:
        launches["n"] += 1
        await asyncio.sleep(0.01)
        return ctx

    client = IdealistaDetailClient(enabled=True)
    client._context_factory = _factory  # type: ignore[attr-defined]
    urls = [f"https://x/{i}" for i in range(5)]
    for url in urls:
        ctx.contents[url] = f"<html>{url}</html>"

    out = await asyncio.gather(*(client.fetch_detail_html(url) for url in urls))

    assert len([html for html in out if html is not None]) == 5
    assert launches["n"] == 1
    assert ctx.new_page_calls == 5
    await client.close()


@pytest.mark.asyncio
async def test_concurrent_launch_failure_disables_after_one_attempt() -> None:
    """Missing Chromium should produce one failed launch, not N warnings."""
    import asyncio

    launches = {"n": 0}

    async def _factory(*, user_agent: str | None) -> Any:
        launches["n"] += 1
        await asyncio.sleep(0.01)
        raise RuntimeError("Chromium not installed")

    client = IdealistaDetailClient(enabled=True)
    client._context_factory = _factory  # type: ignore[attr-defined]

    out = await asyncio.gather(
        *(client.fetch_detail_html(f"https://x/{i}") for i in range(5))
    )

    assert out == [None, None, None, None, None]
    assert launches["n"] == 1
    assert client.is_enabled is False


# --- launch failure -> disable --------------------------------------------


@pytest.mark.asyncio
async def test_launch_failure_disables_client() -> None:
    """A launch failure (e.g. Chromium binary missing) must disable
    the client for the rest of the run; the next call returns
    `None` immediately without retrying. This is the
    "soft 404" / fallback path the sprint spec documents.
    """
    client = IdealistaDetailClient(enabled=True)
    client._context_factory = _factory_raising(  # type: ignore[attr-defined]
        RuntimeError("Chromium not installed")
    )
    out = await client.fetch_detail_html("https://x/1")
    assert out is None
    assert client.is_enabled is False
    # Second call: still `None`, no re-launch attempt.
    out2 = await client.fetch_detail_html("https://x/2")
    assert out2 is None
    assert client.is_enabled is False


# --- per-page failure -> None, not raise ----------------------------------


@pytest.mark.asyncio
async def test_per_page_transport_failure_returns_none() -> None:
    """A 403 / network error on a specific page returns `None`
    (the canonical "could not get the detail page" signal) rather
    than raising. The scraper falls back to the search-card path
    for that one listing.
    """
    ctx = _FakeContext()
    ctx.responses["https://x/blocked"] = _FakeResponse(status=403)
    client = IdealistaDetailClient(enabled=True)
    client._context_factory = _factory_returning(ctx)  # type: ignore[attr-defined]
    out = await client.fetch_detail_html("https://x/blocked")
    assert out is None
    # Client is still enabled for the next page.
    assert client.is_enabled is True
    ctx.contents["https://x/ok"] = "<html>ok</html>"
    out_ok = await client.fetch_detail_html("https://x/ok")
    assert out_ok == "<html>ok</html>"
    await client.close()


# --- disabled client ------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_client_never_launches() -> None:
    """When `enabled=False` (the `--no-detail-fetch` CLI flag or
    `IDEALISTA_DETAIL_FETCH=disabled`), `fetch_detail_html` is a
    no-op. No context is ever created.
    """
    client = IdealistaDetailClient(enabled=False)
    out = await client.fetch_detail_html("https://x/1")
    assert out is None
    # The internal context is still None (never instantiated).
    assert client._context is None  # type: ignore[attr-defined]
    await client.close()


# --- close idempotency ----------------------------------------------------


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    """`close()` can be called multiple times. The second call is a
    no-op; the underlying context is closed at most once.
    """
    ctx = _FakeContext()
    client = IdealistaDetailClient(enabled=True)
    client._context_factory = _factory_returning(ctx)  # type: ignore[attr-defined]
    ctx.contents["https://x/1"] = "<html>1</html>"
    await client.fetch_detail_html("https://x/1")
    await client.close()
    assert ctx.closed is True


@pytest.mark.asyncio
async def test_close_closes_browser_and_playwright_resources() -> None:
    ctx = _FakeContext()
    browser = _FakeBrowser()
    playwright = _FakePlaywright()
    client = IdealistaDetailClient(enabled=True)
    client._context_factory = _factory_returning_resources(  # type: ignore[attr-defined]
        ctx,
        browser,
        playwright,
    )
    await client.fetch_detail_html("https://x/1")

    await client.close()

    assert ctx.closed is True
    assert browser.closed is True
    assert playwright.stopped is True
    # Second close: still safe.
    await client.close()
    assert ctx.closed is True


@pytest.mark.asyncio
async def test_close_before_any_fetch_is_safe() -> None:
    """`close()` is a no-op when no context was ever created."""
    client = IdealistaDetailClient(enabled=True)
    await client.close()
    # And a subsequent fetch returns `None` because the client is
    # closed.
    out = await client.fetch_detail_html("https://x/1")
    assert out is None


@pytest.mark.asyncio
async def test_close_does_not_swallow_context_close_errors() -> None:
    """If the context's own `close()` raises, the client's `close()`
    does NOT propagate the exception — the run is wrapping up and
    we want every scraper to be able to close cleanly.
    """

    class _RaisingContext(_FakeContext):
        async def close(self) -> None:
            await super().close()
            raise RuntimeError("browser already closed")

    ctx = _RaisingContext()
    client = IdealistaDetailClient(enabled=True)
    client._context_factory = _factory_returning(ctx)  # type: ignore[attr-defined]
    await client.fetch_detail_html("https://x/1")
    await client.close()  # must not raise
    assert ctx.closed is True
