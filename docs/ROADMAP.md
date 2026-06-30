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
- **Runs must be operator-observable.** A CLI run should not look like an
  opaque stream of provider POST requests. The operator needs clear phase
  transitions, progress counters, current subagent/tool state, warnings,
  decisions, and links to persisted reports while the run is happening.
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
| Observability | **LangSmith** traces + structured CLI/run reports |
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

## Near-term product debt

- **End-to-end run UX / observability:** add an operator-facing execution
  view for the whole pipeline, not just the first-run researcher. The CLI
  should print structured phases such as `researcher`, `scraper`, `ranker`,
  and `notifier`; show current action, counts, and elapsed time; summarize
  each LLM/tool call in domain terms; clearly distinguish waiting on LLM,
  web search, scraper HTTP, database writes, ranking, and SMTP; and write a
  persistent run report that can be inspected after cron/manual runs. This
  should make it obvious whether the system is researching, ingesting,
  blocked, retrying, ranking, or notifying instead of showing only low-level
  HTTP POST logs.
- **LangSmith full-pipeline tracing:** instrument the complete run in
  LangSmith, not only LLM-dependent steps. Every run should have one parent
  trace with child spans for orchestrator planning, researcher web search,
  scraper search/fetch/ingest, Postgres reads/writes, hard-filter decisions,
  ranker score computation, notification rendering, SMTP send/dedup, and
  filesystem report writes. The trace should expose state transitions,
  counts, input/output summaries, errors, retries, and skip reasons so the
  operator can reconstruct what happened even for deterministic code paths.
- **Ranked result explainability:** the CLI, persisted run report, and email
  must show the actual ranked apartments, not just database ids. Every top-N
  row should include title, price, rooms/bathrooms/size, address, URL,
  final score, and per-criterion score details so the operator can inspect
  the result without opening SQL.
- **Listing data quality before ranking:** invalid coordinates such as
  `(0, 0)` must be normalized to missing values, never treated as real
  locations. The ranker should not award a high distance-to-dangerous score
  when latitude/longitude are missing or invalid; the ingest/scraper path
  should aim to populate coordinates for every listing when the source
  exposes them. Duplicate listings should also be refreshed/backfilled with
  newly extracted soft fields (`pet_policy`, `furnished`) and corrected
  coordinates instead of leaving old rows with stale NULLs.

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

### Sprint 3 — Observability + second scraping source
First make the full daily run observable: structured CLI phase output plus
LangSmith tracing that covers LLM calls and deterministic states alike
(database operations, scraper HTTP, ranking, notification rendering, SMTP,
dedup skips, and report writes). Make ranked results explainable in the
terminal, run report, and email by including apartment title, price,
rooms/bathrooms/size, address, URL, final score, and score breakdown for
each top-N row. Tighten listing quality before ranking: treat `(0, 0)` and
other invalid coordinates as missing, avoid rewarding missing/invalid
coordinates in the distance criterion, backfill duplicate rows with newly
available soft fields and corrected coordinates, and validate coordinate
coverage in `validate-quality`. Then add a second adapter under
`adapters/scrapers/` following the same `ScraperPort` (OCP: no changes to
Fotocasa or the orchestrator). Candidate order: another easy portal first;
Idealista with SSR/CSR + stealth strategy if Fotocasa alone is insufficient.
Prepares the cross-portal dedup use case that may motivate embeddings in
Sprint 4.

### Sprint 4 — Embeddings (purpose to be decided)
Decide between the three embedding use cases (Q2), activate the
`embedding` column already present in the schema, add an
`OpenAIEmbeddings`-style adapter behind the `Embeddings` port, and re-rank by
similarity where applicable.

### Sprint 5 — Full coverage: walk every page, deep-fetch the shortlist

**Problem.** Both scrapers (`FotocasaScraper`, `IdealistaScraper`) are
constructed with `max_cards=settings.ingest_max_listings` (default 50,
see `config.py:55` and `main.py:87,93`). The `search_listings` async
iterator stops the moment `yielded >= max_cards`, so a Zaragoza run
that returns 175 Fotocasa cards and ~500 Idealista cards across 17
pages effectively buys the portal's recency-sorted page 1 — and only
page 1. The current run log (`f7a0ebe8...`) shows Idealista stopping
mid-page-3 with `cumulativeInspected=60 yielded=30` from page 2, and
no page-3 log line because the cap fires *before* the next request.

This is selection bias masquerading as a budget. Neither portal
guarantees a sort order that puts the operator's most-likely-to-want
listings on page 1 — the default is "most recent" × "promoted",
which is a poor proxy for fit. A 95/100 apartment in a competitive
building rotates off page 1 within hours; a stale 60/100 listing
sits on page 1 for a week. The cap also breaks the cross-portal
dedup guarantee (Sprint 3 Pillar F / ADR-012): a Fotocasa-only
listing with an Idealista sibling on page 3 is silently
"not deduplicated" because Idealista never walked to page 3.

