# ADR-001 — Framework: Deep Agents

- Status: Accepted (Sprint 1)
- Date: 2026-06-27

## Context

The project needs an agent that:

1. Plans a multi-step run (search → parse → dedup → ingest) without a hand-
   rolled state machine.
2. Delegates scraping to a specialist that returns a handoff.
3. Reads/writes files (raw HTML, extracted JSON, run reports) without us
   re-implementing filesystem plumbing.
4. Can grow into ranking + notification + a second scraper without rewriting
   the orchestration code.

We have to choose between LangChain's `create_agent` (single-purpose loop),
LangGraph (custom control flow), and Deep Agents (batteries-included harness
on top of LangGraph).

## Decision

Use **Deep Agents** as the orchestration layer for the orchestrator. Each
specialist is a registered subagent, invoked via the `task` tool.

## Consequences

- We get `write_todos` planning, `task` delegation, and `FilesystemMiddleware`
  for free — the kind of glue code we'd otherwise write and maintain.
- Subagents are stateless and ephemeral; memory across runs lives in Postgres.
  This is intentional and documented in ADR-004.
- Deep Agents is a relatively new layer; pinning a tested version matters. We
  pin `deepagents` to a known-good release in `pyproject.toml` and revisit
  on each sprint boundary.
- If a future sprint needs precise control over a sub-pipeline (e.g. a
  deterministic graph for the ranker), the LangGraph graph can be registered
  as a named subagent — the orchestrator does not need to know.
