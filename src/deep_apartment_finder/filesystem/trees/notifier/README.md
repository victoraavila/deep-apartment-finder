# `/notifier/` subtree — contract

The deterministic notifier is not an LLM-driven subagent; it is a
Python function (`domain.notifier.send_notification`) the
orchestrator calls directly after the ranker. The subtree exists
so the notifier has a persistent place to drop its artefacts
(Sprint 2 ADR-005 spirit), even though it does not "own" them
the way an LLM subagent does.

## Folders

| Folder     | Purpose                                                                              | Lifecycle  |
| ---------- | ------------------------------------------------------------------------------------ | ---------- |
| `outbox/`  | Rendered email bodies (plain text + HTML) per send.                                   | Persistent |
| `logs/`    | Per-send SMTP logs.                                                                  | Persistent |

## Why persistent?

The operator wants a copy of every email on disk even if Gmail is
unreachable at send time, and even if the SMTP send succeeds but
the `notifications` DB write fails. The outbox is the human
source of truth for "what was sent" — Postgres' `notifications`
table is the dedup mechanism, the outbox is the archive.

## What lives here in practice?

- `outbox/<sent_on>.txt` and `outbox/<sent_on>.html` — the body
  that was (or would have been) emailed.
- `logs/<sent_on>.log` — a one-line-per-send log: timestamp,
  recipient, subject, ranking_run_id, sent/skipped status.

The orchestrator writes the outbox on every run; the log is
appended-to in the same code path.