**Why the cap exists today.** Two reasons that turn out to be the
*wrong* axes to cap on:

- Scraper HTTP time. Dominated by the polite delay between pages
  (2s for Idealista, configurable). Walking all 17 pages of an
  Idealista result set is ~34s of delay, not the wall-time the cap
  was supposed to bound. This is a small cost relative to the
  detail-page fetches, which take 1–3s *per card* and dominate the
  per-portal runtime.
- LLM tokens. The LLM extracts `pet_policy` and `furnished` from
  each listing's `description` at ingest time. Tokens scale with
  *detail fetches*, not with *cards seen*. Capping search at 50 is
  a proxy for capping detail work, but the wrong proxy — it caps
  the cheap thing (cards) to limit the expensive thing (detail
  fetches), with the side effect of starving the cross-portal dedup.

**Strategy.** Decouple the *search* budget from the *detail* budget.
The scraper walks every page the portal returns for the configured
hard filters; the LLM picks a shortlist of cards to deep-fetch and
ingest with the full normalized `Apartment`. The shortlist is the
*only* thing that gets bounded. The bulk of the result set is
recorded as a **cheap-card-only row** so the DB still grows,
cross-portal dedup still works, and the ranker can later flip the
shortlist into the deep set on a follow-up run if priorities
change.

**Scope (5 sub-pillars).**

- **Pillar A — Drop the search cap.** Remove
  `max_cards=settings.ingest_max_listings` from the `*Scraper(...)`
  constructor calls in `main.py:85-94`. Replace it with a per-portal
  `max_detail_fetches` knob (default 25 per portal) on the
  `Settings`. The `search_listings` async iterator walks every page
  until the portal returns an empty / short page (the existing
  `len(cards) < 15` heuristic at `scraper.py:198` and the Fotocasa
  equivalent stays as the end-of-results signal). The existing
  `details_enriched` / `details_failed` counters on the scraper
  stay; the `ingest_max_listings` setting is repurposed as the
  default for `max_detail_fetches` and kept on the deprecated list
  for one sprint for back-compat.
- **Pillar B — Two-tier ingest in the scraper subagent.** Split the
  subagent's "call `fetch_listing` for every card" loop into:
  1. **Cheap-card ingest** — for every card from `search_listings`,
     upsert a *minimal* row with `external_id`, `url`, `title`,
     `price_eur`, `size_m2`, `rooms`, and the address. The new
     `ApartmentRepository.upsert_card(...)` method on the port
     takes a partial `Apartment` and only writes the cheap fields
     (the rest stay `NULL`). Postgres-side, this is a new
     `upsert_card_only(...)` SQL helper that touches the same row
     as a later full `upsert` would — so a deep-fetched follow-up
     run *upgrades* the row to the full set, no duplicate.
  2. **Deep ingest** — for the LLM's shortlist (at most
     `max_detail_fetches` cards per portal), call `fetch_listing`
     and the existing `ingest_apartment` flow. The existing
     backfill semantics (Sprint 3 Pillar D) already turn a re-visit
     of a cheap-only row into a full row, so no new repository
     logic is needed for the upgrade path.
- **Pillar C — Prompt rewire.** Both subagent prompts
  (`prompts/idealista_scraper.md`, `prompts/fotocasa_scraper.md`)
  change:
  1. "Stop at 50 cards" guidance is removed. The subagent walks
     every page.
  2. New guidance: "for every card, call `ingest_card` (cheap
     fields only). Then, **after** the cheap pass, pick at most
     `max_detail_fetches` cards that look most promising on the
     cheap signals (price, size, rooms, address) and call
     `fetch_listing` + `ingest_apartment` for those. Prefer cards
     with `price_eur < median * 0.8` or that match the
     operator-known good neighborhoods from the researcher output."
  3. The handoff adds `cards_walked` and `cards_deep_fetched`
     counters alongside the existing `inserted / duplicates /
     filtered / soft_extracted`.
- **Pillar D — Soft time budget as a safety net.** Even with the
  cap on detail fetches, a misbehaving portal could return tens of
  thousands of cheap cards. Add a per-scraper `search_time_budget_s`
  (default 120s) that bounds the *search* phase wall time. The
  `search_listings` iterator checks `time.monotonic()` between pages
  and stops cleanly with a structured log line when the budget is
  exhausted, signalling "tail truncated by time budget" in the
  handoff. Detail fetches have their own cap
  (`max_detail_fetches`) and are not affected.
- **Pillar E — Run-report and acceptance surface.** The `RunReport`
  carries the new counters per portal:
  `cards_walked`, `cards_deep_fetched`, `cards_cheap_only_ingested`,
  `cards_deep_ingested`, `cards_already_known`,
  `dedup_siblings_collapsed`, plus the existing
  `inserted / duplicates / filtered / soft_extracted` and the
  Sprint 4 `details_enriched / details_failed`. The
  `validate-quality` script grows a "cheap-only row coverage" check
  so the operator can see the share of rows that are still waiting
  for a deep fetch (and re-run with a higher `max_detail_fetches`
  to upgrade them).

