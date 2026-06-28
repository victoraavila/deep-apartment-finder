# ADR-005 — Per-subagent filesystem: CompositeBackend + prefix-forcing tools + prompt

- Status: Accepted (Sprint 1)
- Date: 2026-06-27

## Context

Deep Agents' `FilesystemMiddleware` lets an agent read/write files. Without
isolation, two subagents could overwrite each other's raw HTML, extracted
JSON, or run reports. We need a defense-in-depth approach so the orchestrator
and the `fotocasa_scraper` subagent cannot step on each other even if a
prompt is loose.

## Decision

Three complementary layers:

1. **Hard (backend)** — A `CompositeBackend` routes writes under
   `/fotocasa_scraper/` and `/orchestrator/` to a **persistent**
   `StoreBackend`. Writes outside those routes go to an **ephemeral**
   `StateBackend`, so a stray write is wiped at the end of the run.
2. **Boundary (tools)** — Each subagent receives only the tools it needs.
   Tools that write files (e.g. `save_snapshot`) **force a prefix** to the
   subagent's own subtree. SQL stays out of the tools: persistence is via
   the injected `ApartmentRepository`, not raw queries.
3. **Soft (prompt)** — The subagent's system prompt documents its allowed
   subtree and the purpose of each folder (`raw/`, `extracted/`, `cache/`,
   `selectors/`, `logs/`, `plans/`, `reports/`). Humans and the LLM agree on
   the same contract because both read the same `README.md`.

## Consequences

- Adding a new subagent means: a new route in `filesystem/routes.py`, a new
  subtree under `filesystem/trees/`, the subagent's tools (which force the
  prefix), and the matching prompt. All four are additive — no other file
  changes.
- The persistent store is **not** a source of truth. The database is.
  Persistent files are for replay, debug, and cross-run dedup hints.
- Layers 1, 2, 3 are deliberately redundant. We accept the duplication
  because the failure mode of one layer (a prompt ignored, a tool misuse) is
  caught by the next.
