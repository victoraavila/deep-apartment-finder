# ADR-012 — Cross-portal dedup key (deterministic hash, preparatory for embeddings in S4)

## Context

Sprint 3 (Pillar F) introduces a second portal (Idealista, per
ADR-011). The same physical Zaragoza apartment is often listed
on both Fotocasa and Idealista with small field drift:

- The street address differs by zipcode presence
  (`"Calle X, 50001 Zaragoza"` vs `"Calle X, Zaragoza"`).
- The size is rounded to 1-3 m².
- The price drifts by 1-3%.
- The number of rooms is identical.
- The portal-specific `external_id` is unrelated
  (Fotocasa's propertyId, Idealista's `/inmueble/<id>/`).

The current `(source, external_id)` uniqueness constraint (Sprint
1) therefore keeps both rows in the table; the ranker scores
both; the notifier emails both. The operator gets two near-
duplicate recommendations in the daily email.

**Sprint 3 doesn't** turn on embeddings (deferred to Sprint 4,
Q2 in the roadmap). What it *does* need is a deterministic
best-effort key that collapses the obvious cross-portal
duplicates so the ranker's top-N doesn't include both siblings
in the daily email.

## Decision

A new nullable `apartments.dedup_key text` column
(`003_sprint3.sql`), populated by the scraper at ingest time
with:

```
sha1("|".join([
    normalized_address,
    rooms,
    size_bucket,
    price_bucket,
]))
```

where:

- `normalized_address` is `address` lowercased + whitespace-
  collapsed + every 5-digit token (Spanish postal code)
  dropped.
- `size_bucket = round(size_m2 / 5) * 5` — ±2.5 m² tolerance.
- `price_bucket = round(price_eur / 25) * 25` — ±12.5 € tolerance.

A partial unique index on `(dedup_key) WHERE dedup_key IS NOT
NULL` provides **soft** cross-portal dedup at the DB level.

**`compute_dedup_key(...) -> str | None`** in
`domain/geo.py` is the pure function the `ingest_apartment`
tool calls. It returns `None` for incomplete inputs (no
address, no rooms, no size, no price) — we'd rather have `NULL`
than a degenerate key.

The ranker's `compute_ranking(...)` applies
**`dedup_top_n_by_key`** (a new step): after sorting the top-N
by score DESC, it walks the list once and drops any apartment
that shares a `dedup_key` with a higher-scoring sibling. The
first occurrence of a key in the sorted list is the highest-
scoring one, so it stays; later occurrences are dropped. The
result is the ranker returns the same `top` shape the operator
already saw, plus a new `dedup_dropped: int` field counting
the dropped siblings. The run report persists both the
`dedup_dropped` count and the (already-deduped) top-N.

**Cross-portal visibility.** The `ApartmentRepository` port
gains two new methods:

- `cross_portal_dup_count() -> int` — the count of distinct
  `dedup_key` values that map to 2+ rows. The `validate-quality`
  CLI prints this so the operator sees the overlap before
  Sprint 4 turns on real embeddings.
- `list_by_dedup_key(dedup_key) -> list[(int, Apartment)]` —
  the ranker uses this to drop the lower-scoring sibling.

**Backfill.** A new `backfill-dedup-keys` CLI subcommand
computes the key for every existing `NULL` row. Re-running is
a no-op: rows that already have a non-NULL `dedup_key` are
left alone. Collisions (the new key is already taken by another
row) are logged and left `NULL` for the operator to inspect.

## Consequences

- **Same physical apartment, two portals → one top-N row.**
  The notifier stops emailing the duplicate recommendation.
- **The DB still holds both rows.** Sprint 3 doesn't *delete*
  the second one — the ranker, the run report, and the
  `validate-quality` operator tool all see both, and the
  `cross_portal_dup_count` exposes the overlap so the operator
  knows the system is doing the right thing.
- **Sprint 1/2 rows are unaffected.** The partial unique
  index on `dedup_key` lets `NULL` rows coexist; backfill is
  opt-in via the new CLI subcommand.
- **The bucket widths are conservative.** 5 m² and 25 € absorb
  the typical field drift (1-3 m² / 1-3 %) while keeping
  unrelated listings apart. A tighter bucket risks dropping
  distinct listings; a wider bucket risks keeping duplicates.
- **No embeddings activation.** The `embedding vector(1536)`
  column from `001_init_apartments.sql` stays nullable. Sprint
  4 decides the use case (Q2) and populates it.
- **The COALESCE upsert already preserves dedup_key.** When a
  row is rewritten on a backfill, the new dedup_key (if any)
  wins, the old one stays otherwise.

## Alternatives considered

- **Embeddings now (Sprint 3).** Rejected: the use case
  decision (Q2 in the roadmap) is explicitly deferred to
  Sprint 4. The deterministic key is the right primitive for
  the obvious duplicates; embeddings catch the long tail.
- **Hard delete the second row.** Loses the audit trail; the
  operator can no longer ask "which two portals both listed
  this apartment?". Keep both, surface the overlap in
  `validate-quality`.
- **`UNIQUE (source, external_id, dedup_key)` instead of a
  partial index.** Breaks Sprint 1's `(source, external_id)`
  constraint and forces every row to have a dedup_key, which
  the scraper can't always produce (e.g. no rooms badge on the
  Idealista search card).
- **Tighter buckets (1 m², 5 €).** Too aggressive: would drop
  two portals listing the same apartment with a 3 m² drift.
- **Looser buckets (10 m², 100 €).** Too permissive: would
  fold different apartments of the same rooms + nearby price.

## Future work

- **Sprint 4 (Q2):** activate the `embedding` column. The
  `dedup_key` becomes the *first-pass* dedup primitive; the
  embedding similarity is the *second-pass* catch for
  the long tail (same building, different floor / orientation).
- **Re-rank by similarity:** when the operator marks an
  apartment as "liked", find more listings similar to it via
  the embedding.
- **Natural-language search:** the LLM can rewrite a free-form
  query ("3-bedroom in Delicias, pet-friendly, under €1100")
  into a SQL filter using the embedding as the ranking signal.
