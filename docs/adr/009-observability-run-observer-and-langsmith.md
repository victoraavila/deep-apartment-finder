# ADR-009 — Observability: `RunObserver` port + CLI phases + LangSmith full-pipeline tracing

## Context

Sprint 2's daily run was an opaque "HTTP POST stream that ends in a
JSON blob". The operator could read the final stdout, but had no way
to tell — mid-run — whether the system was researching, ingesting,
blocked, ranking, or notifying. Low-level `logger.info` lines in
the scraper and the orchestrator didn't translate to a phase view.

Sprint 3 (Pillar A + B) needed:

- A single sink the CLI and the orchestrator both emit events into
  (so the operator sees real-time phase transitions in stderr and
  the full report is persisted to disk + a DB row).
- A LangSmith trace that covers the LLM calls **and** the
  deterministic steps (Postgres reads/writes, scraper HTTP, ranker
  scoring, SMTP send, dedup skips, report writes). The trace URL
  is stamped on the persisted run report so the operator can
  reconstruct the run from one URL.
- A `RunReport` domain object that captures the structured record
  of a single run (phases, counts, decisions, warnings, top-N,
  dedup-dropped count, ranking_run_id, notification outcome,
  trace_url).

## Decision

Three layers, each additive to the previous:

1. **`RunObserver` Protocol (`ports/run_observer.py`)** — the
   single sink. Methods: `phase_start`, `phase_end`, `count`,
   `waiting`, `decision`, `warning`, `error`. The orchestrator's
   deterministic steps and the CLI both emit through it.
2. **Two adapters** (`adapters/observability/`):
   - `CliRunObserver` — writes phase headers (`=== scraper ===`)
     and counter lines to **stderr** in real time. Counters are
     aggregated per phase and rendered in the `phase_end` line.
   - `RecordingRunObserver` — accumulates every event into a
     `RunReport` (Pillar A). The CLI calls
     `await observer.finalize(backend, report_path)` to persist
     the JSON to `/orchestrator/reports/<run-uuid>.json` and to
     the `run_reports` Postgres table.
   - `tracing.py` — a thin `langsmith.run_helpers.traceable`
     wrapper (`@trace(name, **metadata)`). Gated on
     `settings.langsmith_tracing`; when off, the decorator is a
     pass-through with a single bool check. The CLI exposes a
     `--trace` flag to force-enable for a single invocation.
3. **`RunReport` (`domain/run_report.py`)** — a pure value object
   with `start_phase`, `end_phase`, `add_count`, `note`,
   `set_top_n`, `set_dedup_dropped`, `set_criterion_distribution`,
   `to_dict`/`to_json`. The CLI's `show-run <run-uuid>`
   subcommand re-prints a persisted report by reading the
   `run_reports` Postgres table.

The CLI uses a `_FanOutObserver` to wire the same event into both
the `CliRunObserver` and the `RecordingRunObserver` without
coupling them.

## Consequences

- **Operator UX.** A Sprint 3 run looks like
  ```
  === scraper (fotocasa) ===
    waiting on LLM
    waiting on Fotocasa HTTP
    -> scraper: cards 47  inserted 41  duplicates 6  1234 ms
  === ranker ===
    waiting on Postgres
    -> ranker: apartments_scored 121  scores_written 363  top_n_returned 5  52 ms
  === notifier ===
    waiting on SMTP
    -> notifier: apartment_ids 5  312 ms
  === done ===
    run report: /orchestrator/reports/<run-uuid>.json
    trace: https://smith.langchain.com/r/<run-id>
  ```
  instead of an opaque stream.
- **No new dependencies.** `langsmith` was already in
  `pyproject.toml`; the rest is stdlib.
- **Deterministic path is also traced.** Every Postgres read/write,
  the ranker scoring, the SMTP send, the dedup-skip path, and
  the report writes carry a span. The trace reconstructs the run
  end-to-end.
- **Stamping the trace URL on the report.** The CLI reads
  `langsmith.run_helpers.get_current_run_tree()` at the end of
  the run and stamps the URL on the persisted `RunReport`. The
  operator has one URL to reconstruct the whole run.
- **Adapters are pure.** `RunObserver` is a Protocol; tests can
  pass a recording fake without touching LangSmith.
- **One Protocol attribute check fails mypy.** The `end_phase`
  signature uses keyword-only arguments and the `RunObserver`
  Protocol stubs don't fully express it; we added a per-file
  mypy override in `pyproject.toml` and rely on the runtime
  tests (`tests/unit/test_observers.py`) to assert the contract.

## Alternatives considered

- **Stdlib `logging` only.** Lower friction, but the operator
  still has to grep the logs. The Sprint 2 contract (HTTP POST
  stream -> final blob) is preserved.
- **OpenTelemetry.** Stronger ecosystem, but heavier integration
  and the LangSmith backend already covers our needs. Re-evaluate
  if we move off LangSmith in Sprint 5.
- **Single composite observer that prints + records.** Simpler
  wiring, but the test for the recording adapter would have to
  capture stderr. Keeping the two adapters separate makes each
  one testable in isolation.
