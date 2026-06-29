# ADR-007 — Haversine distance provider

- Status: Accepted
- Sprint: 2
- Date: 2026-06-29

## Context

The `DistanceToDangerousCriterion` needs a meters-based distance
between an apartment's lat/lng and each `dangerous_neighborhoods`
center. Two options were considered:

1. **Haversine** (great-circle) — straight-line distance on a
   sphere. Pure function, no I/O, no external service.
2. **OSRM / Mapbox** — route-based distance via a public routing
   engine. Reflects real walking/driving distance; adds a network
   dependency and a per-request cost (rate-limited, latency).

Sprint 2's scope is a daily, personal-scale agent. The ranking
must run quickly and offline. Route-based distance is a
*nice-to-have*, not a correctness requirement — an apartment
that's 800m straight-line from a dangerous neighborhood is almost
always closer to walk to it than one that's 5km away. The
straight-line heuristic is good enough for ranking, and the
operator can swap in OSRM later behind the same port.

## Decision

`HaversineDistanceProvider` implements the `DistanceProvider`
port. It is a pure function (`domain.geo.haversine_meters`),
zero I/O, easy to test. The `domain.geo` module is the
in-process reference implementation.

The `DistanceProvider` port is intentionally minimal
(`meters_between(lat1, lng1, lat2, lng2) -> float`); swapping
to OSRM in a future sprint is a single new class.

## Consequences

- Sprint 2 ranking is deterministic and offline. The full
  pipeline can run in CI without a network round-trip.
- The score is *not* "is this a safe walk home from the bar at
  3am"; it is "is this geographically close to a flagged
  area". That's a softer signal, and it is the right
  signal for a personal-scale agent.
- Tests for the ranker are unit-only (no DB, no network).
- A future sprint can add `OsrmDistanceProvider` (and a
  hybrid: route distance for "live ranking", haversine for
  "filter on ingest"). The ranker does not change.

## Out of scope (deferred)

- Route-based distance. The port is in place; the
  implementation is Sprint 5.
- Polygon-based "is this apartment inside a dangerous area".
  Today we compare against the center + radius.
