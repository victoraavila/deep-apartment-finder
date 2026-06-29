# ADR-006 — Researcher subagent + dangerous-neighborhoods constants table

- Status: Accepted
- Sprint: 2
- Date: 2026-06-29

## Context

`SPRINT2.md` introduces a soft criterion that penalizes apartments
close to a "dangerous" neighborhood. The list of dangerous
neighborhoods in Zaragoza is a fact about the world, not about the
agent — we need a way to populate it without hard-coding it in
`config.py` and without inventing the values ourselves (we are
*not* the source of truth on Zaragoza's safety landscape).

Three options were considered:

1. **Operator-curated list.** A list checked into the repo (or
   in `config.py`) that the operator maintains manually. Pro:
   deterministic, no LLM cost. Con: the operator is not a
   sociologist; the list will be incomplete and biased.
2. **Static public dataset.** A pre-packaged GeoJSON of Zaragoza's
   neighborhoods with a "dangerous" flag, fetched at startup.
   Pro: reproducible. Con: a single source of authority
   (which one?); Sprint 2 doesn't have the bandwidth to pick
   and validate one.
3. **Researcher subagent (chosen).** An LLM-driven subagent that
   runs *once*, on the first run, researches the topic from the
   public web, and persists the result to a constants table. The
   operator can override any row manually.

## Decision

Option 3. The `researcher` subagent is registered in the
orchestrator like any other subagent, with two tools:

- `web_search` — wraps the Exa web search API.
- `upsert_neighborhoods` — writes a JSON list of proposed rows
  to the `dangerous_neighborhoods` table.

The orchestrator gates the first run on
`count_dangerous_neighborhoods() == 0`. When the gate is open,
it delegates to the `researcher` subagent and stops, asking the
operator to re-run after eyeballing the list. The
`list-dangerous` CLI subcommand is the operator's tool to inspect
the table.

## Consequences

- The operator must have an `EXA_API_KEY` set in `.env` to
  bootstrap. (Sprint 2 ships the port; another backend can be
  wired without code changes.)
- The first run requires a manual re-run. This is a deliberate
  human-in-the-loop checkpoint (mirrors the spirit of
  ADR-005).
- The list is the LLM's interpretation of the public web. It is
  not authoritative. The operator is expected to review and
  override via SQL.
- A future sprint can replace the researcher with a
  government-data pipeline (Zaragoza's open data portal has
  `barrios` boundaries) without changing the ranker.
- The ranker treats an empty `dangerous_neighborhoods` table
  as a neutral 0.5 for every apartment and logs a warning. The
  system runs to completion even if the researcher fails.

## Out of scope (deferred)

- **Availability re-check on ranked apartments.** The notifier
  trusts the database. A future sprint will hit each top-5's
  original URL before sending.
- **Polygon-based geometry.** We model neighborhoods as
  center + radius (haversine). Polygons are a future refinement
  (Sprint 5 with `OSRM` / `pgis`).
