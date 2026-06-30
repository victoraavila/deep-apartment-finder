# Sprint 5 — Full coverage: walk every page, deep-fetch the shortlist

**Goal:** remove the per-portal `ingest_max_listings=50` cap on the
search phase and replace it with a *detail-fetch* cap. The scraper
walks every page the portal returns for the configured hard filters;
the LLM picks a shortlist of cards to deep-fetch and ingest with
the full normalized `Apartment`; every other card lands in the DB
as a cheap-card-only row so cross-portal dedup and historical
backfill still work.

This sprint intentionally excludes embeddings activation (Q2 stays
open), a third scraping source, route-based distance, the VPS /
production migration, and any change to the ranker or notifier. See
`ROADMAP.md` for the abstract view of later sprints — this
document does not repeat that material.

## Resolved decisions (from `ROADMAP.md` and prior work)

| # | Question | Resolution |
| --- | --- | --- |
| Q7 | Should the search phase be capped at a hard number? | **No.** The cap is on the detail-fetch phase, not the search. The portal's sort order is not a quality signal, the cap biases the result set against the operator's goals, and the cap silently breaks the cross-portal dedup guarantee. |
| Q8 | Where should the budget live? | **Per-portal `max_detail_fetches`** (primary) + **per-scraper `search_time_budget_s`** (safety net). Both configurable on `Settings`. |
| Q9 | What happens to the cards that are NOT deep-fetched? | **Cheap-card-only ingest.** A new `ApartmentRepository.upsert_card(...)` port method upserts a partial row with `external_id`, `url`, `title`, `price_eur`, `size_m2`, `rooms`, and address. A later deep-fetch upgrades the row in place via the existing backfill semantics (Sprint 3 Pillar D). |

## Context

### The problem today

Both scrapers are constructed with
`max_cards=settings.ingest_max_listings` (default 50, see
`config.py:55` and `main.py:85-94`). The `search_listings` async
iterator stops the moment `yielded >= max_cards`, so a Zaragoza run
that returns 175 Fotocasa cards and ~500 Idealista cards across 17
pages effectively buys the portal's recency-sorted page 1 — and only
page 1. The current run log (`f7a0ebe8-...` on `main`, captured
2026-06-29 23:53) shows Idealista stopping mid-page-3 with
`cumulativeInspected=60 yielded=30` from page 2, and no page-3 log
line because the cap fires *before* the next request.

This is selection bias masquerading as a budget. Neither portal
guarantees a sort order that puts the operator's most-likely-to-want
listings on page 1. The default is "most recent" × "promoted", a
poor proxy for fit. A 95/100 apartment in a competitive building
rotates off page 1 within hours; a stale 60/100 listing sits on
page 1 for a week. A 50-card cap without a sort contract is
guaranteed to miss better-than-median listings that are simply
older.

The cap also breaks the cross-portal dedup guarantee (Sprint 3
Pillar F / ADR-012). Dedup_key only catches the cross-portal
sibling if *both* portals have ingested it. A Fotocasa-only
listing that has an Idealista sibling on page 3 is silently
"not deduplicated" because Idealista never walked to page 3 — the
ranker treats it as a Fotocasa row and never sees the
`idealista.com` URL. The first 60 cards happen to be the page-1 +
page-2 intersection for these two portals in practice, but the
guarantee is not contractual.

### Why the cap exists today (and why it's the wrong knob)

Two reasons that turn out to be the *wrong* axes to cap on:

- **Scraper HTTP time.** Dominated by the polite delay between
  pages (2s for Idealista, configurable). Walking all 17 pages of
  an Idealista result set is ~34s of delay, not the wall-time the
  cap was supposed to bound. This is a small cost relative to the
  detail-page fetches, which take 1–3s *per card* and dominate
  the per-portal runtime.
