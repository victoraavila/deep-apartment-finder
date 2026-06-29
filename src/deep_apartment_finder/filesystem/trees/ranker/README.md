# `/ranker/` subtree — contract

The deterministic ranker is not an LLM-driven subagent; it is a
Python function (`domain.ranking.compute_ranking`) the orchestrator
calls directly after the LLM part. The subtree exists so the ranker
has a persistent place to drop its artefacts (Sprint 2 ADR-005
spirit), even though it does not "own" them the way an LLM subagent
does.

## Folders

| Folder      | Purpose                                                                              | Lifecycle  |
| ----------- | ------------------------------------------------------------------------------------ | ---------- |
| `plans/`    | Weight snapshots, scoring config, and other run-prep artefacts.                      | Persistent |
| `reports/`  | Per-run top-N + per-apartment score breakdowns (the human-readable side).            | Persistent |

## Why persistent?

Reproducibility. If the operator wants to understand *why* a
notification went out on a given day, the per-run breakdown is on
disk. The DB has the same data in `apartment_scores`, but a flat
JSON file is easier to read and diff.

## What lives here in practice?

`reports/<ranking_run_id>.json` — a JSON dump of
`ranking_run_id`, `apartments_scored`, and the full `top` array
with breakdowns. The orchestrator writes this after every rank.
