# ADR-011 — Second scraper: Idealista + stealth/CSR strategy + fallback to an easier portal

## Context

Sprint 1 shipped one portal (Fotocasa) and explicitly deferred
a second. Sprint 3 (Pillar E) committed to delivering a working
second adapter so the daily run pulls from broader inventory and
the cross-portal dedup use case (Pillar F) is exercised.

**Target portal selection.** Sprint 3's `docs/SPRINT3.md` calls
out the criteria:

- Highest Spanish rental coverage.
- Complements Fotocasa (i.e. different listings for the same
  Zaragoza apartments).
- Free / DIY (per the ROADMAP principle "scraping is DIY and
  free until proven insufficient").

**Idealista** is the highest-traffic Spanish rentals portal and
is the committed Sprint 3 target. The first investigation day
was reserved for: pick the portal after seeing what passes the
DataDome-style bot check.

**Strategy constraint.** The project explicitly excludes paid
proxies, headless-detection evasion beyond a real UA + UA pool,
and on-the-fly browser automation for the search path. The
existing `pyproject.toml` already lists `playwright` and
`httpx`; `curl_cffi` was added in Sprint 3 for TLS-fingerprint
impersonation.

## Decision

**`IdealistaScraper` (the primary deliverable)** under
`adapters/scrapers/idealista/`:

- `httpx` is 403'd by DataDome on the very first request. We
  switched to **`curl_cffi`** with a real Chrome TLS profile
  (`impersonate="chrome131"`). Tested profiles that pass:
  `chrome131`, `chrome124`. Profiles that 403: `chrome142` and
  newer.
- The search endpoint is **server-rendered HTML** (no JSON API
  exposed to non-browser clients). The parser is a pure
  function of `(html) -> list[ListingCard]`, no I/O.
- **Detail pages (`/inmueble/<id>/`) are blocked for
  non-browser clients.** DataDome trust-scores session cookies
  against real-browser signals (mouse movement, JS execution);
  a `curl_cffi` session can never accumulate enough trust.
  The scraper therefore *does not* hit detail pages; instead
  it walks the search pages until it finds the requested
  card. The returned `Apartment` carries the search-card
  field set: `title`, `price_eur`, `rooms`, `size_m2`,
  `address`, partial `description`. `bathrooms` may be `None`;
  `lat`/`lng` are always `None` until a future sprint adds a
  playwright upgrade for the detail path.
- A small pool of 3-5 real Chrome/Firefox `User-Agent` strings
  (rotated per request) — the existing `scraper_user_agent`
  setting is reused.
- A polite delay between paged fetches
  (`IDEALISTA_SCRAPER_DELAY_SECONDS=2.0`, slightly higher than
  Fotocasa's 1.5s).
- A `robots.txt` check at adapter construction; if Idealista's
  robots disallows the search path, the scraper logs a warning
  and yields nothing (it does not bypass robots).

**Fallback.** If Idealista's anti-scraping blocks the adapter
during the first investigation day, the fallback is the easier
portal Pisos.com (or any other that survives the bot check) —
same `ScraperPort`, swap one adapter. **No orchestrator change**
is needed for the swap (this is the OCP smoke-test the Sprint 3
acceptance criterion 9 asserts).

**`idealista_scraper` subagent** mirrors `fotocasa_scraper`
exactly: own tools, own `/idealista_scraper/` filesystem route,
own prompt. The orchestrator's prompt was updated to delegate
to **both** subagents in a single run, sequentially.

**The LLM extraction of `pet_policy` + `furnished` is reused
verbatim** — the `ingest_apartment` tool is source-agnostic
(Sprint 2's design), so the new subagent inherits the same
soft-field contract.

**Field coverage on the search card (Pillar D + Q6):**

- `lat` / `lng`: always `None` for Sprint 3 (DataDome blocks
  the detail page for non-browser clients). Distance criterion
  scores these rows a neutral 0.5.
- `bathrooms`: never on the search card. `min_bathrooms=2` in
  the orchestrator's hard filter therefore rejects every
  Idealista row in Sprint 3. This is acceptable — the
  cross-portal dedup catches the Fotocasa row for the same
  physical apartment.

## Consequences

- **Broader inventory.** A Sprint 3 run pulls from both Fotocasa
  and Idealista, doubling the search-result surface for the
  same city.
- **OCP held.** `IdealistaScraper(ScraperPort)` is a single-
  adapter addition. `FotocasaScraper`, the `ingest_apartment`
  tool, the ranker, and the notifier are all unchanged. The
  Sprint 3 acceptance criterion 9 (OCP smoke test) is asserted
  by `tests/integration/test_sprint3_pipeline.py` and
  `tests/unit/test_orchestrator_dual_scraper.py`.
- **Distance criterion treats Idealista rows as 0.5.** The
  operator sees this in the run report and in
  `validate-quality`'s per-source field coverage. Sprint 4
  closed the `bathrooms` half of the gap (via the
  detail-page upgrade) but `lat` / `lng` are still `None`
  (DataDome hides them behind a second click in the real
  UI; filling them is a follow-up ticket).
- **`bathrooms` is now usually populated on Idealista rows.**
  Sprint 4 (Pillar A) added a single shared playwright
  `BrowserContext` (`adapters/scrapers/idealista/detail_client.py`)
  that hits `/inmueble/<id>/` and parses the canonical
  `<div class="details-property_features">` block. The default
  `min_bathrooms=2` no longer rejects every Idealista row;
  the `validate-quality` field-coverage report shows
  `bathrooms` non-null rate ≥ 90% on `source='idealista'`
  rows. The detail path is gated on `IDEALISTA_DETAIL_FETCH`
  (or `--no-detail-fetch`); when disabled, the scraper falls
  back to the search-card walk and `bathrooms` stays `None`
  (the Sprint 3 behaviour).
- **`curl_cffi` is a new dependency.** Tested with 0.15.0+.
  Pinned in `pyproject.toml`. The TLS-fingerprinting surface
  is small (1-2 imports); we don't pull in a third-party
  bot-detection library.

## Alternatives considered

- **Playwright for the search path.** Stronger anti-bot
  surface, but every request is ~5-10x slower than a real
  HTTP client. Reserve for the detail-page upgrade (Sprint 4
  or 5) when the value of `lat` / `lng` justifies the cost.
- **Headless Chrome via the `chrome` Selenium driver.** Same
  cost as playwright, no upside for the SSR search page.
- **Photocasa (rentals photos), Yaencontre, Pisos.com.** All
  easier on the bot check but lower inventory. Reserved as
  fallbacks (Pisos.com is the documented fallback per
  `docs/SPRINT3.md`).

## Future work

- **Detail-page upgrade** *(delivered in Sprint 4)* — A
  playwright-based path that hits `/inmueble/<id>/` and
  backfills `bathrooms` (and the long-form `description`)
  lives at `adapters/scrapers/idealista/detail_client.py`
  (the shared `BrowserContext`) + `parse_detail_page` /
  `apply_detail_enrichment` in `adapters/scrapers/idealista/api.py`.
  `IdealistaScraper.fetch_listing` is now a three-step path
  (search-card walk → detail fetch via the shared context →
  enrichment), and reports per-run `details_enriched` /
  `details_failed` counters in the subagent's handoff. The
  detail path is gated on `IDEALISTA_DETAIL_FETCH` (or the
  `--no-detail-fetch` CLI flag) and gracefully falls back to
  the search-card walk when playwright is unavailable. The
  Sprint 3 `bathrooms=NULL → min_bathrooms=2 rejects every
  Idealista row` failure mode is closed. `lat` / `lng` are
  still `None` — they live behind a second click in the real
  UI and are deferred to a follow-up ticket (likely a
  second-click on the map widget).
- **Exa-backed discovery adapter.** Listed in `docs/ROADMAP.md`
  as Q1 / future sprint. Adding it is a single-adapter change
  behind `ScraperPort` (the same OCP path).