- **LLM tokens.** The LLM extracts `pet_policy` and `furnished`
  from each listing's `description` at ingest time. Tokens scale
  with *detail fetches*, not with *cards seen*. Capping search at
  50 is a proxy for capping detail work, but the wrong proxy — it
  caps the cheap thing (cards) to limit the expensive thing
  (detail fetches), with the side effect of starving the
  cross-portal dedup.

### The right budget shape

The cost asymmetry is the key insight:

- **Search** is 1 GET per page × N pages, with a polite delay of
  2s between pages. Cost: ~2s × N. For an Idealista result set
  of 17 pages, that's 34s of polite-delay wait. Cheap, low risk.
- **Detail** is 1 GET (or 1 playwright render in Idealista's case)
  per card. Cost: 1–3s per card × N cards. The risk: a DataDome
  challenge per request, which can fail mid-batch and force a
  retry.

So the right cut is:

- **Walk the full search space** (cheap, low risk, gives us the
  population).
- **Detail-fetch a *predetermined* tail** — the cards the
  LLM/ranker would actually want to know more about.
- **Bulk-ingest the cheap signals** (price, m², rooms, address,
  photo) on the rest so the DB still grows and cross-portal dedup
  still works.

## Scope

The sprint is organised into five pillars. Each pillar is
independently shippable; Pillar A is the highest-leverage one-line
change and the one the next-sprint test will assert on.

### Pillar A — Drop the search cap, add the detail cap

- Remove `max_cards=settings.ingest_max_listings` from the
  `*Scraper(...)` constructor calls in `main.py:85-94`. The
  `search_listings` async iterator walks every page until the
  portal returns an empty / short page. The existing
  `len(cards) < 15` end-of-results heuristic at
  `scraper.py:198` and the Fotocasa equivalent stay.
- Add `max_detail_fetches: int = 25` to `Settings` (per-portal
  default). The subagent prompt reads this number and respects it
  as the upper bound on `fetch_listing` calls per portal per run.
- Keep `ingest_max_listings` on `Settings` for one sprint as a
  deprecated alias for `max_detail_fetches`. The CLI logs a
  one-line deprecation warning when only the old key is set.
- The scraper's internal `_max_cards` cap (the `yielded >= max_cards`
  check at `scraper.py:143` and `:193`) is *not* removed
  wholesale — it stays as a defensive backstop in case a future
  scraper forgets to bound detail fetches and the cap is the
  only thing protecting the run. It just stops being wired up at
  composition time in `main.py`.

### Pillar B — Two-tier ingest in the scraper subagent

Split the subagent's "call `fetch_listing` for every card" loop
into two passes:

1. **Cheap-card pass** — for every card from `search_listings`,
   call a new `ingest_card(card)` tool. The tool builds a
   *partial* `Apartment` (cheap fields only: `external_id`, `url`,
   `title`, `price_eur`, `size_m2`, `rooms`, `address`, `source`)
   and calls a new `ApartmentRepository.upsert_card(...)` method.
   `upsert_card` writes only those fields and leaves the rest
   `NULL`. It uses the same `dedup_key` collision semantics as the
   existing `upsert`, so cross-portal siblings collide correctly
   at the cheap-card stage and the ranker can still see the
   `idealista.com` URL even if the deep-fetch never fires.
2. **Deep pass** — for the LLM's shortlist (at most
   `max_detail_fetches` cards per portal), call `fetch_listing`
   and the existing `ingest_apartment` flow. The existing
   backfill semantics (Sprint 3 Pillar D) already turn a re-visit
   of a cheap-only row into a full row, so no new repository
   logic is needed for the upgrade path. `dedup_key` collisions
   at the deep-pass stage still drop the lower-scored sibling via
   `dedup_top_n_by_key`.

The cheap-card pass and the deep pass run as **two phases of the
subagent loop**, not two separate subagent calls. The
orchestrator's contract (one `task` call → one handoff) is
preserved.

- New port method on `ApartmentRepository`:
  `upsert_card(card: ListingCard) -> UpsertResult`. Returns
  `inserted | duplicate | updated` with the same shape as the
  existing `upsert(...)`.
