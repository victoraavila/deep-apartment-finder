# Roadmap — Deep Apartment Finder

A personal agent that finds apartments to rent in Zaragoza: ingests listings
from multiple portals, ranks them by hard + soft criteria, and notifies the
top 5 on a daily cadence.

This document is the **top-level** roadmap: principles, stack, execution
model, and an abstract view of every sprint. Detailed scope, layout, and
acceptance criteria for each sprint live in its own `SPRINT{n}.md` file.
Avoid duplicating content between this file and the sprint files.

## Guiding principles

- **Iterate in short, self-contained sprints.** No speculative code. Each
  sprint delivers verifiable value: something you can run, inspect, and use
  to decide the next move.
- **SOLID + Ports & Adapters (hexagonal).** The agent and the domain depend
  only on `ports/` (abstractions). Concrete I/O lives in `adapters/` and is
  injected at a single composition root. Adding a new scraper, notifier,
  criterion, or embedding provider is an additive change, not a refactor.
- **Single Responsibility per module.** Parsing, HTTP, persistence, scoring,
  and notifying are decoupled. A change to a CSS selector never touches the
  repository or the agent.
- **Scraping is DIY and free** until proven insufficient. We try the easy
  portals first and only escalate to stealth/CSR strategies or paid services
  if quality demands it.
- **Decisions are recorded as ADRs** under `docs/adr/NNN-*.md`.

## Stack

| Concern | Choice |
| --- | --- |
| Language / packaging | Python with `uv` |
| Agent framework | **Deep Agents** (LangChain / LangGraph) — orchestrator + specialist subagents |
| Reasoning LLM | **Groq** primary, **opencode-go (glm)** fallback on rate-limit. Both OpenAI-compatible |
| Database | **Postgres + pgvector** via `docker compose` (local; portable to a VPS) |
| DB access | Raw SQL + `asyncpg` migrations (no ORM) |
| Scheduler | Local `cron` calling the CLI; abstraction layer keeps it deployable to a VPS |
| Notification | Free tier (Resend / SMTP / Twilio) — provider chosen in Sprint 2 |
| Idiomes | Decisions and discussion in PT-BR; all code and docs files in English |

## Execution model — agent-driven

The orchestrator drives each run. It plans with `write_todos` and delegates
work to specialist subagents via the Deep Agents `task` tool. Each subagent
runs autonomously with its own tool set, **validates the quality of its own
work**, and returns a handoff summary back to the orchestrator.

Accepted framework constraints:

- Subagents are **stateless and ephemeral**. Memory across runs lives in the
  database, not in the subagent.
- Subagents are **registered at build time**; the orchestrator does not invent
  subagents with arbitrary tools at runtime. "Dynamism" is only in which
  registered subagent the orchestrator chooses to call.
- `FilesystemMiddleware` is always enabled on every agent. Per-subagent
  isolation is achieved in three complementary layers:
  1. **Hard (backend)** — `CompositeBackend` routes persistent writes to the
     subagent's subtree; anything outside routes to ephemeral `StateBackend`.
  2. **Boundary (tools)** — each subagent receives only the tools it needs,
     and tools that write files force a prefix to its own subtree.
  3. **Soft (prompt)** — the subagent's system prompt documents its allowed
     subtree and the purpose of each folder.

## Reversible decisions (deferred)

These are intentionally left open and revisited at the indicated sprint:

| # | Question | Sprint |
| --- | --- | --- |
| Q1 | Use Exa as a discovery layer for listings (free tier)? | S3 |
| Q2 | Purpose of embeddings: (a) cross-portal dedup, (b) "similar to ones I liked", (c) natural-language search | S4 |
| Q3 | Dangerous neighborhoods list (Delicias, El Gancho, ...): curated manually vs researched by the agent | start of S2 |
| Q4 | Distance to dangerous neighborhoods: straight-line (haversine) vs route (OSRM/Mapbox) | S2 |
| Q5 | Notification provider: Resend vs SMTP vs Twilio | S2 |
| Q6 | Coverage of the pet-policy field on Fotocasa / Idealista | S2 / S3 |

The database schema is prepared for embeddings from Sprint 1 (a nullable
`embedding vector(1536)` column), so activating them in Sprint 4 is a
non-breaking change.

## Sprints — abstract view

### Sprint 1 — Foto casa ingestion MVP + quality validation
Validate that we can extract valid Zaragoza appartments for free using agents
and persist them to Postgres. Establishes the SOLID project skeleton, the
`FotocasaScraper` adapter, the `ingest_apartment` tool with dedup, and the
`CompositeBackend` per-subagent filesystem routes. No ranking, no soft
criteria, no notifications, no embeddings, no scheduler, no Idealista.
Details: `SPRINT1.md`.

### Sprint 2 — Ranking + soft filters + notification
Add soft criteria as pluggable `SoftCriterion` implementations
(distance-to-dangerous, pet-policy extracted from the listing text by the
LLM), a `ranker` subagent, a `notifier` subagent, and a daily free-tier
notification of the top 5. Wire up the local cron scheduler for real.
Decisions Q3, Q4, Q5 are resolved at the start of this sprint.

### Sprint 3 — Second scraping source
Add a second adapter under `adapters/scrapers/` following the same `ScraperPort`
(OCP: no changes to Fotocasa or the orchestrator). Candidate order: another
easy portal first; Idealista with SSR/CSR + stealth strategy if Fotocasa
alone is insufficient. Prepares the cross-portal dedup use case that may
motivate embeddings in Sprint 4.

### Sprint 4 — Embeddings (purpose to be decided)
Decide between the three embedding use cases (Q2), activate the
`embedding` column already present in the schema, add an
`OpenAIEmbeddings`-style adapter behind the `Embeddings` port, and re-rank by
similarity where applicable.

### Sprint 5 (optional) — Production
Migrate the compose + cron to a VPS, enable permanent LangSmith tracing, add
backoff/retry and collection-failure alerting. Only undertaken if the daily
loop is stable enough to leave running.

## ADRs
- ADR-001 — Framework: Deep Agents
- ADR-002 — LLM: Groq primary + opencode-go fallback
- ADR-003 — Database: Postgres + pgvector
- ADR-004 — Execution: agent-driven, DIY free scraping
- ADR-005 — Per-subagent filesystem: CompositeBackend + prefix-forcing tools + prompt
- ADR-006 — Researcher subagent + dangerous-neighborhoods constants table
- ADR-007 — Haversine distance provider
- ADR-008 — Gmail SMTP notifier