# Sprint 4 — Idealista detail-page upgrade + parallel scraper execution

**Goal:** unblock every Idealista row that the current `min_bathrooms=2`
hard filter kills today, and shave the daily run's wall-clock time by
running the two scraper subagents (and, where useful, individual
detail-page fetches) concurrently. Both items are pre-conditions for
the Sprint 5 production migration: without `bathrooms` on Idealista
rows, the second portal's coverage is half-blind; without parallelism,
the run's wall time will keep growing as more portals are added.

This sprint intentionally excludes embeddings activation (the
nullable `embedding vector(1536)` column stays unused), route-based
distance (OSRM/Mapbox), a third scraping source, paid proxies, and
any VPS / production migration. See `ROADMAP.md` for the abstract
view of later sprints — this document does not repeat that material.

## Resolved decisions (from `ROADMAP.md` and prior work)

| # | Question | Resolution |
| --- | --- | --- |
| Q2 | Purpose of embeddings | **Still deferred.** Sprint 4 takes a different angle: it fixes the second scraper's data quality (Pillar A below) and reduces run time (Pillar B). Embeddings remain a Sprint 5+ decision once the dedup data set is non-trivial. The `embedding` column stays nullable. |
| Detail-page block on Idealista | Pay for Bright Data, pay 2Captcha, or upgrade the scraper? | **Upgrade the scraper.** ADR-011 calls the playwright upgrade out as future work. The HAR captured in Sprint 3 (`docs/idealista_recon/` — see `adr/011-second-scraper-idealista-stealth-strategy.md` §"Future work") confirms the bathroom data is in a stable, machine-readable `<li>1 baño</li>` block once the page is rendered. Sprint 4 implements that upgrade. |
| Concurrency model | Parallel subagents vs parallel pages vs parallel detail fetches | **All three, layered.** See Pillar B and the "Concurrency model" section below. The orchestrator's prompt stops dictating the order of subagent calls and delegates to a new `run_scrapers` tool that fires them concurrently. |

## Context

### The Idealista problem today

ADR-011 records that the Idealista scraper (`adapters/scrapers/idealista/`)
only harvests the **search-card** data; the `/inmueble/<id>/` detail
endpoint is protected by a DataDome device check that a `curl_cffi`
session can never satisfy. The detail page is where `bathrooms` lives —
the search card has it as `None` on every listing. With the operator's
default `HardFilters(min_bathrooms=2)`, the
`domain/filters/hard.py:34-37` predicate rejects every Idealista row,
so the second portal's inventory is invisible to the ranker and the
notifier. The cross-portal dedup (`dedup_key`) catches the Fotocasa
sibling for the same physical apartment, but the second portal is
*adding nothing* to coverage.

The Spring 3 capture of the listing at
`https://www.idealista.com/inmueble/111886330/` (HAR file under
`.context/attachments/`) shows the 200 response carries a clean,
stable block:

```html
<div class="details-property_features">
  <ul>
    <li>70 m² construidos</li>
    <li>2 habitaciones</li>
    <li>1 baño</li>
    <li>Segunda mano/buen estado</li>
    <li>Amueblado y cocina equipada</li>
  </ul>
</div>
```

There is no JSON-LD and no `__NEXT_DATA__` blob, so the upgrade is
**render → parse HTML**, not API-shaping. Lat / lng are not on this
block (they live behind a second click in the real UI); `bathrooms`,
`rooms`, and `size_m2` *are* there, and that's the gap Sprint 4
closes.

### The runtime problem today

Sprint 3 deliberately defers parallelism — the orchestrator's prompt
notes that the two scraper subagents "may run sequentially (Fotocasa
first, then Idealista) or in a single round if the subagents are
independent; either is fine". In practice the LLM-driven
orchestrator's `task` tool calls each subagent one at a time, so the
two scrapers run serially. As we add a third portal, the serial
schedule is the bottleneck.

Inside each subagent, the same pattern repeats: the LLM calls
`search_listings`, gets the full list back in one tool call, then
loops `fetch_listing` + `ingest_apartment` one card at a time. For
Idealista, `fetch_listing` re-paginates the search until it finds the
target card (per `adapters/scrapers/idealista/scraper.py:151-201`),
which makes a 3-page walk take 6+ seconds for cards deep in the
result set. With the playwright detail-page upgrade, the per-card
work changes shape — `fetch_listing` becomes a real HTTP round-trip
on the detail URL, so the inner loop has more wall time per card
and benefits more from concurrency.

