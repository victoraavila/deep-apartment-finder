"""`RunReportRepository` — persists `RunReport` rows to the
`run_reports` Postgres table for SQL inspection.

The persisted-on-disk JSON at
`/orchestrator/reports/<run-uuid>.json` is the source of truth
for the full report; the DB row carries only the headline
counters + the persisted path so a `SELECT * FROM run_reports
ORDER BY started_at DESC LIMIT 1` answers the operator's
"what did the last run do?" question without reading the file.

The repository is a thin layer over `asyncpg`. It mirrors the
shape of `PostgresApartmentRepository` (no ORM, raw SQL,
idempotent where possible).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from deep_apartment_finder.domain.run_report import RunReport

_UPSERT_SQL = """
INSERT INTO run_reports (
    run_id, started_at, finished_at, phases, counts,
    ranking_run_id, notification_sent, report_path, trace_url
) VALUES (
    $1, $2, $3, $4::jsonb, $5::jsonb, $6, $7, $8, $9
)
ON CONFLICT (run_id) DO UPDATE SET
    finished_at = EXCLUDED.finished_at,
    phases = EXCLUDED.phases,
    counts = EXCLUDED.counts,
    ranking_run_id = EXCLUDED.ranking_run_id,
    notification_sent = EXCLUDED.notification_sent,
    report_path = EXCLUDED.report_path,
    trace_url = EXCLUDED.trace_url
"""


_FETCH_SQL = """
SELECT run_id, started_at, finished_at, phases, counts,
       ranking_run_id, notification_sent, report_path, trace_url
FROM run_reports
WHERE run_id = $1
"""


def _row_to_report_dict(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "run_id": str(row["run_id"]),
        "started_at": row["started_at"].isoformat(),
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
        "phases": row["phases"],
        "counts": row["counts"],
        "ranking_run_id": (
            str(row["ranking_run_id"]) if row["ranking_run_id"] else None
        ),
        "notification_sent": bool(row["notification_sent"]),
        "report_path": row["report_path"],
        "trace_url": row["trace_url"],
    }


class PostgresRunReportRepository:
    """Persist and fetch `RunReport` rows."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def upsert(self, report: RunReport) -> None:
        d = report.to_dict()
        async with self._pool.acquire() as conn:
            await conn.execute(
                _UPSERT_SQL,
                UUID(d["run_id"]),
                datetime.fromisoformat(d["started_at"]),
                datetime.fromisoformat(d["finished_at"]) if d["finished_at"] else None,
                json.dumps(d.get("phases", [])),
                json.dumps(d.get("counts", {})),
                UUID(d["ranking_run_id"]) if d.get("ranking_run_id") else None,
                d.get("notification", {}).get("sent", False)
                if d.get("notification")
                else False,
                d.get("report_path"),
                d.get("trace_url"),
            )

    async def fetch(self, run_id: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_FETCH_SQL, UUID(run_id))
        if row is None:
            return None
        return _row_to_report_dict(row)


__all__ = ["PostgresRunReportRepository"]
