# Sprint 3 — Observability + data quality + second scraping source

**Goal:** turn the Sprint 2 daily run from an opaque "HTTP POST stream
that ends in a JSON blob" into an **operator-observable, explainable,
trustworthy** pipeline, and broaden listing coverage by adding a
second portal behind the same `ScraperPort`.

Sprint 3 pays down the four near-term product-debt items the roadmap
records after Sprint 2 (`docs/ROADMAP.md` §"Near-term product
debt"): end-to-end run UX, LangSmith full-pipeline tracing, ranked
result explainability, and listing data quality before ranking. It
then adds a second scraper adapter (OCP: additive only — Fotocasa
and the orchestrator are untouched) and prepares the cross-portal
dedup use case that may motivate embeddings in Sprint 4.

This sprint intentionally excludes embeddings activation, route-based
distance (OSRM/Mapbox), availability re-check of ranked apartments,
a third scraping source, a paid notification provider, and any VPS /
production migration. See `ROADMAP.md` for principles, stack,
execution model, and the abstract view of later sprints — this
document does not repeat that material.

## Resolved decisions (from `ROADMAP.md`)

| # | Question | Resolution |
| --- | --- | --- |
| Q1 | Use Exa as a discovery layer for listings (free tier)? | **Not in S3.** Exa stays as the `researcher` subagent's web-search backend (S2). The scrapers are the listing discovery layer. An Exa-backed discovery adapter can be added in a future sprint if the scrapers miss inventory; the `ScraperPort` makes that a single-adapter addition. |
| Q6 | Coverage of the pet-policy field on Fotocasa / Idealista | Measured and reported. The scraper subagent already extracts `pet_policy` at ingest (S2). Sprint 3 adds a **field-coverage report** to `validate-quality` (per-source, per-field null rate) so the operator can see whether a portal exposes the field before relying on it in ranking. No schema change. |
| —  | Second scraping source | **Idealista** is the committed target — highest Spanish rental coverage, complements Fotocasa. Delivered as `IdealistaScraper(ScraperPort)` with a documented stealth + CSR strategy. If Idealista's anti-scraping blocks the adapter during the first investigation day, the fallback is an easier portal (Pisos.com) behind the same port — a single-adapter swap, no orchestrator change. The sprint commits to delivering *a* working second scraper, with the portal finalised by reconnaissance. |

## Scope

### Deliverables

The sprint is organised into six pillars. Each is independently
shippable; the order below is the suggested implementation sequence.

#### Pillar A — Operator-facing run observability (CLI phases + run report)
- A `RunReport` domain object (`domain/run_report.py`) that
  accumulates structured **phase events** during a single CLI `run`:
  - `phase(started_at, name, ...)` / `phase(finished_at, name,
    duration_ms, counts, errors)` for `researcher`, `scraper`,
    `ranker`, `notifier`, `deterministic_tail`.
  - `count(name, n)` for incremental counters (cards seen, pages
    fetched, rows inserted, duplicates, filtered out, scored, sent).
  - `warning(...)` / `error(...)` for non-fatal and fatal issues.
  - `decision(label, value)` for human-readable state transitions
    (e.g. `researcher skipped: dangerous_neighborhoods already
    populated (n=6)`, `notifier skipped: already sent today`).
- A `RunObserver` **port** (`ports/run_observer.py`) with two
  adapters:
  - `CliRunObserver` — prints phase headers and counters to **stderr
    in real time** as events arrive (so the operator sees progress
    while the run is happening, not only a final blob). Phase headers
    look like `=== researcher ===`, `=== scraper ===`, etc.; counters
    are one-line `scraper: fetched 12 pages, 47 cards, 41 inserted, 6
    duplicates`; waits are labelled in domain terms (`waiting on LLM`,
    `waiting on Fotocasa HTTP`, `waiting on Postgres`, `waiting on
    SMTP`).
  - `RecordingRunObserver` — collects every event into the
    `RunReport` object that is persisted to
    `/orchestrator/reports/<run-uuid>.json` at the end of the run.
- The CLI's `run` subcommand is rewritten to drive both observers in
  parallel: stderr shows live phases; the final stdout JSON is kept
  (enriched — see Pillar C) and the full `RunReport` is written to
  disk. The existing per-ranking report at
  `/ranker/reports/<uuid>.json` (S2) is **merged** into the run
  report; the old path is kept as a redirect for one sprint and
  removed in S4.
- The orchestrator's `_DeterministicSteps.run()` and the LLM
  invocation in `cli.py` emit events through the injected observer
  instead of (or in addition to) `logger.info`. The observer is the
  single sink the CLI listens to; logging stays for low-level debug.

#### Pillar B — LangSmith full-pipeline tracing
- Every significant operation in a run is wrapped in a LangSmith
  span, so a single parent trace reconstructs the whole pipeline —
  including deterministic code paths that Sprint 2 did not
  instrument.
- A thin `observability/tracing.py` module wraps the `langsmith`
  client (already in `pyproject.toml`; no new dependency) and exposes
  `@traceable(name=...)` decorators / context managers used at:
  - orchestrator planning (`orchestrator.plan`)
  - `researcher` subagent (LLM call auto-traced; web search +
    upsert as child spans)
  - `scraper.search_listings` (one span per page, with
    `page_number`, `page_size`, `yielded`, `inspected` metadata)
  - `scraper.fetch_listing` (one span per listing, with `url`,
    `external_id`, `cache_hit`)
  - `ingest_apartment` (one span, with `source`, `external_id`,
    `result` = `inserted`/`duplicate`/`updated`)
  - Postgres reads / writes (`repo.list_all`,
    `ranking_repo.write_scores`, `ranking_repo.record_send`) — one
    span each, with row counts and `dedup_skip` when applicable
  - `compute_ranking` (one parent span with `apartments_scored`,
    `scores_written`, `top_n`; one child span per criterion with the
    per-criterion score distribution)
  - `notifier.render_email`, `notifier.send_email` (SMTP),
    `notifier.record_send` (DB), and the dedup-skip path
  - filesystem report writes (`backend.awrite` for the run report +
    outbox)
- Spans carry **domain-meaningful metadata** (counts, urls,
  apartment ids, skip reasons, weights) so the operator can read the
  trace without cross-referencing code. Errors and retries are
  recorded as span events.
- Tracing is **gated** on `settings.langsmith_tracing` (already
  present in `config.py`); when off, the decorators are no-ops with
  negligible overhead. The CLI prints the LangSmith trace URL at the
  end of the run when tracing is on.
- A `--trace` CLI flag on `run` force-enables tracing for a single
  invocation regardless of the env default (useful for ad-hoc
  debugging).

#### Pillar C — Ranked result explainability
- The top-N ranked apartments are shown **with their fields** in
  every operator-facing surface, not just the email body (which S2
  already enriches):
  - **CLI stdout** — `_format_deterministic` in `cli.py` is changed
    so each top-N row includes `title`, `price_eur`, `rooms`,
    `bathrooms`, `size_m2`, `address`, `url`, `final_score`, and the
    per-criterion `breakdown` (criterion, score, weight, details).
    The apartment fields are joined from the `apartments_by_id` map
    the orchestrator already builds; no extra DB round-trip.
  - **Run report** — the persisted
    `/orchestrator/reports/<run-uuid>.json` carries the same
    enriched top-N (one entry per ranked apartment with all fields +
    breakdown) so the operator can inspect the result without SQL.
  - **Email** — the S2 `domain/notifier.py` renderer is already
    explainable; Sprint 3 only normalises the field set across the
    three surfaces (CLI, report, email) so they show the same
    columns in the same order. No email rendering rewrite.
- A new CLI subcommand `show-run <run-uuid>` reads a persisted run
  report and re-prints the enriched top-N + phase breakdown, so the
  operator can inspect a past run without `jq` on the JSON file.

#### Pillar D — Listing data quality before ranking
- **Invalid-coordinate normalization.** A new pure function
  `domain/geo.is_valid_coordinate(lat, lng)` returns `False` for
  `None`, `(0, 0)`, and any point outside a coarse Zaragoza bounding
  box (lat ∈ [41.5, 41.8], lng ∈ [-1.05, -0.8]). The
  `FotocasaScraper` and `IdealistaScraper` parsers call it before
  setting `lat`/`lng`; invalid values are stored as `NULL` (not
  `0`), so the DB never holds a fake coordinate.
- **Distance criterion hardening.**
  `DistanceToDangerousCriterion.score` (S2) already returns a
  neutral `0.5` for `None` lat/lng. Sprint 3 extends the guard: when
  `is_valid_coordinate` is `False` the criterion returns `0.5` with
  `details={"reason": "invalid coordinates"}` and the ranker
  **never awards a high distance score** to a listing whose
  coordinates are missing or bogus. Unit tests cover the `(0, 0)`
  and out-of-bbox cases.
- **Duplicate backfill.** The repository's `upsert` changes from
  `ON CONFLICT (source, external_id) DO NOTHING` (S1/S2) to
  `ON CONFLICT (source, external_id) DO UPDATE SET
   pet_policy = COALESCE(EXCLUDED.pet_policy, apartments.pet_policy),
   furnished = COALESCE(EXCLUDED.furnished, apartments.furnished),
   lat = COALESCE(EXCLUDED.lat, apartments.lat),
   lng = COALESCE(EXCLUDED.lng, apartments.lng),
   description = COALESCE(EXCLUDED.description, apartments.description),
   raw_json = EXCLUDED.raw_json,
   scraped_at = EXCLUDED.scraped_at
   WHERE apartments.pet_policy IS DISTINCT FROM EXCLUDED.pet_policy
      OR apartments.furnished IS DISTINCT FROM EXCLUDED.furnished
      OR apartments.lat IS DISTINCT FROM EXCLUDED.lat
      OR apartments.lng IS DISTINCT FROM EXCLUDED.lng
      OR apartments.description IS DISTINCT FROM EXCLUDED.description`
  so a re-scrape of the same listing **backfills** newly extracted
  soft fields and corrected coordinates instead of leaving stale
  NULLs. The `ApartmentRepository.upsert` return type gains an
  `Updated` variant alongside `Inserted` and `Duplicate`:
  - `Inserted` — new row.
  - `Updated` — existing row, at least one field changed.
  - `Duplicate` — existing row, nothing changed (the `WHERE`
    clause matched zero rows).
  `InMemoryApartmentRepository` is updated to the same contract so
  tests stay portable. The `ingest_apartment` tool reports all three
  outcomes in its handoff summary.
- **`validate-quality` coverage report.** The existing
  `validate-quality` subcommand gains per-source, per-field null
  rates: for each `source`, the fraction of rows where `lat`,
  `lng`, `pet_policy`, `furnished`, `description` are non-null, plus
  a count of rows with invalid `(0, 0)` / out-of-bbox coordinates
  (should be 0 after the normalization above). The operator sees at
  a glance whether a portal exposes a field before trusting it in
  ranking (resolves Q6).

#### Pillar E — Second scraping source (Idealista)
- A new `IdealistaScraper` adapter under
  `adapters/scrapers/idealista/`, implementing the same `ScraperPort`
  as Fotocasa (`search_listings` -> `AsyncIterator[ListingCard]`,
  `fetch_listing` -> `Apartment`, `close`). No change to Fotocasa,
  the orchestrator, or the `ingest_apartment` tool (OCP).
- A new `idealista_scraper` subagent (own tools, own filesystem
  subtree `/idealista_scraper/`, own prompt) registered with the
  orchestrator. The orchestrator's prompt is updated so it delegates
  to **both** `fotocasa_scraper` and `idealista_scraper` in a single
  run (sequentially or as two `task` calls), then writes a combined
  report. The orchestrator does not pick one portal — it calls both
  every run; cross-portal dedup is handled downstream (Pillar F).
- Stealth + CSR strategy (documented in the new ADR-011):
  - `httpx` with a realistic, rotating browser `User-Agent` per
    request (a small pool of 3–5 real Chrome/Firefox UA strings).
  - Polite delay between requests, reusing
    `adapters/scrapers/base.polite_sleep` with a slightly higher
    default (2.0s) than Fotocasa (1.5s).
  - `playwright` (already in `pyproject.toml`) as the **primary**
    render path for Idealista's CSR search + detail pages, with a
    plain `httpx` fast path for pages that serve SSR HTML.
  - A `robots.txt` check at adapter construction; if Idealista's
    robots disallows the search path, the scraper logs a warning and
    yields nothing (it does not bypass robots).
  - No paid proxy, no headless-detection evasion beyond a real UA +
    playwright. If this proves insufficient, the fallback is the
    easier portal (Pisos.com) — same `ScraperPort`, swap one
    adapter.
- CSS/JSON selectors isolated in
  `adapters/scrapers/idealista/selectors.py` (mirrors the Fotocasa
  layout). A `listing_parser.py` turns a rendered detail page into
  an `Apartment`; `item_to_card` / `item_to_apartment` live in an
  `api.py` if Idealista exposes a JSON endpoint, else in the parser.
- The LLM extraction of `pet_policy` + `furnished` (S2) is reused
  verbatim — the `ingest_apartment` tool is source-agnostic.

#### Pillar F — Cross-portal dedup preparation
- A new nullable `apartments.dedup_key text` column (migration
  `003_sprint3.sql`) populated by the scraper at ingest time with a
  best-effort **deterministic key**: `sha1("|".join([normalized_address,
  rooms, size_bucket, price_bucket]))` where `normalized_address` is
  lowercased + whitespace-collapsed + trailing-zipcode-stripped,
  `size_bucket = round(size_m2 / 5) * 5`, and
  `price_bucket = round(price_eur / 25) * 25`. The key is the same
  across portals for the same physical apartment listed with
  minor field drift.
- A partial unique index `ON apartments (dedup_key) WHERE dedup_key
  IS NOT NULL` provides **soft** cross-portal dedup at the DB
  level: the second portal's `upsert` hits the conflict and the
  repository returns `Duplicate` (cross-portal). Sprint 3 does
  **not** decide which row to keep — both stay in the table, the
  ranker scores both, and the notifier's top-N may include at most
  one per `dedup_key` (a new `dedup_top_n_by_key` step in
  `compute_ranking` that drops the lower-scored sibling). Full
  embeddings-based similarity dedup is Sprint 4.
- `validate-quality` reports the count of cross-portal duplicates
  (rows sharing a `dedup_key`) so the operator can see the
  overlap before Sprint 4 turns it on.

### Out of scope (explicitly deferred)
- **Embeddings activation.** The `embedding vector(1536)` column
  stays nullable. Sprint 4 decides the use case (Q2) and populates
  it.
- **Route-based distance (OSRM/Mapbox).** Haversine only. A future
  sprint can add `OsrmDistanceProvider` behind the same
  `DistanceProvider` port.
- **Availability re-check of ranked apartments.** The notifier
  still trusts the database. Captured as a known gap in ADR-006.
- **A third scraping source.** Two portals are enough to validate
  cross-portal dedup preparation.
- **Retry / backoff / alerting on scraper or SMTP failure.** The
  cron re-fires the next day (S2 contract).
- **Multi-recipient notifications, unsubscribes.** S2 contract.
- **VPS / production migration.** Sprint 5.

## Database schema additions (migration `003_sprint3.sql`)

```sql
-- 003_sprint3.sql
-- Sprint 3: cross-portal dedup preparation + run reports support.
-- All changes are additive: 001 and 002 are not modified.

-- Best-effort deterministic key for cross-portal dedup. Populated by
-- the scrapers at ingest time. NULL for Sprint 1/2 rows; backfilled
-- by a one-off `backfill-dedup-keys` CLI command (see below).
ALTER TABLE apartments
    ADD COLUMN IF NOT EXISTS dedup_key text;

-- Soft cross-portal dedup: at most one row per dedup_key. The
-- partial index lets Sprint 1/2 rows (NULL dedup_key) coexist.
CREATE UNIQUE INDEX IF NOT EXISTS apartments_dedup_key_idx
    ON apartments (dedup_key)
    WHERE dedup_key IS NOT NULL;

-- Run reports: one row per CLI `run` invocation, pointing at the
-- persisted JSON on disk and carrying the phase-level counts for
-- quick SQL inspection without reading the file.
CREATE TABLE IF NOT EXISTS run_reports (
    id              bigserial PRIMARY KEY,
    run_id          uuid        NOT NULL UNIQUE,
    started_at      timestamptz NOT NULL,
    finished_at     timestamptz,
    phases          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    counts          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    ranking_run_id  uuid,
    notification_sent boolean,
    report_path     text,
    trace_url       text
);
CREATE INDEX IF NOT EXISTS run_reports_started_at_idx
    ON run_reports (started_at DESC);
```

A new one-off CLI subcommand `backfill-dedup-keys` computes
`dedup_key` for every existing row that has `NULL` and attempts the
update; rows that collide on an already-taken key are logged and
left NULL (the operator inspects them). This is idempotent and safe
to re-run.

## Package layout (additions on top of Sprint 2)

```
src/deep_apartment_finder/
  domain/
    run_report.py               # RunReport, PhaseEvent, CountEvent, ...
    geo.py                      # + is_valid_coordinate(lat, lng)
  ports/
    run_observer.py             # RunObserver Protocol
  adapters/
    observability/
      cli_observer.py           # CliRunObserver (stderr live phases)
      recording_observer.py     # RecordingRunObserver -> RunReport
      tracing.py                # langsmith @traceable wrappers
    postgres/
      run_report_repository.py  # persists run_reports rows
      migrations/
        003_sprint3.sql
    scrapers/
      idealista/
        __init__.py
        client.py               # httpx + playwright render
        listing_parser.py
        selectors.py
        scraper.py              # IdealistaScraper(ScraperPort)
  tools/
    idealista/
      search_listings.py        # mirrors fotocasa/search_listings.py
      fetch_listing.py
      save_snapshot.py          # forces prefix /idealista_scraper/raw/
  subagents/
    idealista_scraper.py
    prompts/
      idealista_scraper.md
      orchestrator.md           # updated: delegate to BOTH scrapers
  filesystem/
    routes.py                   # adds /idealista_scraper/ route
    trees/idealista_scraper/    # raw/ extracted/ cache/ selectors/ logs/
```

The `ingest_apartment` tool, the soft-criteria registry, the
ranker, and the notifier are **unchanged** — they are source-agnostic
by construction (S1/S2). The only orchestrator change is the prompt
(delegates to two scrapers) and the observer wiring.

## Per-subagent filesystem additions

```
/idealista_scraper/
  raw/         rendered HTML / JSON snapshots for replay & debug
  extracted/   normalized JSON before ingest
  cache/       URLs already seen (pre-DB cross-run dedup)
  selectors/   editable selectors (no recompile)
  logs/        scraper run logs
```

The `/idealista_scraper/` route is **persistent**
(`StoreBackend`), like the S1/S2 routes. Writes outside any
registered route stay on ephemeral `StateBackend` per ADR-005.

## LLM usage in Sprint 3

- **`researcher` subagent:** unchanged (S2). One LLM call on the
  first run only; no-op afterwards.
- **`fotocasa_scraper` + `idealista_scraper` subagents:** the LLM
  extracts `pet_policy` and `furnished` from each listing's
  `description` at ingest time (S2 pattern, reused verbatim). Two
  subagents = two independent LLM sessions per run; the orchestrator
  calls them sequentially.
- **`ranker` + `notifier`:** still deterministic Python, no LLM at
  rank / notify time (S2 contract).
- **Orchestrator:** one LLM session for planning + delegating to the
  two scraper subagents. The observer emits `waiting on LLM` /
  `waiting on scraper HTTP` events so the operator can tell the
  phases apart.

Sprint 3 does not introduce any new LLM call site. The
observability work makes the existing calls visible; it does not
add reasoning.

## Observability — phase contract

The `RunObserver` port is the single sink both the CLI and the
orchestrator emit events into. The contract (in `ports/run_observer.py`):

```python
class RunObserver(Protocol):
    async def phase_start(self, name: str, **meta: Any) -> None: ...
    async def phase_end(self, name: str, *, duration_ms: int,
                        counts: dict[str, int] | None = None,
                        errors: int = 0) -> None: ...
    async def count(self, name: str, n: int = 1) -> None: ...
    async def waiting(self, label: str) -> None: ...
    async def decision(self, label: str, value: str) -> None: ...
    async def warning(self, msg: str) -> None: ...
    async def error(self, msg: str, *, exc: BaseException | None = None) -> None: ...
```

The phases the operator sees, in order, for a subsequent (non-first)
run:

```
=== researcher ===           (skipped: dangerous_neighborhoods already populated (n=6))
=== scraper (fotocasa) ===
  waiting on LLM
  waiting on Fotocasa HTTP
  scraper: fetched 12 pages, 47 cards, 41 inserted, 6 duplicates
=== scraper (idealista) ===
  waiting on Idealista HTTP (playwright)
  scraper: fetched 9 pages, 33 cards, 28 inserted, 5 duplicates
=== ranker ===
  waiting on Postgres
  ranker: scored 121 apartments, wrote 363 score rows, top 5
=== notifier ===
  notifier: rendered email (5 apartments)
  waiting on SMTP
  notifier: sent (ranking_run_id=...)
=== done ===
  run report: /orchestrator/reports/<run-uuid>.json
  trace: https://smith.langchain.com/runs/<run-id>
```

The first-run path adds a `=== researcher ===` phase that actually
runs (web search + upsert) and stops before `scraper`.

## Acceptance criteria

1. `docker compose up -d` + `uv run python -m deep_apartment_finder
   migrate` applies `001`, `002`, and `003`; the new `dedup_key`
   column and `run_reports` table exist with the constraints above.
2. **CLI phase output** — `python -m deep_apartment_finder run`
   prints phase headers and counters to stderr **as they happen**
   (not only a final blob). The operator can tell at a glance
   whether the run is researching, ingesting, blocked, ranking, or
   notifying. The final stdout JSON is preserved (enriched per
   criterion 7).
3. **Run report persisted** — every `run` writes exactly one
   `/orchestrator/reports/<run-uuid>.json` with the full phase
   breakdown, counts, errors, the enriched top-N (criterion 7), the
   `ranking_run_id`, the `notification_sent` flag, and the LangSmith
   `trace_url` when tracing is on. A `run_reports` row is inserted
   with the same data for SQL inspection. `show-run <run-uuid>`
   re-prints it.
4. **LangSmith full-pipeline trace** — with
   `LANGSMITH_TRACING=true`, a single run produces one parent trace
   with child spans for orchestrator planning, each scraper
   subagent, each `search_listings` page, each `fetch_listing`,
   each `ingest_apartment`, every Postgres read/write,
   `compute_ranking` (with per-criterion child spans),
   `render_email`, `send_email` (SMTP), `record_send`, and the
   dedup-skip path when it fires. The trace URL is printed at the
   end of the run. With tracing off, the run behaves identically
   minus the URL.
5. **Invalid coordinates** — a listing whose `lat`/`lng` are `(0,
   0)` or outside the Zaragoza bounding box is stored with `NULL`
   coordinates (not `0`); the `DistanceToDangerousCriterion` scores
   it a neutral `0.5` with `details={"reason": "invalid
   coordinates"}`; the ranker never awards a high distance score to
   it. Unit tests cover the `(0, 0)` and out-of-bbox cases.
6. **Duplicate backfill** — re-scraping a listing already in the DB
   with newly extracted `pet_policy` / `furnished` / corrected
   coordinates updates the existing row (repository returns
   `Updated`); re-scraping with no new information returns
   `Duplicate` (no write). The `ingest_apartment` tool's handoff
   reports `inserted` / `updated` / `duplicate` counts separately.
   A Sprint 1 row with `NULL` `furnished` gets backfilled on its
   next scrape.
7. **Explainable top-N** — the CLI stdout, the persisted run
   report, and the email body all show, for each top-N apartment:
   `title`, `price_eur`, `rooms`, `bathrooms`, `size_m2`,
   `address`, `url`, `final_score`, and the per-criterion
   `breakdown` (criterion, score, weight, details). The three
   surfaces show the same fields in the same order.
8. **`validate-quality` coverage** — the command reports, per
   `source`: the fraction of rows with non-null `lat`, `lng`,
   `pet_policy`, `furnished`, `description`; the count of rows with
   invalid `(0, 0)` / out-of-bbox coordinates (expected 0); and the
   count of cross-portal duplicates (rows sharing a `dedup_key`).
9. **Second scraper** — `IdealistaScraper` implements `ScraperPort`;
   `python -m deep_apartment_finder run` ingests from **both**
   Fotocasa and Idealista in a single run; both sources' rows are
   visible in `validate-quality` with their per-source coverage.
   Adding the second scraper did not require editing
   `FotocasaScraper`, the `ingest_apartment` tool, the ranker, or
   the notifier (OCP smoke test: a unit test asserts the
   `IdealistaScraper` is accepted by any code that type-checks
   against `ScraperPort`).
10. **Cross-portal dedup preparation** — the same physical
    apartment listed on both portals produces two rows with the
    same `dedup_key`; the ranker's top-N includes at most one per
    `dedup_key`; `validate-quality` reports the overlap count.
    Full embeddings-based dedup is **not** implemented (deferred to
    S4).
11. **`backfill-dedup-keys`** — the one-off CLI subcommand computes
    `dedup_key` for existing NULL rows; re-running it is a no-op;
    colliding keys are logged and left NULL.
12. **Idempotency preserved** — the S1 ingest dedup + the S2
    `notifications` one-per-day invariant + the S3
    `apartments_dedup_key_idx` all hold when `run` is invoked twice
    on the same day; the second run sends no second email and
    inserts no duplicate rows.

## Definition of done

- All acceptance criteria pass on a clean local machine.
- Unit tests cover:
  - `domain/geo.is_valid_coordinate` (the `(0, 0)`, out-of-bbox,
    and valid cases)
  - `DistanceToDangerousCriterion` with invalid coordinates
    (returns `0.5`, never rewards)
  - `PostgresApartmentRepository.upsert` backfill path
    (`Inserted` / `Updated` / `Duplicate`) with a fixture row
  - `InMemoryApartmentRepository` matching the new three-way
    contract
  - `RunReport` event accumulation + JSON serialisation
  - `CliRunObserver` phase formatting (snapshot of stderr lines
    for a canned event sequence)
  - the `dedup_key` computation (same physical apartment on two
    portals yields the same key)
  - `compute_ranking` top-N dedup by `dedup_key`
  - `IdealistaScraper` parsing with fixture HTML/JSON (mirrors the
    existing `test_fotocasa_parser.py`)
  - tracing wrappers are no-ops when `langsmith_tracing` is off
- One new integration test exercises the full
  orchestrator → `fotocasa_scraper` + `idealista_scraper` →
  `ranker` → `notifier` flow with fake adapters for every external
  I/O (Fotocasa, Idealista, Gmail SMTP), asserts the run report is
  persisted with the enriched top-N, and asserts the
  `dedup_top_n_by_key` step drops the cross-portal sibling.
- New ADRs committed under `docs/adr/`:
  - ADR-009 — Observability: `RunObserver` port + CLI phases +
    LangSmith full-pipeline tracing
  - ADR-010 — Listing data quality: invalid-coordinate
    normalization + duplicate backfill (`Updated` result)
  - ADR-011 — Second scraper: Idealista + stealth/CSR strategy +
    fallback to an easier portal
  - ADR-012 — Cross-portal dedup key (deterministic hash,
    preparatory for embeddings in S4)
- `README.md` updated with: the new `show-run` and
  `backfill-dedup-keys` subcommands, the `--trace` flag, a
  "What's new in Sprint 3" section, and the second portal's setup
  notes (any env vars Idealista requires, the playwright browser
  install step).
- `.env.example` documents any new env vars (e.g.
  `IDEALISTA_BASE_URL`, `IDEALISTA_SCRAPER_DELAY_SECONDS`); real
  values stay in `.env` (gitignored).
