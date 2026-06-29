# ADR-010 — Listing data quality: invalid-coordinate normalisation + duplicate backfill (`Updated` result)

## Context

Two recurring data-quality bugs in the Sprint 2 daily run:

1. **Scrapers left `(0, 0)` placeholders.** A listing that
   hadn't been geo-located yet came back from the portal as
   `(lat=0, lng=0)`. The `DistanceToDangerousCriterion`
   computed a great-circle distance of ~6,500 km to every
   Zaragoza dangerous neighborhood — i.e. it rewarded the
   listing with the maximum score (1.0). The notifier was
   happy to email a fake "very far from danger" listing to the
   operator.
2. **Re-scrapes lost newly extracted soft fields.** A Sprint 1
   row with `pet_policy=NULL` was re-scraped in Sprint 2 and the
   scraper subagent now extracted `"allowed"`. The repository's
   `ON CONFLICT (source, external_id) DO NOTHING` returned
   `Duplicate` and the new value was thrown away. The row stayed
   `NULL` forever.

Both bugs were invisible from the CLI (the JSON summary just
showed "inserted" / "duplicate" counts).

## Decision

Two changes, both in Pillar D:

1. **`domain/geo.is_valid_coordinate(lat, lng) -> bool`.** Pure
   function, no I/O. Rejects `None`, `(0, 0)`, NaN/Inf, and any
   point outside a coarse Zaragoza bounding box
   `lat ∈ [41.5, 41.8], lng ∈ [-1.05, -0.8]`. Accepts `Decimal`
   (the type the `Apartment` value object uses).
2. **`PostgresApartmentRepository.upsert` backfill path.** Replaced
   `ON CONFLICT (source, external_id) DO NOTHING` with
   ```
   ON CONFLICT (source, external_id) DO UPDATE SET
       pet_policy = COALESCE(EXCLUDED.pet_policy, apartments.pet_policy),
       furnished = COALESCE(EXCLUDED.furnished, apartments.furnished),
       lat = COALESCE(EXCLUDED.lat, apartments.lat),
       lng = COALESCE(EXCLUDED.lng, apartments.lng),
       description = COALESCE(EXCLUDED.description, apartments.description),
       raw_json = EXCLUDED.raw_json,
       scraped_at = EXCLUDED.scraped_at
   WHERE
       apartments.pet_policy IS DISTINCT FROM EXCLUDED.pet_policy
       OR apartments.furnished IS DISTINCT FROM EXCLUDED.furnished
       OR apartments.lat IS DISTINCT FROM EXCLUDED.lat
       OR apartments.lng IS DISTINCT FROM EXCLUDED.lng
       OR apartments.description IS DISTINCT FROM EXCLUDED.description
   ```
   The COALESCE preserves `NULL` (so a re-scrape that didn't
   extract `pet_policy` doesn't clobber an existing value); the
   WHERE clause only rewrites when at least one of the 5 fields
   is DISTINCT. The adapter returns a third `Updated` outcome
   carrying `apartment_id` + `changed_fields`.
3. **`DistanceToDangerousCriterion` hardening.** When
   `is_valid_coordinate(lat, lng)` is `False`, the criterion
   returns a neutral `0.5` with
   `details={"reason": "invalid coordinates"}` instead of
   silently rewarding the listing.
4. **`ingest_apartment` tool** drops invalid coordinates to
   `None` before persisting, and stamps the cross-portal
   `dedup_key` on `apartment.raw["dedup_key"]`. The tool returns
   `{ "status": "updated", "id": <int>, "changed_fields": [...] }`
   on the new third outcome.

The `InMemoryApartmentRepository` mirrors the same three-way
contract (`Inserted` / `Updated` / `Duplicate`) so tests stay
portable.

## Consequences

- **The DB never holds a fake coordinate.** `(0, 0)` and
  out-of-bbox values land in the row as `NULL`; the ranker
  scores them 0.5 with a clear `reason`.
- **Re-scrapes backfill soft fields.** A Sprint 1 row with
  `pet_policy=NULL` gets the value on the next scrape; the
  `ingest_apartment` handoff reports `updated` with the changed
  columns.
- **No new dependencies.** Pure SQL change in the migration,
  pure-Python change in the domain.
- **The `Updated` outcome is a new public surface.** Tool
  callers and tests had to grow an `isinstance(result, Updated)`
  branch. The CLI's `ingest_apartment` handoff counts
  `inserted` / `updated` / `duplicate` separately (Pillar D
  acceptance criterion 6).
- **`field_coverage` exposed in `validate-quality`.** Per-source,
  per-field null rate + invalid-coordinate count. The operator
  can see at a glance whether a portal exposes a field before
  trusting it in ranking (resolves Q6).

## Alternatives considered

- **Hard-delete the listing on invalid coordinates.** Loses the
  row entirely; the operator can no longer see the description
  or the URL. `NULL` keeps the row observable.
- **Trigger-based validation.** Stronger at the DB level, but
  the scraper subagent already validates in Python; a
  trigger would be a second source of truth.
- **Store the original coordinates alongside the normalised
  ones.** Useful for the operator to debug, but doubles the
  column count and no Sprint 1 row needs it. Capture the raw
  `raw_json` blob (already there) and re-parse if needed.
