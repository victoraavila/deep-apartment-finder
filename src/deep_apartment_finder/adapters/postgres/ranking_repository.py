"""Postgres impl of `RankingRepository`.

Writes `apartment_scores` (one row per criterion per apartment) and
`notifications` (one row per send). The `record_send` translate the
unique-violation on `notifications_one_per_day_idx` into a
`NotificationAlreadySent` exception so the notifier can handle
re-runs gracefully without a `try/except asyncpg.UniqueViolationError`
scattered around the orchestrator.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

import asyncpg

from deep_apartment_finder.ports.ranking_repository import (
    NotificationAlreadySent,
    RankingRepository,
    ScoreRow,
)

_SCORE_INSERT_SQL = """
INSERT INTO apartment_scores
    (ranking_run_id, apartment_id, criterion, score, weight, details)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""


class PostgresRankingRepository(RankingRepository):
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def write_scores(
        self, ranking_run_id: UUID, rows: list[ScoreRow]
    ) -> int:
        if not rows:
            return 0
        import json

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for r in rows:
                    await conn.execute(
                        _SCORE_INSERT_SQL,
                        ranking_run_id,
                        r.apartment_id,
                        r.criterion,
                        r.score,
                        r.weight,
                        json.dumps(r.details or {}),
                    )
        return len(rows)

    async def record_send(
        self,
        *,
        ranking_run_id: UUID,
        sent_on: date,
        apartment_ids: list[int],
    ) -> int:
        sql = """
            INSERT INTO notifications
                (sent_on, apartment_ids, ranking_run_id)
            VALUES ($1, $2, $3)
            RETURNING id
        """
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    sql, sent_on, list(apartment_ids), ranking_run_id
                )
        except asyncpg.UniqueViolationError as exc:
            # The partial unique index `notifications_one_per_day_idx`
            # is the source of truth: at most one notification per day.
            raise NotificationAlreadySent(sent_on=sent_on) from exc
        if row is None:
            raise NotificationAlreadySent(sent_on=sent_on)
        return int(row["id"])

    async def top_for_run(
        self, ranking_run_id: UUID, top_n: int
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT apartment_id,
                   SUM(score * weight) / NULLIF(SUM(weight), 0) AS final_score
            FROM apartment_scores
            WHERE ranking_run_id = $1
            GROUP BY apartment_id
            ORDER BY final_score DESC NULLS LAST
            LIMIT $2
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, ranking_run_id, top_n)
        return [
            {"apartment_id": int(r["apartment_id"]), "score": float(r["final_score"] or 0.0)}
            for r in rows
        ]

    async def delete_send_for_date(self, sent_on: date) -> int:
        sql = "DELETE FROM notifications WHERE sent_on = $1"
        async with self._pool.acquire() as conn:
            result = await conn.execute(sql, sent_on)
        # asyncpg's `execute` returns a status string like "DELETE 1".
        try:
            return int(result.rsplit(" ", 1)[-1])
        except (AttributeError, ValueError):
            return 0


__all__ = ["PostgresRankingRepository"]
