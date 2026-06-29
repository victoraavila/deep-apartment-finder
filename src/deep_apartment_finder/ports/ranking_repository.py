"""Repository port for ranking traces and notification dedup.

Two responsibilities live here because they share the same Postgres
pool and the same migration file:

- `apartment_scores` — one row per (ranking_run_id, apartment_id, criterion).
  The ranker writes one batch per ranking run; nothing else writes here.
- `notifications`    — one row per send, with a partial unique index
  enforcing at-most-one-per-day. `record_send` may raise
  `NotificationAlreadySent` when the same day is written twice
  (idempotent re-run case). The notifier catches that and logs.

The port keeps the `ranking_run_id` as an `uuid.UUID` because that's
what the ranker uses to thread a single run's writes together; the
adapter binds it as a `uuid` to Postgres' `uuid` column.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, runtime_checkable
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ScoreRow:
    """One row to write into `apartment_scores`."""

    apartment_id: int
    criterion: str
    score: float
    weight: float
    details: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class NotificationAlreadySent(Exception):
    """Raised by `record_send` when the same `sent_on` already has a row."""

    sent_on: date


@runtime_checkable
class RankingRepository(Protocol):
    """Persistence boundary for ranking traces and notification dedup."""

    async def write_scores(
        self, ranking_run_id: UUID, rows: list[ScoreRow]
    ) -> int:
        """Insert every row. Returns the count of rows written."""
        ...

    async def record_send(
        self,
        *,
        ranking_run_id: UUID,
        sent_on: date,
        apartment_ids: list[int],
    ) -> int:
        """Insert a `notifications` row.

        Raises `NotificationAlreadySent` if a row already exists for
        `sent_on` (the partial unique index turns the second insert
        into a violation). The notifier catches this and turns it into
        a no-op.
        """
        ...

    async def top_for_run(
        self, ranking_run_id: UUID, top_n: int
    ) -> list[dict[str, Any]]:
        """Return the top-N apartments for `ranking_run_id` by
        composite score. Used by the `list_top` tool.

        The result is `[{apartment_id, score}, ...]` sorted desc by
        score. We re-compute the weighted score from the trace rows
        so this stays a single round-trip.
        """
        ...

    async def delete_send_for_date(self, sent_on: date) -> int:
        """Delete the dedup row for `sent_on`, if present.

        Used by the notifier to roll back the dedup write when the
        SMTP send fails, so a re-run can retry. Returns the count
        of rows deleted (0 or 1).
        """
        ...


__all__ = [
    "NotificationAlreadySent",
    "RankingRepository",
    "ScoreRow",
]
