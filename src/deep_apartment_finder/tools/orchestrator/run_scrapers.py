"""`run_scrapers` tool — fires the two scraper subagents concurrently.

Sprint 4 (Pillar B.1). Sprint 1+3 ran the scraper subagents
sequentially: the orchestrator's LLM would issue two `task` tool
calls, one at a time, and the two scraper subagents would each
take 2-3 minutes. Sprint 4 replaces that with a single
`run_scrapers` tool that calls both subagent graphs in parallel
via `asyncio.gather`. The wall-time saving is the slower of the
two subagents, not the sum.

The tool is a normal LangChain `@tool` function the orchestrator
calls. It owns the **already-compiled** subagent runnables (built
in `agent/orchestrator.build_orchestrator` via `create_sub_agent`),
so the tool can `.ainvoke(...)` them directly — it does not go
through the deepagents `task` tool. Each subagent still owns its
own LLM session, its own scraper, its own filesystem subtree; the
only thing that changed is the scheduling.

The tool is the orchestrator's only entry point for the scraper
phase. The prompt in `subagents/prompts/orchestrator.md` is updated
to call `run_scrapers` exactly once per run, instead of two `task`
calls. The `task` tool itself stays registered (some operators
may want to debug a single portal in isolation; see ADR-013).

Concurrency model
-----------------
- Inside the tool, we `asyncio.gather(...)` the two subagent
  runs. One Python event loop; one Postgres pool (which is already
  async-safe — `PostgresApartmentRepository.upsert` uses
  `async with self._pool.acquire()`); two separate LLM sessions;
  two separate HTTP / browser sessions.
- A failure in one subagent does not cancel the other. We
  `return_exceptions=True` and surface the exception in the
  combined handoff so the orchestrator can decide how to react.
- The `RecordingRunObserver` already handles out-of-order phase
  events: it accumulates counts and persists the union at
  `phase_end`. The two `=== scraper (fotocasa) ===` and
  `=== scraper (idealista) ===` blocks interleave on stderr as the
  two subagents make progress.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, tool

logger = logging.getLogger(__name__)


async def _gather_subagents(
    runnables: list[tuple[str, Runnable]],
    *,
    brief: str,
) -> dict[str, Any]:
    """Invoke each subagent concurrently and return the combined handoff.

    `runnables` is a list of `(name, runnable)` pairs. Each runnable
    is invoked with a `{"messages": [HumanMessage(brief)]}` state.
    The brief is the orchestrator's hard-filter / city brief; it
    is the same one the LLM would otherwise have used in its
    `task` call.

    A failure in one subagent does not cancel the others. The
    result has shape:

        {
            "<name>": {"status": "ok", "summary": "<last AI text>"}
                       | {"status": "error", "error": "<repr>"},
            "errors": ["<name>: <repr>", ...]
        }
    """
    from langchain_core.messages import HumanMessage

    state: dict[str, Any] = {"messages": [HumanMessage(content=brief)]}

    async def _run_one(name: str, runnable: Runnable) -> tuple[str, dict[str, Any]]:
        try:
            result = await runnable.ainvoke(state)
        except BaseException as exc:  # noqa: BLE001
            logger.warning("run_scrapers: %s raised %r", name, exc)
            return name, {"status": "error", "error": repr(exc)}
        # Pull the last AI message text out of the result state.
        summary = ""
        for msg in reversed(result.get("messages", []) or []):
            text = getattr(msg, "text", None)
            if text:
                summary = text.rstrip()
                break
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content.strip():
                summary = content.rstrip()
                break
        return name, {"status": "ok", "summary": summary}

    pairs = await asyncio.gather(
        *(_run_one(name, runnable) for name, runnable in runnables),
    )
    out: dict[str, Any] = {}
    errors: list[str] = []
    for name, payload in pairs:
        out[name] = payload
        if payload.get("status") == "error":
            errors.append(f"{name}: {payload.get('error', 'unknown')}")
    if errors:
        out["errors"] = errors
    return out


def make_run_scrapers_tool(
    runnables: list[tuple[str, Runnable]],
) -> BaseTool:
    """Build the `run_scrapers` tool bound to the two subagent runnables.

    `runnables` is a list of `(name, runnable)` pairs. Today the
    list is exactly `("fotocasa_scraper", fotocasa_graph)` and
    `("idealista_scraper", idealista_graph)`. Adding a third
    portal is one more tuple.

    The tool's single argument is `brief` — the hard-filter / city
    brief the orchestrator would otherwise have passed to each
    `task` call. The brief is the SAME for both subagents; that's
    the point of "always call both, every run".
    """

    if not runnables:
        raise ValueError("run_scrapers: at least one subagent runnable is required")

    @tool
    async def run_scrapers(brief: str) -> str:
        """Run every configured scraper subagent concurrently.

        Each subagent receives the SAME `brief` (hard filters, city,
        ingest cap, etc.) and executes in parallel. The tool
        returns a JSON object:

            {
              "fotocasa_scraper": {"status": "ok", "summary": "..."},
              "idealista_scraper": {"status": "ok", "summary": "..."},
              "errors": ["..."]   // present only when one or more subagents raised
            }

        A failure in one subagent does not cancel the others; the
        exception is captured in that subagent's entry and the
        combined handoff is returned. The orchestrator decides
        how to surface the error in its summary.
        """
        combined = await _gather_subagents(runnables, brief=brief)
        return json.dumps(combined, default=str)

    return run_scrapers


__all__ = ["make_run_scrapers_tool", "_gather_subagents"]