## Scope

The sprint is organised into two pillars. Each is independently
shippable; Pillar A is the higher-value deliverable and the one the
next sprint test will assert.

### Pillar A — Idealista detail-page upgrade (playwright path)

- New module `adapters/scrapers/idealista/detail_client.py` exposing
  `fetch_detail_html(url) -> str | None` built on `playwright`'s
  async API. **One `BrowserContext` per `IdealistaScraper` instance**
  — created lazily on the first `fetch_detail_html` call, reused
  for every subsequent detail fetch, and closed in `scraper.close()`.
  This keeps the browser's DataDome trust across pages, which is the
  whole point of using a real browser: the same context accumulates
  session signals that the `curl_cffi` session could not.
- New pure function `parse_detail_page(html, url) -> dict | None`
  in `adapters/scrapers/idealista/api.py` extracting `bathrooms`,
  `rooms`, `size_m2` (with a re-parse fallback to the card values
  if the detail block is absent — for the "soft 404" case where the
  listing has been delisted but the URL still resolves), and the
  long-form `description` (which carries more text than the search
  card's truncated `<p class="ellipsis">`). `lat` / `lng` stay
  `None` for Sprint 4; filling them is a separate ticket.
- `IdealistaScraper.fetch_listing(url)` becomes a two-step:
  1. Try the existing search-card walk
     (`adapters/scrapers/idealista/scraper.py:151-201`); if it
     finds the card, return that `Apartment` *enriched* with the
     detail block via `parse_detail_page(...)` when the HTML was
     fetched as a side-effect.
  2. If the search-card walk does **not** find the card
     (because the card scrolled off the visible pages), call
     `fetch_detail_html(url)` directly and parse the result.
  3. If playwright is not installed or the browser launch fails,
     fall back to today's behaviour (search-card walk, no
     `bathrooms`). The "soft" fallback preserves the existing
     "no entry from Idealista is in the DB" failure mode in
     degraded environments.
- The Idealista scraper prompt
  (`subagents/prompts/idealista_scraper.md`) drops the
  "you will reject every Idealista row" caveat. The new handoff
  reports a new counter, `details_enriched` (how many cards the
  detail-page fetch succeeded on), alongside the existing
  `inserted / duplicates / filtered / soft_extracted`.
- ADR-011 §"Future work" is updated to mark the detail-page
  upgrade as delivered. The ADR's "Consequences" section's
  "distance criterion treats Idealista rows as 0.5" bullet
  stays — `lat` / `lng` are still `None`; only the `bathrooms`
  gap is closed.
- Optional: an `IDEALISTA_DETAIL_FETCH=disabled` env var that
  short-circuits the playwright path (e.g. CI runners without a
  Chromium install, or operators who want to test the ranker
  against search-card data only). Default: enabled when
  playwright is importable.

### Pillar B — Parallel scraper execution

Three independent axes of parallelism, layered so each delivers
value on its own.

#### B.1 — Across-portal: run the two scraper subagents concurrently

- New tool `run_scrapers(filters_json: str) -> str` in
  `tools/orchestrator/run_scrapers.py` that the orchestrator calls
  instead of two `task` calls. The tool:
  - Parses the filter brief once, fans out to the
    `fotocasa_scraper` and `idealista_scraper` subagent graphs
    concurrently via `asyncio.gather(...)`, and returns a single
    combined handoff (per-portal counters + totals).
  - Each subagent still owns its own LLM session, its own
    scraper, its own filesystem subtree — the only change is
    that the two sessions are co-scheduled.
  - A failure in one subagent does not cancel the other. The
    tool's return is structured as
    `{ "fotocasa": {...}, "idealista": {...}, "errors": [...] }`
    so the orchestrator can surface per-portal failures in its
    summary without losing the partial result.
- The orchestrator's prompt
  (`subagents/prompts/orchestrator.md`) is updated to call
  `run_scrapers` exactly once, instead of the current "two
  sequential `task` calls" pattern. The cross-portal-dedup
  guarantee in the prompt is unchanged: both portals still
  report their own handoff; the ranker is still the single
  authority on cross-portal siblings.
- The `task` tool stays registered (some operators may want to
  debug a single portal) but is no longer the orchestrator's
  primary path.
- The observer emits `=== scraper (fotocasa) ===` and
  `=== scraper (idealista) ===` interleaved as the two
  subagents make progress — the `RecordingRunObserver` already
  handles out-of-order phase events (it accumulates counts and
  persists the union at `phase_end`).
