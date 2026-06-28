# `/orchestrator/` subtree — contract

The orchestrator owns this prefix. ADR-005 documents the isolation
layers; this README documents what the orchestrator is expected to
leave behind.

## Folders

| Folder    | Purpose                                                            | Lifecycle |
| --------- | ------------------------------------------------------------------ | --------- |
| `plans/`  | Exported `TodoList`s from the orchestrator's planning step.       | Persistent |
| `reports/`| Final per-run reports (counts, handoff summary from subagents).   | Persistent |

## Why persistent?

A daily run leaves a paper trail: the plan it started with, and the
report it ended with. The next run can refer back to the prior report
without scanning the database.

## What lives here in practice?

In Sprint 1 the orchestrator writes:
- `plans/<run-uuid>.md` — the planning step
- `reports/<run-uuid>.md` — the final summary the CLI prints