**Scope (continued) — ranker & verifier additions.**

- **Pillar F — Top-N: 5 → 10.** The ranker already takes `top_n`
  as a config knob (`Settings.rank_top_n`, default 5 — see
  `config.py:102` and `main.py:152`). Bumping the default to
  `10` is a one-line change in `config.py`; the notifier and the
  email template consume whatever the ranker returns, so no
  caller changes. The handoff's `top_n_returned` field
  (already emitted by `_DeterministicSteps`) reports the actual
  count, which the email body uses verbatim. The 5 → 10 bump is
  the smallest possible change, but it is paired with Pillar G
  because a larger top-N makes the existence check
  proportionally more valuable: more rows means more chances
  for a stale URL to slip through to the email.
- **Pillar G — Verifier step: every ranked URL must still
  exist.** A new deterministic phase `verifier` runs **after**
  the ranker and **before** the notifier. For each of the
  top-N rows, the verifier confirms the URL still resolves
  to a live listing, and surfaces a `verification_status`
  field per row in the run report. The phase has three
  outcomes per row:
  1. `live` — the detail page returns 200 (or the portal's
     equivalent) and the apartment's `external_id` is still
     present in the HTML / JSON.
  2. `changed` — the page is live but the listing was
     materially edited (price, size, rooms, bathrooms, or
     `lat`/`lng` differ from the DB row by more than a
     threshold). The verifier updates the DB row with the
     fresh fields, re-scores the apartment using the existing
     `RankableApartment` pipeline, and emits a
     `verification_changes` diff in the run report.
  3. `dead` — the page returns 404, the listing was
     delisted, or the URL is redirected to a search page
     with no match. The row is dropped from the top-N
     entirely; the next-best row from the ranker's
     pre-dedup top-N×2 list is promoted into the email
     so the operator still gets 10.
  Implementation strategy: a per-portal `verify_url` hook
  on the `ScraperPort` interface, with two concrete
  implementations. `FotocasaScraper` reuses its existing
  `httpx` session for a cheap `GET` of the listing URL
  (Fotocasa does not gate the detail endpoint on
  DataDome-like challenges). `IdealistaScraper` reuses the
  **same** playwright `BrowserContext` from Sprint 4
  Pillar A — one shared context, ten page loads against
  `/inmueble/<id>/`, no new browser launch. The
  `ScraperPort.verify_listing(url) -> VerificationResult`
  method is the only port expansion in Sprint 5.
  The verifier phase has its own soft time budget
  (`verifier_time_budget_s`, default 60s) and runs the
  10 fetches in parallel via `asyncio.gather` (the same
  pattern as the Sprint 4 B.2 axis). On time-budget
  exhaustion, unverifiable rows are kept in the email
  with `verification_status="unknown"` and a
  `verification_warning` field — the operator still
  gets the recommendation, but the email body flags it
  as "could not be re-verified in time, click before
  trusting."

**Out of scope (explicitly deferred).**

- A third scraping source. The two-tier model is added per portal;
  when a third portal lands it gets the same shape.
- Embeddings activation. Q2 stays open.
- Route-based distance (OSRM/Mapbox). Haversine only.
- Multiprocessing / multi-process workers. One event loop is enough
  at the current scale.
- VPS / production migration. The full-coverage model is
  compatible with the VPS move (a VPS has more headroom for the
  longer search walk) but the migration itself is a separate
  concern.

**Why this is Sprint 5, not a Pillar of Sprint 4.** It is
logically orthogonal to Sprint 4's parallel-execution work — the
parallel `fetch_listing` calls still apply, just on the LLM's
shortlist instead of every card. It is an additive change to the
scraper + prompts + one new port method (`upsert_card`); no schema
migration, no new port, no change to the ranker. The verifier
phase in Pillar G is the only Sprint 5 piece that expands the
`ScraperPort` (one new method), and the expansion is a
verification hook — no change to search/detach behavior. The
sprint pairs naturally with the production migration: the daily
cron on a VPS has more headroom, and the "walk every page,
deep-fetch the shortlist, verify before emailing" throughput
profile is exactly what a VPS can absorb.

### Sprint 6 (optional) — Production
Migrate the compose + cron to a VPS, make LangSmith tracing durable in the
production environment, add backoff/retry and collection-failure alerting.
Only undertaken if the daily loop is stable enough to leave running.

## ADRs
- ADR-001 — Framework: Deep Agents
- ADR-002 — LLM: Groq primary + opencode-go fallback
- ADR-003 — Database: Postgres + pgvector
- ADR-004 — Execution: agent-driven, DIY free scraping
- ADR-005 — Per-subagent filesystem: CompositeBackend + prefix-forcing tools + prompt
- ADR-006 — Researcher subagent + dangerous-neighborhoods constants table
- ADR-007 — Haversine distance provider
- ADR-008 — Gmail SMTP notifier