- New SQL helper in
  `adapters/postgres/repository.py`: `upsert_card_only(...)` that
  `INSERT ... ON CONFLICT (dedup_key) DO UPDATE` for the cheap
  fields only. The existing `upsert` SQL is unchanged.
- New tool `ingest_card` in `tools/ingest/` (and a per-portal
  thin wrapper if the LLM's tool descriptions diverge).
- The InMemory `ApartmentRepository` test double gets a matching
  `upsert_card` method so unit tests do not have to mock the
  Postgres path.

### Pillar C — Prompt rewire

Both subagent prompts
(`subagents/prompts/idealista_scraper.md`,
`subagents/prompts/fotocasa_scraper.md`) change. The Idealista
prompt is the canonical example because the cap visibly truncates
its result set today (the run log shows it stopping mid-page-3).
The Fotocasa prompt gets the same edits for symmetry; the
`validate-quality` per-source field coverage is the diff
operator-visible at the end of the sprint.

- **"Walk every page" guidance.** The "you stop when either
  you've ingested up to the orchestrator's cap" sentence is
  removed. The new stop condition is one of:
  (a) `search_listings` returned an empty / short page (existing
  heuristic), (b) the `search_time_budget_s` budget was hit
  (Pillar D), or (c) the LLM hit `max_detail_fetches` for the
  deep pass.
- **Cheap-card-first loop.** The "For each card that looks
  promising, call `fetch_listing`" step is replaced with:
  1. For every card, call `ingest_card` (cheap fields only).
  2. After the cheap pass, pick at most `max_detail_fetches`
     cards that look most promising on the cheap signals
     (`price_eur < median * 0.8`, `size_m2` near the operator's
     target, address in the researcher-curated good-neighborhood
     list) and call `fetch_listing` + `ingest_apartment` for
     those.
  3. The selection criterion is *soft* — the LLM is told to use
     judgment, not to follow a strict formula — but the cap is
     hard.
- **Handoff counters.** The handoff adds
  `cards_walked: <int>`, `cards_deep_fetched: <int>`,
  `cards_cheap_only: <int>` (cheap rows that did not get
  upgraded), alongside the existing
  `inserted / duplicates / filtered / soft_extracted` and the
  Sprint 4 `details_enriched / details_failed`.
- **Time-budget signal.** A new handoff field
  `search_truncated_by_time_budget: <bool>` flags runs where
  Pillar D kicked in.

### Pillar D — Soft time budget as a safety net

A misbehaving portal could return tens of thousands of cheap
cards. The detail cap is not enough — the *search* walk itself
becomes the bottleneck in that pathological case. Add a
per-scraper `search_time_budget_s: int = 120` on `Settings`. The
`search_listings` iterator:

- Reads `time.monotonic()` at the top of the page loop.
- If the budget is exhausted, logs
  `idealista search: time budget exhausted after page=N, cards_walked=K`
  and returns cleanly.
- The `scraper.search_listings` port method gains a new
  structured result attribute
  `truncated_by_time_budget: bool` (set on the *last* iterator
  yield, threaded through the existing iterator protocol) so
  the subagent can flag it in the handoff. (Implementation note:
  this is a thin wrapper; the underlying check is in
  `IdealistaScraper.search_listings` and the Fotocasa
  equivalent. The existing async iterator protocol is preserved.)

The time budget applies only to the *search* phase. Detail
fetches are bounded by `max_detail_fetches` and the per-card
playwright timeouts from Sprint 4.

### Pillar E — Run-report and acceptance surface

- The `RunReport` (the JSON the CLI writes under
  `/orchestrator/reports/<run_id>.json`) carries the new counters
  per portal:

  ```json
  {
    "scrapers": {
      "fotocasa": {
        "cards_walked": 175,
        "cards_deep_fetched": 25,
        "cards_cheap_only_ingested": 142,
        "cards_deep_ingested": 25,
        "cards_already_known": 8,
        "search_truncated_by_time_budget": false,
        "inserted": 150, "duplicates": 8, "updated": 0,
        "filtered": 17, "soft_extracted": 25,
        "details_enriched": 0, "details_failed": 0
      },
      "idealista": {
        "cards_walked": 510,
        "cards_deep_fetched": 25,
        "cards_cheap_only_ingested": 478,
        "cards_deep_ingested": 25,
        "cards_already_known": 7,
        "search_truncated_by_time_budget": false,
        "inserted": 32, "duplicates": 0, "updated": 0,
        "filtered": 3, "soft_extracted": 25,
        "details_enriched": 25, "details_failed": 0
      }
    },
    "dedup_siblings_collapsed": 11
  }
  ```

  The keys are present even when the per-portal scraper is
  disabled (zeroed), so downstream consumers do not have to
  branch.

- The `validate-quality` script (`scripts/validate_quality.py`
  or equivalent) grows a "cheap-only row coverage" check: for
  each source, the share of rows where `description IS NULL OR
  bathrooms IS NULL` (the cheap-only signature). The operator
  can read this as "X% of rows are still waiting for a deep
  fetch; re-run with `--max-detail-fetches 50` to upgrade them."
- The `show-run` CLI subcommand (Sprint 3 Pillar A) shows the
  new counters in its terminal summary.

## Concurrency model

The Sprint 4 concurrency story (ADR-013) is unchanged and
composes cleanly:

- **Across portals (B.1).** `run_scrapers` still uses
  `asyncio.gather` on the two subagent graphs. The new
  cheap-card pass inside each subagent is one extra tool call
  per page × N pages; the parallel `fetch_listing` calls on the
  LLM's shortlist (B.2) still apply.
- **Across detail fetches in one subagent (B.2).** The
  `max_detail_fetches` cap is the *only* thing that changed.
  The LLM still fires N parallel `fetch_listing` calls in a
  single batch where N ≤ `max_detail_fetches`.
- **Inside the Idealista `fetch_listing` (B.3).** Unchanged.

The cheap-card pass is serial inside one subagent by design:
`ingest_card` is one short Postgres write per card, and serial
keeps the per-card log line order meaningful for the operator.
The Postgres pool is the contention point, and it is not
saturated by cheap-card writes (the asyncpg pool's
`max_size` from `config.py` covers it).

## Database schema

No new migrations. The `apartments` table already has nullable
`description`, `bathrooms`, `lat`, `lng`, `pet_policy`,
`furnished`, `dedup_key`. The cheap-card pass writes only the
non-nullable columns (`external_id`, `url`, `title`, `price_eur`,
`size_m2`, `rooms`, `address`, `source`, `dedup_key`); the
nullable ones stay `NULL` until the deep pass upgrades the row.

## Package layout (additions on top of Sprint 4)

```
src/deep_apartment_finder/
  config.py                            # + max_detail_fetches, search_time_budget_s
  ports/
    apartment_repository.py            # + upsert_card(...) abstract method
  adapters/
    postgres/
      repository.py                    # + upsert_card_only(...) SQL helper
  tools/
    ingest/
      ingest_card.py                   # new tool factory
  subagents/
    prompts/
      idealista_scraper.md             # updated: walk every page, two-tier
      fotocasa_scraper.md              # updated: same shape for symmetry
  scripts/
    validate_quality.py                # + cheap-only row coverage check
```

The `ScraperPort` (`ports/scraper.py`) is unchanged — the
time-budget signal is a private detail of the concrete scraper.
`IdealistaScraper` and `FotocasaScraper` are unchanged on the
outside; their `search_listings` iterator grows a single
`time.monotonic()` check at the top of the page loop. The
ingest layer is the only thing that changes shape.

## LLM usage in Sprint 5

- **`fotocasa_scraper` + `idealista_scraper` subagents:** the
  cheap-card pass is **no LLM** — the partial `Apartment` is
  built from the search-card fields only, so `ingest_card` is a
  pure-Python call. The deep pass still goes through the LLM
  for soft-field extraction (`pet_policy`, `furnished`). The
  net LLM cost per run is bounded by `max_detail_fetches` per
  portal, not by `ingest_max_listings`.
- **Selection step:** the LLM picks the shortlist of cards to
  deep-fetch. This is a single LLM call per portal, fed the
  full card list as context. The token cost is bounded by
  `cards_walked` × (avg card size) — for 500 Idealista cards at
  ~200 tokens each, that's ~100k input tokens per selection
  call, which fits comfortably in the LLM's context window.
  The selection prompt is template-driven: "pick the top
  `max_detail_fetches` cards on these criteria". The LLM
  returns a JSON list of `external_id`s.
- **`ranker` + `notifier`:** unchanged (deterministic Python).
- **Orchestrator:** unchanged (one `run_scrapers` call).

Sprint 5 *reduces* total LLM tokens for the same portal
coverage, because the cap now bounds the *expensive* path
(detail) instead of the cheap path (search).

## Observability — phase contract

The `RunObserver` (Sprint 3 Pillar A) is unchanged. Sprint 5
emits the same events as Sprint 4, with three new counters per
portal in the handoff and one new flag in the run report:

```
=== scraper (fotocasa) ===
  waiting on LLM
  waiting on Fotocasa HTTP
  scraper: walked 6 pages, 175 cards, cheap_ingested 175,
           deep_fetched 25, deep_ingested 25,
           duplicates 8, filtered 17, soft_extracted 25
=== scraper (idealista) ===
  waiting on LLM
  waiting on Idealista HTTP (playwright)
  scraper: walked 17 pages, 510 cards, cheap_ingested 510,
           deep_fetched 25, deep_ingested 25,
           duplicates 7, filtered 3, soft_extracted 25
           details_enriched=25 details_failed=0
=== ranker ===
  ranker: scored 84 apartments, wrote 252 score rows, top 5
=== orchestrator ===
  dedup_siblings_collapsed: 11
  search_truncated_by_time_budget: false
```

The RecordingRunObserver already handles per-portal counters
from Sprint 4, so no new observer code is needed.

## Acceptance criteria

1. **Search walks every page.** A run that includes a portal
   whose search returns N pages (N > 1) hits N `page=N` log
   lines per portal before the iteration stops. The
   `cards_walked` counter in the handoff equals the sum of
   `cumulativeInspected` at the end of the search. A unit test
   with a fake `ScraperPort` that returns K pages of 30 cards
   each asserts the iterator walks all K pages.
2. **Detail cap is respected.** A run that includes a portal
   whose search returns > `max_detail_fetches` cards does not
   issue more than `max_detail_fetches` `fetch_listing` calls
   per portal. A unit test with a fake `ScraperPort` and a
   card list of 100 cards asserts the LLM-facing tool wrapper
   surfaces at most `max_detail_fetches` URLs to
   `fetch_listing`.
3. **Cheap-card row exists in the DB.** A cheap-card-only
   `ingest_card` call produces a row in `apartments` with the
   cheap fields populated and the deep fields (`description`,
   `bathrooms`, `lat`, `lng`, `pet_policy`, `furnished`)
   `NULL`. The `dedup_key` is set the same way as the full
   `upsert`.
4. **Cross-portal dedup works at the cheap stage.** Two portals
   that surface the same physical apartment at the cheap stage
   (no detail fetch yet) collide on `dedup_key`, and the
   ranker's `dedup_top_n_by_key` drops the lower-scored
   sibling. A test with two fake `ScraperPort`s returning the
   same `dedup_key` for one card asserts the ranker sees one
   row, not two.
5. **Deep pass upgrades the cheap row.** A `fetch_listing` +
   `ingest_apartment` call on a card that already has a
   cheap-only row in the DB produces an `updated` upsert
   result (Sprint 3 Pillar D semantics). The cheap fields
   stay populated; the deep fields are now populated. The
   `dedup_key` is unchanged. A test asserts the row's
   `description`, `bathrooms`, `lat`, `lng` go from
   `NULL → value`.
6. **Time budget is honoured.** A fake `ScraperPort` that
   sleeps 5s per page, with `search_time_budget_s=12`, stops
   after 3 pages. A test asserts the iterator returns early
   and sets `truncated_by_time_budget=True` on the
   structured handoff signal.
7. **`ingest_max_listings` deprecation.** A run started with
   `INGEST_MAX_LISTINGS=50` and no
   `MAX_DETAIL_FETCHES=...` logs a one-line deprecation
   warning and applies the value to `max_detail_fetches`. A
   test asserts the warning is emitted exactly once per
   process.
8. **`validate-quality` cheap-only coverage.** The script
   reports a per-source `cheap_only_share` field. A test
   with a seeded mix of cheap-only and full rows asserts the
   share is computed correctly.
9. **No regression on Sprint 4 acceptance criteria.** All
   Sprint 4 acceptance criteria still pass: parallel
   subagents, parallel detail fetches, `bathrooms` populated
   on Idealista, graceful degradation when playwright is
   absent. The only diff is the search no longer caps at 50.
10. **Run report carries the new counters.** A run report
    produced by the new code path has `cards_walked`,
    `cards_deep_fetched`, `cards_cheap_only_ingested`,
    `cards_deep_ingested`, `cards_already_known`,
    `search_truncated_by_time_budget` per portal, and a
    top-level `dedup_siblings_collapsed`. A test asserts the
    JSON shape and value ranges.

## Definition of done

- All acceptance criteria pass on a clean local machine.
- Unit tests cover:
  - `ApartmentRepository.upsert_card` happy path
    (cheap-card row inserted, deep fields `NULL`),
    duplicate path (same `dedup_key` → `duplicate` result,
    row unchanged), and upgrade path (full `upsert` on an
    existing cheap-only row → `updated` result, deep
    fields populated, cheap fields preserved).
  - The new `ingest_card` tool factory (parameter wiring,
    `UpsertResult` mapping).
  - The two-tier subagent loop (cheap pass for every
    card, deep pass for at most `max_detail_fetches`
    cards, parallel `fetch_listing` calls preserved).
  - The `search_listings` time-budget check (stops
    cleanly when the budget is exhausted, sets the
    truncation flag).
  - The `validate-quality` cheap-only coverage check.
- One new integration test exercises the full
  orchestrator → `run_scrapers` (with both
  `fotocasa_scraper` + `idealista_scraper`) → `ranker` →
  `notifier` flow with fake adapters that return
  multi-page result sets, asserts the run report carries
  the per-portal `cards_walked` and `cards_deep_fetched`
  counters, and asserts cross-portal dedup still works at
  the cheap stage.
- New ADR: **ADR-014 — Two-tier ingest: full search walk
  with bounded deep fetches.** The ADR records the
  cost-asymmetry rationale, the cap location (per-portal
  `max_detail_fetches` + per-scraper
  `search_time_budget_s`), the cheap-card-only row shape,
  and the relationship to Sprint 3 Pillar D backfill
  semantics.
- `README.md` updated with: the new
  `MAX_DETAIL_FETCHES` and `SEARCH_TIME_BUDGET_S` env
  vars, the `INGEST_MAX_LISTINGS` deprecation note, the
  expected run-report shape change, and a "What's new in
  Sprint 5" section.
- `.env.example` documents the two new env vars
  (default `25` and `120` respectively) and the
  deprecated alias.
- The Sprint 4 acceptance criteria that were not changed
  by Sprint 5 still pass: parallel subagent execution,
  parallel detail fetches, `bathrooms` populated on
  Idealista, graceful degradation, idempotency,
  `dedup_key` semantics, the OCP smoke test (no port
  expansion visible to the orchestrator).