- Net wall-time saving: the two scraper subagents today run
  for ~2–3 minutes each in series. With `asyncio.gather`, the
  total is `max(t_fotocasa, t_idealista) + LLM_overhead`. For
  the current portals that is a 40–50% reduction in the scraper
  phase.

#### B.2 — Within a scraper: parallelise the detail fetch loop

The Idealista scraper is the only one that *currently* does many
detail fetches per run — every card the LLM picks triggers a
`fetch_listing` (which re-paginates the search today; after Pillar
A it hits the detail endpoint). The Fotocasa scraper does the same
for the cards the LLM picks.

Two sub-deliverables:

- `search_listings` stays serial: a single-page (Fotocasa) or
  paged (Idealista) iteration over the search endpoint with the
  polite delay between pages. Parallel paginating helps little
  here — the polite delay is per page, and the LLM wants the
  cards returned in a single tool call, not as a stream.
- `fetch_listing` becomes fan-out-capable. A new port method
  `fetch_listings(urls: Sequence[str]) -> list[Apartment]`
  is **not** added; instead, the LLM-facing tool
  (`tools/idealista/fetch_listing.py`,
  `tools/fotocasa/fetch_listing.py`) keeps its single-URL
  signature and the LLM is free to make N parallel tool calls
  in a single batch — most modern LLM clients support this
  natively and Deep Agents preserves parallel tool calls in
  the agent's tool loop. Sprint 4 *documents* this in the
  scraper prompts ("you may call `fetch_listing` for several
  cards in a single batch; the framework will execute them
  concurrently") and verifies the per-portal async session
  (curl_cffi for Idealista, httpx for Fotocasa) is
  concurrency-safe — both libraries are, as long as the
  session is shared.

#### B.3 — Within Idealista: parallelise the page walk

The current `fetch_listing` walks search pages serially until it
finds the target card (`scraper.py:179-201`). For cards that
have scrolled to page 4+ this is 4× polite-delay seconds. The
upgrade in Pillar A replaces this walk with a single detail
fetch and removes the cost entirely, so B.3 is
**automatically delivered by Pillar A** and does not need its
own work item. Documented here so the next reader does not
look for a separate "parallelise the page walk" ticket.

### Concurrency model

The Sprint 4 concurrency story is the union of B.1 + B.2, plus
the implicit B.3:

| Axis | Mechanism | Boundary | Failure mode |
| --- | --- | --- | --- |
| Across portals (B.1) | `asyncio.gather(...)` of the two subagent graphs in the new `run_scrapers` tool | Inside the orchestrator process; one Python event loop | Subagent exception → other still completes; the exception is captured and surfaced in the handoff |
| Across detail fetches in one subagent (B.2) | LLM-side parallel tool calls on the existing single-URL `fetch_listing` | Inside the subagent's LLM session | Per-card error handled by the tool's existing try/except |
| Inside the Idealista `fetch_listing` (B.3) | Detail-page fetch replaces the page walk (Pillar A) | Inside the scraper | `fetch_detail_html` failure falls back to search-card walk |

There is no multiprocessing. Everything is one Python event loop,
one Postgres pool (which is already concurrent — the
`PostgresApartmentRepository.upsert` uses `async with
self._pool.acquire()` and asyncpg serialises per-connection
transactions), and one set of HTTP sessions. The InMemory
repository's `_by_source_ext` dict is touched only under
coroutines — single-thread asyncio is safe.

### Out of scope (explicitly deferred)

- **Embeddings activation.** The `embedding vector(1536)` column
  stays nullable. Sprint 5+ decides the use case (Q2) and
  populates it.
- **Route-based distance (OSRM/Mapbox).** Haversine only. A
  future sprint can add `OsrmDistanceProvider` behind the same
  `DistanceProvider` port.
- **`lat` / `lng` on Idealista rows.** The detail block has
  rooms, bathrooms, size, and description; the geo coordinates
  are loaded by a second click in the real UI and are not in
  the SSR HTML. Filling them is a separate ticket — likely a
  second `playwright` click on the map widget, or a
  GeoJSON-on-static-asset scraper. Distance criterion still
  scores Idealista rows at 0.5 (neutral) until then.
- **A third scraping source.** The parallelism story makes a
  third portal cheap to add (one more subagent + one more
  `asyncio.gather` arg), but adding Pisos.com or another
  fallback is its own product decision.
- **Multiprocessing / multi-process workers.** One event loop
  is enough at the current scale. The single Postgres pool is
  the contention point, and it is not saturated.
- **Retry / backoff on scraper or SMTP failure.** The cron
  re-fires the next day.
- **VPS / production migration.** Sprint 5.

## Database schema

No new migrations. The `apartments` table already has
`bathrooms int` and `dedup_key text`; the detail-page upgrade
populates `bathrooms` more often, and the ranker can then score
the row against the `min_bathrooms` filter normally. Existing
NULL `bathrooms` rows are unchanged (the duplicate-backfill
semantics from Sprint 3 Pillar D still apply: a re-scrape with
a non-None `bathrooms` value backfills the column).

## Package layout (additions on top of Sprint 3)

```
src/deep_apartment_finder/
  adapters/
    scrapers/
      idealista/
        detail_client.py        # playwright BrowserContext wrapper
  tools/
    orchestrator/
      run_scrapers.py           # asyncio.gather of the two subagents
  subagents/
    prompts/
      orchestrator.md           # updated: use run_scrapers
      idealista_scraper.md      # updated: drop bathroom caveat
      fotocasa_scraper.md       # updated: parallel fetch_listing hint
```

The `ScraperPort` (in `ports/scraper.py`) is unchanged — the
detail-page upgrade is a private detail of `IdealistaScraper`,
not a port expansion. `FotocasaScraper` is unchanged. The
`ingest_apartment` tool, the soft-criteria registry, the
ranker, and the notifier are all unchanged — they are
source-agnostic by construction (S1/S2/S3).

## LLM usage in Sprint 4

- **`researcher` subagent:** unchanged (S2). One LLM call on the
  first run only; no-op afterwards.
- **`fotocasa_scraper` + `idealista_scraper` subagents:** the
  LLM extracts `pet_policy` and `furnished` from each listing's
  `description` at ingest time (S2/S3 pattern, reused verbatim).
  The Idealista subagent's prompt changes: it now reads a
  longer `description` (from the detail page) and can extract
  more reliable `pet_policy` / `furnished` signals.
- **`ranker` + `notifier`:** still deterministic Python, no LLM
  at rank / notify time.
- **Orchestrator:** the LLM call pattern changes from
  "two sequential `task` calls" to "one `run_scrapers` call".
  The number of LLM round-trips drops (one fewer decision step
  in the orchestrator's plan).

Sprint 4 does not introduce any new LLM call site. The
`run_scrapers` tool wraps existing LLM sessions; it does not
add reasoning.

## Observability — phase contract

The `RunObserver` is unchanged. Sprint 4 emits the same events
as Sprint 3, with two new counters in the per-portal handoff:

```
=== scraper (fotocasa) ===     (runs concurrently with idealista)
  waiting on LLM
  waiting on Fotocasa HTTP
  scraper: fetched 12 pages, 47 cards, 41 inserted, 6 duplicates
=== scraper (idealista) ===    (runs concurrently with fotocasa)
  waiting on LLM
  waiting on Idealista HTTP (playwright)
  scraper: fetched 9 pages, 33 cards, 28 inserted, 5 duplicates
  scraper: details_enriched=33 details_failed=0
=== ranker ===
  ranker: scored 121 apartments, wrote 363 score rows, top 5
```

The two `=== scraper (...) ===` blocks interleave on stderr as
the subagents make progress. The `RecordingRunObserver` already
handles out-of-order phase events (it accumulates counts and
persists the union at `phase_end`), so no new observer code is
needed. LangSmith's per-subagent traces become siblings under
the orchestrator's parent trace instead of a strict
"fotocasa → idealista" chain — LangSmith supports this
natively.

## Acceptance criteria

1. **Idealista detail-page enrichment.** A run that includes
   Idealista populates `bathrooms` for at least 90% of
   ingested rows (the gap is the `parse_detail_page` returning
   `None` for delisted listings). The
   `validate-quality` per-source field coverage report shows
   `bathrooms` non-null rate ≥ 90% on `source='idealista'`
   rows; the prior Sprint 3 baseline was 0%.
2. **Hard filter no longer kills Idealista.** A run with
   `min_bathrooms=2` ingests at least 10 Idealista rows when
   the search returns at least 10 listings with `bathrooms≥2`
   (the count is the search-card / detail block intersection
   for the configured `Zaragoza` filters).
3. **Cross-portal dedup unchanged.** The two-portal dedup
   (`dedup_key` collisions) still works; the ranker's
   `dedup_top_n_by_key` still drops the lower-scored sibling.
4. **Parallel subagents.** `asyncio.gather` is exercised by
   the integration test: a single `run_scrapers` call yields
   handoffs from both subagents; the wall-clock time of the
   scraper phase is at most `max(t_foto, t_idealista) + 5s`
   of overhead. A unit test with delayed fake subagents
   asserts the two sessions overlap in time (e.g. by
   measuring that the second subagent's first event arrives
   before the first subagent's last event).
5. **Parallel detail fetches inside a subagent.** A unit test
   with a delayed fake `ScraperPort` shows that issuing N
   `fetch_listing` calls in one tool batch completes in
   roughly the time of the slowest call, not N× the slowest.
6. **Graceful degradation.** When playwright is not
   installed, `fetch_detail_html` returns `None`, the
   `fetch_listing` falls back to the search-card walk, and the
   `bathrooms` field is `None` exactly as in Sprint 3. A test
   with `IDEALISTA_DETAIL_FETCH=disabled` (or a fake
   `detail_client` that raises on launch) exercises the
   fallback path end-to-end.
7. **No regression on the existing acceptance criteria.** All
   Sprint 3 acceptance criteria still pass; the only diff is
   that `bathrooms` is now usually populated on Idealista rows
   and the scraper phase wall-time is shorter.
8. **Operator sees the parallel execution.** A run report
   shows both `=== scraper (fotocasa) ===` and
   `=== scraper (idealista) ===` with their own counters and
   timings; the parent trace in LangSmith (when configured)
   shows the two subagent graphs as siblings, not a chain.
9. **One new CLI flag.** `--no-detail-fetch` (env-equivalent
   `IDEALISTA_DETAIL_FETCH=disabled`) disables the
   playwright path for that run. Documented in `--help`.

## Definition of done

- All acceptance criteria pass on a clean local machine.
- Unit tests cover:
  - `parse_detail_page` on a captured detail-page HTML
    fixture (the new `tests/fixtures/idealista/detail_page1.html`,
    byte-for-byte from the Sprint 3 HAR capture) and on
    edge cases: missing block, malformed HTML, Spanish
    decimal-comma `70,5 m²`, the "1 baño" singular vs
    "2 baños" plural.
  - `IdealistaScraper.fetch_listing` happy path (detail
    fetch succeeds → `Apartment.bathrooms` is set) and
    fallback path (detail fetch fails / disabled → search
    card → `bathrooms` is `None`).
  - `run_scrapers` happy path (both subagents succeed,
    handoff is combined) and partial failure (one raises,
    the other completes, the error is captured in the
    returned dict).
  - The parallel subagent integration: with delayed fake
    subagents, total wall-time is < `t_foto + t_idealista`
    (i.e. not the sum).
  - The parallel detail fetch integration: with a delayed
    fake `ScraperPort.fetch_listing`, N parallel calls
    complete in ≈ time of the slowest.
- One new integration test exercises the full
  orchestrator → `run_scrapers` (with both
  `fotocasa_scraper` + `idealista_scraper`) → `ranker` →
  `notifier` flow with fake adapters for every external
  I/O (Fotocasa, Idealista, Gmail SMTP), asserts the run
  report carries the per-portal `details_enriched` counter
  alongside the existing `inserted` / `duplicate` /
  `filtered` / `soft_extracted`, and asserts the
  `dedup_top_n_by_key` step still drops the cross-portal
  sibling.
- New ADR: **ADR-013 — Parallel scraper execution** (the
  `run_scrapers` tool, the `asyncio.gather` of the two
  subagent graphs, and the per-card parallel tool calls).
  ADR-011 is updated in-place to mark its "Future work —
  detail-page upgrade" bullet as delivered.
- `README.md` updated with: the new `IDEALISTA_DETAIL_FETCH`
  env var, the playwright browser install step (already
  present from Sprint 1), a "What's new in Sprint 4" section,
  and the expected wall-time improvement (~40–50% on the
  scraper phase).
- `.env.example` documents `IDEALISTA_DETAIL_FETCH` (default
  `enabled`).
- The Sprint 3 acceptance criteria that were not changed by
  Sprint 4 still pass: idempotency, dedup_key semantics,
  `validate-quality` field coverage, `show-run` /
  `backfill-dedup-keys`, the OCP smoke test (the
  `IdealistaScraper` is still accepted by anything
  type-checked against `ScraperPort` — the detail-page
  enrichment is private to the implementation).
