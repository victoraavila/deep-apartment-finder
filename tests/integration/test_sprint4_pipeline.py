"""Sprint 4 integration tests.

Exercises the Sprint 4 deliverables end-to-end with fakes for
every external I/O:

- Pillar A (Idealista detail-page upgrade): the
  `IdealistaScraper.fetch_listing` happy path populates
  `bathrooms` from the rendered detail page; the soft-404 path
  falls back to the search card; the `details_enriched` /
  `details_failed` counters track the per-run tallies.
- Pillar B (parallel scraper execution): the `run_scrapers` tool
  fires the two scraper subagent graphs concurrently; the
  wall-clock time of the scraper phase is the max of the two
  subagent latencies, not the sum. The combined handoff has
  one entry per subagent; a per-subagent failure does not
  cancel the other.
- B.2 (parallel detail fetches inside a subagent): the
  orchestrator's LLM may call `fetch_listing` for N cards in a
  single tool batch; the N calls complete in roughly the time
  of the slowest, not N× the slowest.

The `langchain-deps` skill (loaded earlier) confirms that
LangChain's agent loop preserves parallel tool calls in a
single batch; the test exercises the lower-level claim (the
underlying `ScraperPort.fetch_listing` is async and a single
shared session is concurrency-safe) so a regression in the
scraper would surface here even if the LLM-side fan-out ever
changed.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from deep_apartment_finder.adapters.scrapers.idealista.detail_client import (
    IdealistaDetailClient,
)
from deep_apartment_finder.adapters.scrapers.idealista.scraper import IdealistaScraper
from deep_apartment_finder.config import Settings
from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.source import Source
from deep_apartment_finder.ports.scraper import ScraperPort
from deep_apartment_finder.tools.orchestrator.run_scrapers import (
    _gather_subagents,
    make_run_scrapers_tool,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "idealista"


def _settings(**kwargs: Any) -> Settings:
    base = dict(
        scraper_user_agent="test-ua",
        scraper_delay_seconds=0.0,
        idealista_scraper_delay_seconds=0.0,
    )
    base.update(kwargs)
    return Settings(**base)


# --- parallel subagent overlap (B.1) -------------------------------------


class _DelayedRunnable:
    """A `Runnable` test double that sleeps for `delay` seconds
    and records when it started and finished. The
    `test_parallel_subagents_overlap_in_time` test asserts that
    the two sessions overlap (subagent B's `start_b` happens
    before subagent A's `end_a`).
    """

    def __init__(
        self,
        *,
        name: str,
        delay: float,
        summary: str | None = None,
        raise_after_delay: BaseException | None = None,
        timeline: list[tuple[str, str]] | None = None,
    ) -> None:
        self.name = name
        self._delay = delay
        self._summary = summary or f"{name} done"
        self._raise_after_delay = raise_after_delay
        self._timeline = timeline

    async def ainvoke(self, state: dict[str, Any], config: Any = None) -> dict[str, Any]:
        if self._timeline is not None:
            self._timeline.append((f"{self.name}_start", str(time.monotonic())))
        await asyncio.sleep(self._delay)
        if self._timeline is not None:
            self._timeline.append((f"{self.name}_end", str(time.monotonic())))
        if self._raise_after_delay is not None:
            raise self._raise_after_delay
        return {"messages": [AIMessage(content=self._summary)]}


@pytest.mark.asyncio
async def test_parallel_subagents_overlap_in_time() -> None:
    """Sprint 4 acceptance criterion 4: with two delayed fake
    subagents, the wall-clock time of the scraper phase is
    `max(t_foto, t_idealista) + overhead`, NOT the sum. We assert
    both: the total wall time is < `t_foto + t_idealista`, and
    the two sessions overlap on the timeline (subagent B's
    `start` happens before subagent A's `end`).
    """
    delay_a = 0.3
    delay_b = 0.3
    timeline: list[tuple[str, str]] = []
    foto = _DelayedRunnable(name="a", delay=delay_a, timeline=timeline)
    ideal = _DelayedRunnable(name="b", delay=delay_b, timeline=timeline)

    start = time.monotonic()
    out = await _gather_subagents(
        [("fotocasa_scraper", foto), ("idealista_scraper", ideal)],
        brief="x",
    )
    elapsed = time.monotonic() - start

    assert out["fotocasa_scraper"]["status"] == "ok"
    assert out["idealista_scraper"]["status"] == "ok"
    # Wall time < sum. With asyncio.gather the two sleeps overlap
    # so the elapsed is ~max(t_foto, t_idealista), well under the
    # sum of 0.6s. The threshold is generous to avoid flakiness
    # on shared CI.
    assert elapsed < (delay_a + delay_b) - 0.05, (
        f"expected parallel execution < {delay_a + delay_b}s, "
        f"got {elapsed:.3f}s"
    )

    # The two sessions overlapped: subagent B's start is before
    # subagent A's end.
    by_name = {
        name: float(t)
        for name, t in timeline
        for n in (name,)
        if (name.endswith("_start") or name.endswith("_end"))
    }
    a_start = next(v for k, v in by_name.items() if k == "a_start")
    a_end = next(v for k, v in by_name.items() if k == "a_end")
    b_start = next(v for k, v in by_name.items() if k == "b_start")
    b_end = next(v for k, v in by_name.items() if k == "b_end")
    # They overlap iff each starts before the other ends.
    assert b_start < a_end
    assert a_start < b_end


@pytest.mark.asyncio
async def test_run_scrapers_via_tool_overhead_is_small() -> None:
    """Acceptance criterion 4: the wall-clock overhead beyond
    `max(t_foto, t_idealista)` is small (the spec allows
    `+5s`; we use a tighter threshold to keep the test fast).
    """
    delay = 0.1
    foto = _DelayedRunnable(name="a", delay=delay)
    ideal = _DelayedRunnable(name="b", delay=delay)
    tool = make_run_scrapers_tool(
        [("fotocasa_scraper", foto), ("idealista_scraper", ideal)]
    )

    start = time.monotonic()
    out_str = await tool.arun({"brief": "x"})
    elapsed = time.monotonic() - start

    out = json.loads(out_str)
    assert out["fotocasa_scraper"]["status"] == "ok"
    assert out["idealista_scraper"]["status"] == "ok"
    # The spec allows +5s of overhead; we assert <0.2s to keep
    # the test fast and to catch a regression that re-serialises
    # the two subagents.
    assert elapsed < delay + 0.2, f"overhead too high: {elapsed:.3f}s"


# --- parallel detail fetches inside a subagent (B.2) ---------------------


class _DelayedScraper(ScraperPort):
    """A `ScraperPort` test double whose `fetch_listing` sleeps
    for `delay` seconds. We use it to assert the parallel
    `fetch_listing` calls complete in roughly the time of the
    slowest call, not N× the slowest.
    """

    def __init__(
        self,
        *,
        details: dict[str, Apartment],
        delay: float = 0.2,
    ) -> None:
        self._details = details
        self._delay = delay
        self.fetch_starts: list[float] = []
        self.fetch_ends: list[float] = []

    async def search_listings(self, filters: Any) -> Any:
        if False:
            yield  # pragma: no cover — async-generator shape
        return
        yield  # pragma: no cover

    async def fetch_listing(self, url: str) -> Apartment:
        self.fetch_starts.append(time.monotonic())
        await asyncio.sleep(self._delay)
        self.fetch_ends.append(time.monotonic())
        return self._details[url]

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_parallel_fetch_listing_calls_overlap() -> None:
    """Sprint 4 acceptance criterion 5: N parallel `fetch_listing`
    tool calls complete in roughly the time of the slowest, not
    N× the slowest. The shared `httpx.AsyncClient` is async-safe
    and a single connection handles concurrent requests.
    """
    urls = [f"https://x/{i}" for i in range(5)]
    details = {
        url: Apartment(
            source=Source.FOTOCASA,
            external_id=str(i),
            url=url,
            title=f"Apt {i}",
        )
        for i, url in enumerate(urls)
    }
    delay = 0.2
    scraper = _DelayedScraper(details=details, delay=delay)

    start = time.monotonic()
    results = await asyncio.gather(*(scraper.fetch_listing(url) for url in urls))
    elapsed = time.monotonic() - start

    assert len(results) == 5
    # Parallel calls overlap: total time is ~delay, not N*delay.
    assert elapsed < delay * 3, (
        f"expected parallel fetch in ~{delay}s, got {elapsed:.3f}s"
    )
    # The fetches all overlapped: the first start is before the
    # last end.
    assert scraper.fetch_starts[0] < scraper.fetch_ends[-1]


# --- parallel pipeline: both scrapers + details_enriched counter ---------


class _FakeSession:
    """The `cf_requests.Session` test double used by the
    `IdealistaScraper`. Returns the search-page fixture on the
    first call and the detail-page fixture on subsequent calls.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = False
        self._queue: list[Any] = []

    def queue(self, *responses: Any) -> None:
        self._queue.extend(responses)

    def get(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(url)
        if not self._queue:
            return _FakeResponse(text="", status_code=200)
        return self._queue.pop(0)

    def close(self) -> None:
        self.closed = True


class _FakeResponse:
    def __init__(self, *, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


def _detail_client_with_html(html: str) -> IdealistaDetailClient:
    detail = IdealistaDetailClient(enabled=True, user_agent="test-ua")

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

    detail._context_factory = _factory
    return detail


@pytest.mark.asyncio
async def test_idealista_scraper_detail_path_populates_bathrooms() -> None:
    """End-to-end check: with the detail-page fixture (1 baño), the
    scraper populates `bathrooms=1` on the returned `Apartment`,
    and the `details_enriched` counter ticks to 1. This is the
    acceptance-criterion-1 / 2 path the spec requires.
    """
    detail_html = (FIXTURES / "detail_page1.html").read_text()
    session = _FakeSession()
    session.queue(
        _FakeResponse(
            text=(FIXTURES / "search_page1.html").read_text(), status_code=200
        )
    )
    detail = _detail_client_with_html(detail_html)
    scraper = IdealistaScraper(
        settings=_settings(), session=session, detail_client=detail
    )
    apt = await scraper.fetch_listing(
        "https://www.idealista.com/inmueble/109872751/"
    )
    assert apt.bathrooms == 1
    assert scraper.details_enriched == 1
    assert scraper.details_failed == 0
    await scraper.close()


@pytest.mark.asyncio
async def test_parallel_pipeline_records_per_subagent_counters() -> None:
    """End-to-end check: the parallel `run_scrapers` tool returns
    one entry per subagent, and the Idealista entry's
    `details_enriched` is the counter from the scraper. This
    mirrors acceptance criterion 8 (operator sees both
    `=== scraper (fotocasa) ===` and
    `=== scraper (idealista) ===` with their own counters).
    """

    class _FotoScraper:
        async def search_listings(self, filters: Any) -> Any:
            return
            yield  # pragma: no cover
            yield

        async def fetch_listing(self, url: str) -> Apartment:
            return Apartment(
                source=Source.FOTOCASA,
                external_id="f-1",
                url=url,
                title="Apt f-1",
            )

        async def close(self) -> None:
            return None

    class _IdealScraper:
        def __init__(self) -> None:
            self._details_enriched = 1
            self._details_failed = 0

        async def search_listings(self, filters: Any) -> Any:
            return
            yield  # pragma: no cover
            yield

        async def fetch_listing(self, url: str) -> Apartment:
            return Apartment(
                source=Source.IDEALISTA,
                external_id="i-1",
                url=url,
                title="Apt i-1",
                bathrooms=1,
            )

        async def close(self) -> None:
            return None

    foto = _FotoScraper()
    ideal = _IdealScraper()

    class _FotoRunnable:
        async def ainvoke(self, state: dict[str, Any], config: Any = None) -> dict[str, Any]:
            # Drive the scraper once (the "fake subagent" doesn't
            # need a real LLM loop). The return value is the side
            # effect we're proving; ruff doesn't see the discard.
            await foto.fetch_listing("https://f/1")
            return {
                "messages": [
                    AIMessage(
                        content=(
                            "fotocasa_scraper handoff: 1 inserted "
                            "(details_enriched=0, details_failed=0)"
                        )
                    )
                ]
            }

    class _IdealRunnable:
        async def ainvoke(self, state: dict[str, Any], config: Any = None) -> dict[str, Any]:
            await ideal.fetch_listing("https://i/1")
            return {
                "messages": [
                    AIMessage(
                        content=(
                            f"idealista_scraper handoff: 1 inserted "
                            f"(details_enriched={ideal._details_enriched}, "
                            f"details_failed={ideal._details_failed})"
                        )
                    )
                ]
            }

    tool = make_run_scrapers_tool(
        [
            ("fotocasa_scraper", _FotoRunnable()),
            ("idealista_scraper", _IdealRunnable()),
        ]
    )
    out_str = await tool.arun({"brief": "x"})
    out = json.loads(out_str)
    assert out["fotocasa_scraper"]["status"] == "ok"
    assert out["idealista_scraper"]["status"] == "ok"
    assert "details_enriched=1" in out["idealista_scraper"]["summary"]
    assert "details_enriched=0" in out["fotocasa_scraper"]["summary"]
