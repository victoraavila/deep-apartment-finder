"""`run_scrapers` tool tests.

The tool fires the two scraper subagent graphs concurrently via
`asyncio.gather` and returns a single combined handoff. The
tests use a fake `Runnable` per subagent so we can drive both the
happy path and the partial-failure path without spinning up real
LLM sessions.

What's covered:
- Both subagents succeed → combined handoff has both `ok`
  entries; `errors` is absent.
- One subagent raises → the other completes; the failure is
  captured in that subagent's `{"status": "error", "error": ...}`
  entry and surfaced in the top-level `errors` list. The
  surviving subagent's handoff is intact.
- The tool rejects an empty `runnables` list at construction time.
- The tool receives a single `brief` and forwards it to both
  subagents in the same `messages` state.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from deep_apartment_finder.tools.orchestrator.run_scrapers import (
    _gather_subagents,
    make_run_scrapers_tool,
)


class _FakeRunnable:
    """A `Runnable` test double that records the state it was
    invoked with and returns a canned result.

    `delay` simulates subagent LLM latency; the integration test
    for parallel subagent overlap relies on the delay to assert
    that the two sessions overlap in time.
    """

    def __init__(
        self,
        *,
        name: str,
        summary: str = "fake summary",
        delay: float = 0.0,
        raise_after_delay: BaseException | None = None,
    ) -> None:
        self.name = name
        self._summary = summary
        self._delay = delay
        self._raise_after_delay = raise_after_delay
        self.invocations: list[dict[str, Any]] = []

    async def ainvoke(self, state: dict[str, Any], config: Any = None) -> dict[str, Any]:
        self.invocations.append({"state": state, "config": config})
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        if self._raise_after_delay is not None:
            raise self._raise_after_delay
        return {
            "messages": [AIMessage(content=self._summary)],
        }


# --- happy path ---------------------------------------------------------


@pytest.mark.asyncio
async def test_run_scrapers_combines_both_handoffs() -> None:
    """Both subagents succeed. The combined handoff has one entry
    per subagent with `status: "ok"` and the last AI text surfaced
    as `summary`. `errors` is absent.
    """
    foto = _FakeRunnable(name="fotocasa_scraper", summary="foto done")
    ideal = _FakeRunnable(name="idealista_scraper", summary="ideal done")
    tool = make_run_scrapers_tool(
        [("fotocasa_scraper", foto), ("idealista_scraper", ideal)]
    )

    out = await tool.arun({"brief": "find me a flat"})
    parsed = json.loads(out)
    assert parsed["fotocasa_scraper"] == {
        "status": "ok",
        "summary": "foto done",
    }
    assert parsed["idealista_scraper"] == {
        "status": "ok",
        "summary": "ideal done",
    }
    assert "errors" not in parsed


@pytest.mark.asyncio
async def test_run_scrapers_forwards_same_brief_to_both_subagents() -> None:
    """The same `brief` argument is forwarded to every subagent.
    Each subagent's `messages[0]` is a `HumanMessage` with the
    brief as content.
    """
    foto = _FakeRunnable(name="fotocasa_scraper")
    ideal = _FakeRunnable(name="idealista_scraper")
    tool = make_run_scrapers_tool(
        [("fotocasa_scraper", foto), ("idealista_scraper", ideal)]
    )

    await tool.arun({"brief": "Plan a Sprint 4 run for Zaragoza."})

    for runnable in (foto, ideal):
        assert len(runnable.invocations) == 1
        state = runnable.invocations[0]["state"]
        assert "messages" in state
        assert len(state["messages"]) == 1
        msg = state["messages"][0]
        # The forwarded message is a `HumanMessage` with the brief.
        assert msg.content == "Plan a Sprint 4 run for Zaragoza."


# --- partial failure -----------------------------------------------------


@pytest.mark.asyncio
async def test_run_scrapers_partial_failure_captures_error() -> None:
    """One subagent raises; the other completes. The combined
    handoff has the survivor's `ok` entry and the loser's
    `error` entry, and surfaces the error string in the
    top-level `errors` list. The orchestrator decides what to do
    with the partial result.
    """
    foto = _FakeRunnable(name="fotocasa_scraper", summary="foto done")
    ideal = _FakeRunnable(
        name="idealista_scraper",
        raise_after_delay=RuntimeError("DataDome blocked the run"),
    )
    tool = make_run_scrapers_tool(
        [("fotocasa_scraper", foto), ("idealista_scraper", ideal)]
    )

    out = await tool.arun({"brief": "x"})
    parsed = json.loads(out)
    assert parsed["fotocasa_scraper"]["status"] == "ok"
    assert parsed["idealista_scraper"]["status"] == "error"
    assert "DataDome blocked" in parsed["idealista_scraper"]["error"]
    assert any("idealista_scraper" in e for e in parsed["errors"])


@pytest.mark.asyncio
async def test_run_scrapers_both_fail_still_returns_combined() -> None:
    """Both subagents raise. The combined handoff has two `error`
    entries and the top-level `errors` list mentions both. The
    orchestrator sees the run is broken.
    """
    foto = _FakeRunnable(name="fotocasa_scraper", raise_after_delay=RuntimeError("a"))
    ideal = _FakeRunnable(
        name="idealista_scraper", raise_after_delay=RuntimeError("b")
    )
    tool = make_run_scrapers_tool(
        [("fotocasa_scraper", foto), ("idealista_scraper", ideal)]
    )

    out = await tool.arun({"brief": "x"})
    parsed = json.loads(out)
    assert parsed["fotocasa_scraper"]["status"] == "error"
    assert parsed["idealista_scraper"]["status"] == "error"
    assert len(parsed["errors"]) == 2


@pytest.mark.asyncio
async def test_run_scrapers_does_not_cancel_on_error() -> None:
    """`asyncio.gather(return_exceptions=True)` ensures a failure
    in one subagent does not cancel the other. The survivor's
    handoff is intact.
    """
    foto = _FakeRunnable(name="fotocasa_scraper", summary="foto done")
    ideal = _FakeRunnable(
        name="idealista_scraper", raise_after_delay=RuntimeError("boom")
    )

    out = await _gather_subagents(
        [("fotocasa_scraper", foto), ("idealista_scraper", ideal)],
        brief="x",
    )
    assert out["fotocasa_scraper"]["status"] == "ok"
    assert out["idealista_scraper"]["status"] == "error"
    assert "boom" in out["idealista_scraper"]["error"]


# --- factory validation -------------------------------------------------


def test_run_scrapers_rejects_empty_runnable_list() -> None:
    """The factory raises on an empty runnable list — there is
    nothing to run, so the configuration is a bug, not a runtime
    error.
    """
    with pytest.raises(ValueError, match="at least one"):
        make_run_scrapers_tool([])


# --- single-portal support -----------------------------------------------


@pytest.mark.asyncio
async def test_run_scrapers_with_single_subagent() -> None:
    """When only one portal is wired (Sprint 1/2 compatibility),
    `run_scrapers` works with a single subagent. The combined
    handoff has just that one entry.
    """
    only = _FakeRunnable(name="fotocasa_scraper", summary="solo")
    tool = make_run_scrapers_tool([("fotocasa_scraper", only)])
    out = await tool.arun({"brief": "x"})
    parsed = json.loads(out)
    assert parsed["fotocasa_scraper"] == {"status": "ok", "summary": "solo"}
    assert "errors" not in parsed
