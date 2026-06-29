# Sprint 2 — Ranking + soft filters + notification + researcher

**Goal:** turn the Sprint 1 ingestion pipeline into a daily, useful product.
Add a `researcher` subagent that bootstraps a constants table of Zaragoza's
"dangerous" neighborhoods, teach the scraper subagent to extract
`pet_policy` and `furnished` at ingest time, add a deterministic `ranker`
subagent that scores stored apartments on pluggable soft criteria
(distance-to-dangerous, pet policy, furnished), and wire a `notifier`
subagent that emails the current top 5 via Gmail SMTP. Cron the whole
thing for 09:00 Europe/Madrid.

This sprint intentionally excludes cross-portal dedup, embeddings
activation, a second scraper, a route-based distance provider, an
availability re-check on ranked apartments, and a paid notification
provider. See `ROADMAP.md` for principles, stack, execution model, and the
abstract view of later sprints — this document does not repeat that
material.

## Resolved decisions (from `ROADMAP.md`)

| # | Question | Resolution |
| --- | --- | --- |
| Q3 | Dangerous neighborhoods list | Bootstrapped once by a `researcher` subagent on its first run (web search of news / public data), persisted in Postgres, then reused. Operator can override the table manually. |
| Q4 | Distance to dangerous neighborhoods | **Haversine** (straight-line), free, deterministic, trivial to test. A future sprint can swap in OSRM behind the same port. |
| Q5 | Notification provider | **Gmail SMTP** with an App Password. Free, tied to operator's personal Gmail, no third-party SaaS. |

## Scope

### Deliverables
- New migration `002_sprint2.sql`:
  - new column `apartments.furnished text` (nullable; populated by the
    scraper subagent at ingest time)
  - new table `dangerous_neighborhoods(id, name, center_lat, center_lng,
    radius_m, source, notes, created_at)` — populated by the researcher
    subagent, editable by the operator
  - new table `apartment_scores(ranking_run_id, apartment_id, criterion,
    score numeric, weight numeric, details jsonb, computed_at)` — for
    traceability of each ranker's outputs
  - new table `notifications(id, sent_on date, apartment_ids bigint[],
    ranking_run_id, created_at)` — one row per send; **prevents
    re-sending the same top-5 on the same day**
  - migration is **additive**; `001_init_apartments.sql` is not edited
- New `DangerousNeighborhoodRepository` port + `PostgresDangerousNeighborhoodRepository`
  adapter; new `RankingRepository` port + `PostgresRankingRepository` adapter
  (writes `apartment_scores` and `notifications`).
