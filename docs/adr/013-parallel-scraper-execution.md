# ADR-013 — Parallel scraper execution (Sprint 4 Pillar B)

## Context

Sprint 1 + 2 ran one scraper (Fotocasa). Sprint 3 added the
`idealista_scraper` subagent and the orchestrator's prompt
delegated to both in a single run — but sequentially. The
LLM-driven orchestrator's `task` tool calls each subagent one
at a time, so the two scraper subagents run back-to-back. As
we add a third portal, the serial schedule is the bottleneck:
each scraper takes 2-3 minutes (LLM extraction + database
writes), and the wall time grows linearly with the number of
portals.

Sprint 4 (Pillar B) reduces the scraper phase wall time by
firing the two subagent graphs concurrently via `asyncio.gather`
in a new `run_scrapers` tool. The same idea applies to the
per-card detail fetches inside a subagent (B.2) — the LLM may
batch N `fetch_listing` calls in a single tool call, and the
underlying async session is concurrency-safe.

## Decision

### B.1 — Across-portal: `run_scrapers` tool

A new `run_scrapers(filters_brief: str) -> str` tool
(`tools/orchestrator/run_scrapers.py`) is added to the
orchestrator's tool set. The tool:

1. Takes a list of `(name, compiled_subagent_runnable)` pairs.
2. Calls `asyncio.gather(...)` on the two runnables, each
   receiving the same `{"messages": [HumanMessage(brief)]}`
   state.
3. Returns a single combined JSON handoff of shape:
   ```json
   {
     "fotocasa_scraper": {"status": "ok", "summary": "..."},
     "idealista_scraper": {"status": "ok", "summary": "..."},
     "errors": ["..."]   // present only when one or more subagents raised
   }
   ```

A failure in one subagent does NOT cancel the others. The tool
uses `asyncio.gather(..., return_exceptions=True)` semantics
internally; the captured exception is reflected in that
subagent's `{"status": "error", "error": "<repr>"}` entry and
added to the top-level `errors` list. The orchestrator decides
how to surface the partial result in its summary.

The orchestrator's prompt (`subagents/prompts/orchestrator.md`)
is updated to call `run_scrapers` **exactly once** per run
instead of two sequential `task` calls. The `task` tool stays
registered for debugging a single portal in isolation.

The `run_scrapers` tool is built in
`agent/orchestrator.build_orchestrator` by:

1. Iterating the existing scraper subagent specs
   (`fotocasa_scraper`, `idealista_scraper`).
2. Compiling each via `create_sub_agent({**spec, "model": llm})`
   — the same factory the deepagents `task` tool uses
   internally.
3. Passing the compiled `Runnable` list to
   `make_run_scrapers_tool(...)`.

The orchestrator's deepagents `subagents=[...]` argument is
also kept (so the `task` tool still works), but the daily run
goes through `run_scrapers`. The two are independent.

### B.2 — Across detail fetches in one subagent

`fetch_listing` is async and the underlying `curl_cffi.Session`
(Idealista) / `httpx.AsyncClient` (Fotocasa) is concurrency-safe
— a single shared session handles N concurrent requests without
contention. The LLM is free to issue N parallel `fetch_listing`
tool calls in a single batch; the framework executes them
concurrently and the N calls complete in roughly the time of
the slowest. The subagent prompts
(`subagents/prompts/{fotocasa,idealista}_scraper.md`) document
this hint so the LLM batches aggressively.

The integration test
`tests/integration/test_sprint4_pipeline.py::test_parallel_fetch_listing_calls_overlap`
asserts the contract at the scraper level (N parallel
`fetch_listing` calls complete in ~the slowest time) so a
regression in the scraper surfaces even if the LLM-side fan-out
ever changes.

### B.3 — Inside the Idealista `fetch_listing`

The Sprint 4 detail-page upgrade (Pillar A) replaced the
Sprint 3 page-walk with a single detail fetch. The walk was
the bottleneck for cards that scrolled to page 4+; the upgrade
removes the cost. No separate ticket is needed.

