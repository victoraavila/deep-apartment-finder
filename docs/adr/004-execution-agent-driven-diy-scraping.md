# ADR-004 — Execution: agent-driven, DIY free scraping

- Status: Accepted (Sprint 1)
- Date: 2026-06-27

## Context

We could:

- (a) Write a deterministic scraping pipeline and have the LLM only summarize.
- (b) Make the LLM the orchestrator that drives the pipeline and validates
  each step's output.

For a personal-scale project with a single source portal (Sprint 1) and a
modest monthly target (top-5 daily notification), we have to choose how much
of the loop is the LLM's responsibility.

## Decision

- The **orchestrator (Deep Agents)** plans the run with `write_todos` and
  delegates to the **`fotocasa_scraper` subagent** via the `task` tool.
- The subagent runs autonomously with its own tool set, **validates the
  quality of its own work** (count fetched vs. count ingested, dedup
  surfaced, no rows silently dropped), and returns a handoff summary.
- Scraping is **DIY and free** in Sprint 1: `httpx` + `selectolax` for SSR
  pages, with a `playwright` fallback for CSR detail pages. No paid proxy
  or scraping service.
- We escalate to stealth/CSR strategies or a paid service **only if** Sprint
  1's quality is not good enough. That decision lives at the start of
  Sprint 3.

## Consequences

- Subagents are stateless and ephemeral. Persistence is in Postgres. This is
  intentional — it keeps the agent loop debuggable.
- Subagents are **registered at build time**; the orchestrator does not
  invent subagents at runtime. Adding a new subagent is a code change, not a
  prompt change.
- "The LLM made a bad call" becomes a LangSmith trace, not a black box.
- The free-tier constraint means we may need a Sprint 3 escalation. ADR
  records this as a known risk, not a blocker.