- New `Notifier` port + `GmailSmtpNotifier` adapter (uses Python's
  stdlib `smtplib` + `email.message.EmailMessage`; no extra runtime
  dep beyond what's already in `pyproject.toml`).
- New `DistanceProvider` port + `HaversineDistanceProvider` adapter
  (pure function, no I/O, easy to unit-test).
- New domain types:
  - `domain/soft_criteria/` with `SoftCriterion` Protocol, a tiny
    `Score` value object, and three concrete pluggable implementations:
    - `DistanceToDangerousCriterion`
    - `PetPolicyCriterion`
    - `FurnishedCriterion`
  - `domain/soft_criteria/registry.py` so adding a 4th criterion is a
    single-line additive change (OCP).
  - `domain/geo.py` with a `haversine_meters` pure function and a
    `is_in_dangerous_neighborhood(lat, lng, neighborhoods)` pure function
    (point vs. center+radius, haversine).
- New subagents, each with its own tools, its own `CompositeBackend`
  route, and its own prompt:
  - `researcher` — web-search tool, writes to
    `/researcher/dangerous_neighborhoods/`, **on its first run only**
    it bootstraps `dangerous_neighborhoods`; on subsequent runs it is a
    no-op. If web search returns nothing usable, it logs and the table
    stays empty (ranker handles "no dangerous neighborhoods" gracefully).
  - `ranker` — pure scorer (no LLM); reads apartments + neighborhoods,
    applies the registered `SoftCriterion`s, writes
    `apartment_scores`, returns the top 5.
  - `notifier` — reads the latest ranking + the `notifications` table
    for today, renders an email body (HTML + plain text), sends via
    `GmailSmtpNotifier`, writes a `notifications` row.
- Scrapers subagent is extended: the LLM now extracts **both**
  `pet_policy` and `furnished` from the listing `description` at ingest
  time, and they are persisted as columns. No re-extraction at rank
  time — the ranker is deterministic.
- Orchestrator flow becomes (after the first-run bootstrap):
  1. `researcher` (idempotent no-op on subsequent runs)
  2. `fotocasa_scraper`
  3. `ranker`
  4. `notifier`
  On the **first** run the orchestrator stops after `researcher`
  populates the dangerous-neighborhoods table, logs a clear
  "researcher populated N dangerous neighborhoods; re-run
  `python -m deep_apartment_finder run` to proceed" line, and exits
  cleanly. This is a deliberate human-in-the-loop checkpoint so the
  operator can eyeball the list before it influences ranking.
- CLI gains a new subcommand `python -m deep_apartment_finder
  validate-quality` already exists from S1 and is kept; S2 adds no new
  CLI subcommands but the existing `run` command now does the full
  pipeline. A small `python -m deep_apartment_finder list-dangerous`
  helper is added for operator inspection/override.
- New cron entry documented in `README.md`:
  `0 9 * * * cd /path && /usr/bin/env -S .venv/bin/python -m
  deep_apartment_finder run >> logs/cron.log 2>&1` with `TZ=Europe/Madrid`.
  The CLI is **idempotent** (ingest dedup from S1 + `notifications`
  dedup per day from S2); running it twice on the same day is safe.
- New ADRs:
  - ADR-006 — Researcher subagent + dangerous-neighborhoods constants
    table
  - ADR-007 — Haversine distance provider
  - ADR-008 — Gmail SMTP notifier

### Out of scope (explicitly deferred)
- **Availability re-check of ranked apartments** (i.e. hitting each
  top-5's original URL to confirm it's still listed before notifying).
  Captured as a known gap in `docs/adr/006-*.md` with a pointer to a
  future sprint. For S2 the notifier trusts the database.
- Cross-portal dedup, embeddings activation, second scraper (S3 / S4).
- Route-based distance (OSRM/Mapbox) — Haversine only in S2; a future
  sprint can add `OsrmDistanceProvider` behind the same
  `DistanceProvider` port.
- Pet-policy / furnished extraction by the `ranker` subagent — those
  happen at ingest time in the scraper subagent.
- Resend / Twilio / any paid notification provider.
- Multi-recipient notifications, unsubscribes, retry/backoff alerting.

## Soft criteria (pluggable, ranked deterministically)

The ranker is a **deterministic** Python function — no LLM at rank time.
The LLM's only job for soft criteria is to extract `pet_policy` and
`furnished` from the listing description during ingest, and to
bootstrap the dangerous-neighborhoods table during the researcher's
first run.

| Criterion | Input | Output | Weight (configurable) |
| --- | --- | --- | --- |
| `DistanceToDangerousCriterion` | `(lat, lng)` vs each row in `dangerous_neighborhoods` | `0.0` if inside any radius, decays linearly to `1.0` at the maximum configured distance (default 2 km) | `0.5` |
| `PetPolicyCriterion` | `apartments.pet_policy` enum: `allowed` / `negotiated` / `not_allowed` / `unknown` | `1.0` / `0.7` / `0.0` / `0.3` | `0.3` |
| `FurnishedCriterion` | `apartments.furnished` enum: `true` / `false` / `unknown` | `1.0` / `0.0` / `0.3` | `0.2` |

Final score per apartment = `sum(weight_i * score_i) / sum(weight_i)`.
Weights live in `config.py` as a single pydantic model so an operator
can rebalance without touching code. Adding a 4th criterion is one
class + one line in `registry.py`.

If `dangerous_neighborhoods` is empty (researcher failed to bootstrap),
`DistanceToDangerousCriterion` returns a neutral `0.5` for every
apartment and logs a warning. The ranker must never crash on an empty
neighborhoods table.

## Database schema additions (migration `002_sprint2.sql`)

```sql
-- New column on apartments (additive, nullable)
ALTER TABLE apartments
  ADD COLUMN IF NOT EXISTS furnished text
    CHECK (furnished IS NULL OR furnished IN ('true', 'false', 'unknown'));

-- Constants table populated by the researcher subagent
CREATE TABLE IF NOT EXISTS dangerous_neighborhoods (
  id          bigserial PRIMARY KEY,
  name        text NOT NULL UNIQUE,
  center_lat  numeric(9,6) NOT NULL,
  center_lng  numeric(9,6) NOT NULL,
  radius_m    integer NOT NULL CHECK (radius_m > 0),
  source      text NOT NULL,           -- e.g. 'researcher:web:elpais-2024-...'
  notes       text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- Per-apartment, per-criterion trace of every ranking run
CREATE TABLE IF NOT EXISTS apartment_scores (
  id              bigserial PRIMARY KEY,
  ranking_run_id  uuid NOT NULL,
  apartment_id    bigint NOT NULL REFERENCES apartments(id) ON DELETE CASCADE,
  criterion       text NOT NULL,       -- e.g. 'distance_to_dangerous'
  score           numeric(5,3) NOT NULL CHECK (score >= 0 AND score <= 1),
  weight          numeric(4,3) NOT NULL CHECK (weight >= 0 AND weight <= 1),
  details         jsonb,               -- e.g. {"nearest_m": 412, "name": "Delicias"}
  computed_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apartment_scores_run_idx
  ON apartment_scores (ranking_run_id);

-- One row per notification send; prevents re-sending the same top-5 on
-- the same day when the CLI is run twice (manual + cron overlap)
CREATE TABLE IF NOT EXISTS notifications (
  id              bigserial PRIMARY KEY,
  sent_on         date NOT NULL,
  apartment_ids   bigint[] NOT NULL,
  ranking_run_id  uuid NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (sent_on, ranking_run_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS notifications_one_per_day_idx
  ON notifications (sent_on);
```

A partial unique index enforces "at most one notification per day" at
the DB level; the notifier handles the unique-violation as a no-op
(re-run safety) and logs it.

## Package layout (additions on top of Sprint 1)

```
src/deep_apartment_finder/
  domain/
    geo.py                       # haversine_meters, in_dangerous_neighborhood
    soft_criteria/
      __init__.py
      base.py                    # SoftCriterion Protocol, Score dataclass
      distance_to_dangerous.py
      pet_policy.py
      furnished.py
      registry.py                # default_criteria() -> list[SoftCriterion]
  ports/
    dangerous_neighborhood_repository.py
    ranking_repository.py
    distance_provider.py         # HaversineDistanceProvider implements it
    notifier.py                  # Notifier Protocol
  adapters/
    postgres/
      dangerous_neighborhood_repository.py
      ranking_repository.py
      migrations/
        002_sprint2.sql
    notifiers/
      gmail_smtp.py              # GmailSmtpNotifier(Notifier)
    distance/
      haversine.py
  tools/
    researcher/
      web_search.py              # thin wrapper around the web search tool
      upsert_neighborhoods.py    # forces prefix /researcher/dangerous_neighborhoods/
    ranker/
      compute_scores.py
      list_top.py
    notifier/
      render_email.py
      send_email.py
  subagents/
    researcher.py
    ranker.py
    notifier.py
    prompts/
      researcher.md
      ranker.md
      notifier.md
  filesystem/
    routes.py                    # adds /researcher/, /ranker/, /notifier/ routes
    trees/                       # new README.md files for each new subtree
```

## Per-subagent filesystem additions

```
/researcher/
  dangerous_neighborhoods/   raw research notes + proposed rows
                             (final accepted rows are in Postgres, not here)
/ranker/
  plans/                     exported scoring plans / weight snapshots
  reports/                   per-run top-5 + per-apartment score breakdowns
/notifier/
  outbox/                    rendered email bodies (HTML + txt) per send
  logs/                      SMTP send logs
```

All three new routes are **persistent** (`StoreBackend`). Writes
outside any registered route stay on ephemeral `StateBackend` per the
ADR-005 pattern.

## LLM usage in Sprint 2

- **`researcher` subagent:** one LLM call to interpret web results and
  propose 3–8 Zaragoza neighborhoods with center coordinates and an
  estimated radius. Output is validated as JSON against a pydantic
  schema and upserted into `dangerous_neighborhoods`. If web search
  fails or returns nothing, the table is left empty and the agent logs
  the failure.
- **`fotocasa_scraper` subagent (extended):** for each listing, the LLM
  is asked to extract `pet_policy` and `furnished` from
  `description` as a small JSON object. Same pattern as the existing
  parse step; new fields are added to the persisted row.
- **`ranker` subagent:** **no LLM**. Pure deterministic Python. The
  subagent's "intelligence" is its system prompt documenting the
  scoring formula and the registry; the work happens in
  `tools/ranker/compute_scores.py`.
- **`notifier` subagent:** **no LLM** in the hot path. Renders the
  email body from a jinja-style string template in
  `tools/notifier/render_email.py`. Optional future enhancement:
  summarize the top 5 with an LLM call (deferred).

## Notification provider — Gmail SMTP

- Provider: **Gmail SMTP** (`smtp.gmail.com:465`, SSL) with an
  **App Password** (operator generates one in Google account security
  settings; 2FA must be enabled on the account).
- New env vars in `.env.example`:
  - `GMAIL_SMTP_ADDRESS` (default: `victor.oxi@gmail.com` — operator
    confirms at setup time)
  - `GMAIL_SMTP_APP_PASSWORD` (operator-supplied; **never committed**)
  - `NOTIFY_TO_ADDRESS` (default: same as `GMAIL_SMTP_ADDRESS`; can be
    a comma-separated list later, not in S2)
- Implementation: Python stdlib `smtplib.SMTP_SSL` +
  `email.message.EmailMessage`. No new runtime dependency.
- Email body: plain text + simple HTML; one section per ranked
  apartment (title, price, rooms/bathrooms/size, link to the original
  URL, per-criterion score breakdown).
- Failure mode: SMTP errors are caught, logged, and the
  `notifications` row is **not** written. The next run retries.
  No retry/backoff loop in S2 — the cron re-fires the next day.
- `GmailSmtpNotifier` lives behind the `Notifier` port so a future
  sprint can add `ResendNotifier` / `TwilioNotifier` without touching
  the orchestrator.

## Acceptance criteria

1. `docker compose up -d` brings up Postgres; running migrations applies
   `001_init_apartments.sql` and `002_sprint2.sql`; the new tables and
   the `furnished` column exist with the constraints above.
2. **First run** — `python -m deep_apartment_finder run` from a clean
   state: the `researcher` subagent performs a web search, persists
   between 3 and 8 rows into `dangerous_neighborhoods` (or logs a clear
   failure and leaves the table empty), and the orchestrator exits with
   a "re-run to proceed" message. `list-dangerous` prints the
   bootstrapped list.
3. **Subsequent run** — re-running `python -m deep_apartment_finder
   run`: `researcher` is a no-op; `fotocasa_scraper` ingests new
   listings (and persists `pet_policy` + `furnished` on each new row);
   `ranker` scores every stored apartment that satisfies the Sprint 1
   hard filters, writes one `apartment_scores` row per criterion per
   apartment, and returns a top-5 ranked list; `notifier` sends one
   email via Gmail SMTP with the top-5 and writes exactly one
   `notifications` row for today.
4. **Re-run safety** — running `python -m deep_apartment_finder run`
   twice in the same day does **not** send a second email; the
   notifier's `INSERT` hits the `notifications_one_per_day_idx` unique
   violation, is caught, and the run logs "already notified today".
5. **Empty / <5 ranked apartments** — the ranker handles <5
   apartments gracefully; the notifier sends what it has (or skips and
   logs "0/5 ranked apartments, nothing to send"); the run does not
   crash.
6. **Pet policy + furnished extraction** — for at least 80% of newly
   ingested rows in a sample run, `pet_policy` and `furnished` are
   non-null and equal to one of the allowed enum values. Existing
   rows ingested in Sprint 1 keep `NULL` for `furnished` (acceptable
   per `validate-quality`).
7. **Distance criterion is correct** — unit tests: an apartment at the
   exact center of a dangerous neighborhood scores `0.0`; an apartment
   at `> 2 km` from every center scores `1.0`; an empty
   `dangerous_neighborhoods` table yields `0.5` for every apartment
   and a warning log.
8. **Soft criteria registry** — adding a 4th criterion is a single
   class + a single line in `registry.py`; no other file needs to
   change. (Demonstrated by a unit test that monkey-patches the
   registry.)
9. **CLI idempotency** — the existing S1 dedup test still passes; the
   S2 `notifications` dedup test passes.
10. **Cron** — the documented crontab line is in `README.md` with
    `TZ=Europe/Madrid`; a manual `run` from the shell behaves
    identically to the cron-launched run (no env-dependent branching).

## Definition of done
- All acceptance criteria pass on a clean local machine.
- Unit tests cover:
  - `domain/geo.py` (haversine, in_dangerous_neighborhood)
  - each `SoftCriterion` with hand-crafted inputs
  - the soft-criteria registry (OCP smoke test)
  - `GmailSmtpNotifier` with an in-process fake SMTP server
    (`aiosmtpd` in test deps, or a tiny `smtplib`-mocking fixture)
  - `PostgresRankingRepository` dedup-per-day path
- One new integration test exercises the orchestrator →
  `researcher` → `ranker` → `notifier` flow with fake adapters for
  every external I/O (web search, Gmail SMTP, Fotocasa).
- ADRs 006, 007, 008 committed under `docs/adr/`.
- `README.md` updated with the cron line, the Gmail App Password
  setup steps, and a one-paragraph "what changed in S2" section.
- `.env.example` documents the new SMTP env vars; real values stay in
  `.env` (gitignored).
