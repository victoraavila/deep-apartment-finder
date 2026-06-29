# `/researcher/` subtree — contract

The `researcher` subagent owns everything under this prefix
(Sprint 2). ADR-005 documents the three layers of isolation; this
README documents the *shape* the subagent is expected to leave
behind, so a human maintainer and the LLM agree on the same
contract.

## Folders

| Folder                       | Purpose                                                                                | Lifecycle  |
| ---------------------------- | -------------------------------------------------------------------------------------- | ---------- |
| `dangerous_neighborhoods/`   | JSON snapshots of the researcher's proposed neighborhoods, before they hit Postgres.   | Persistent |

## Why persistent?

The researcher runs **once** — on the first run, when
`dangerous_neighborhoods` is empty. After that, the orchestrator
short-circuits and the researcher is not invoked. The proposed JSON
snapshot is the only on-disk evidence of what the agent researched;
it lets the operator eyeball the list before re-running.

The persistent store is **not** a source of truth. Postgres is. The
operator can always `TRUNCATE dangerous_neighborhoods` to force a
re-bootstrap.

## What lives here in practice?

`dangerous_neighborhoods/proposed.json` — a JSON object with
`source` (string) and `rows` (array of proposed neighborhoods). The
`upsert_neighborhoods` tool writes this file before writing to
Postgres.