### Concurrency model

| Axis | Mechanism | Boundary | Failure mode |
| --- | --- | --- | --- |
| Across portals (B.1) | `asyncio.gather(...)` of the two subagent graphs in `run_scrapers` | Inside the orchestrator process; one Python event loop | Subagent exception → other still completes; the exception is captured in the handoff |
| Across detail fetches in one subagent (B.2) | LLM-side parallel tool calls on the existing single-URL `fetch_listing` | Inside the subagent's LLM session | Per-card error handled by the tool's existing try/except |
| Inside the Idealista `fetch_listing` (B.3) | Detail-page fetch replaces the page walk (Pillar A) | Inside the scraper | `fetch_detail_html` failure falls back to search-card walk |

There is no multiprocessing. Everything is one Python event
loop, one Postgres pool (which is already concurrent —
`PostgresApartmentRepository.upsert` uses
`async with self._pool.acquire()` and asyncpg serialises
per-connection transactions), and one set of HTTP sessions.
The InMemory repository's `_by_source_ext` dict is touched
only under coroutines — single-thread asyncio is safe.

## Consequences

- **Wall time shrinks.** Sprint 3 ran Fotocasa + Idealista
  sequentially. Sprint 4 fires them in parallel; the scraper
  phase is `max(t_foto, t_idealista) + overhead` instead of
  `t_foto + t_idealista + overhead`. For the current portals
  the saving is ~40-50% (the integration test
  `test_parallel_subagents_overlap_in_time` asserts the
  contract).
- **OCP held.** The orchestrator's `task` tool still works
  for single-portal debugging. The new `run_scrapers` tool is
  added to the orchestrator's tool list; the scraper subagent
  factories and their tool sets are unchanged.
- **LLM session count is unchanged.** The two subagent
  graphs each own their own LLM session; the parallel path
  co-schedules the two sessions, it does not multiplex them.
- **No multiprocessing.** One event loop is enough at the
  current scale. The single Postgres pool is the contention
  point, and it is not saturated.
- **The orchestrator's prompt is updated** to call
  `run_scrapers` once instead of two `task` calls. The `task`
  tool stays registered but is no longer the orchestrator's
  primary path for the scraper phase.
- **New observability counters** (`details_enriched`,
  `details_failed`) are tracked per IdealistaScraper instance
  and reported in the subagent's handoff (Pillar A's
  acceptance criterion 1 + 8).
- **The `RecordingRunObserver` already handles out-of-order
  phase events** (it accumulates counts and persists the union
  at `phase_end`), so no new observer code is needed for the
  parallel emission. The two `=== scraper (fotocasa) ===` and
  `=== scraper (idealista) ===` blocks interleave on stderr as
  the subagents make progress.

## Alternatives considered

- **Multiprocessing / process pool.** Overkill at the current
  scale; one event loop is enough. The Postgres pool would
  still be the bottleneck, and per-process interpreter startup
  cost is high.
- **Per-subagent event loops + cross-loop communication.**
  Adds complexity (asyncio queues / cross-loop primitives) for
  no measurable wall-time win at this scale.
- **A pre-forked pair of long-lived subagent worker processes
  with a queue.** Re-introduces a lot of state management for
  a daily run; the cron is the "queue" today.
- **Await the two subagents serially in the orchestrator's
  deterministic tail.** Doesn't help — the LLM still drives
  the planning, and the tool-call fan-out is what controls
  scheduling. The `run_scrapers` tool is the right boundary.

## Future work

- **Multiprocessing / multi-process workers.** One event loop
  is enough at the current scale. If the ranker or the LLM
  step later becomes the bottleneck, a process pool with one
  worker per portal would be the next move.
- **A third portal.** Adding a third subagent is one more
  tuple in `run_scrapers`'s `runnables` list (and one more
  branch in `subagents/`). The parallel orchestration is
  already in place; the cost of "yet another portal" is now
  only the wall time of the new subagent, not the sum.
