"""`RecordingRunObserver` — collects every event into a `RunReport`
that the CLI persists at the end of the run.

The report is serialised to JSON, written to
`/orchestrator/reports/<run-uuid>.json` via the shared
`CompositeBackend`, and (optionally) inserted into the
`run_reports` Postgres table for SQL inspection.

The observer's `report` property is the live `RunReport`
instance; the CLI calls `await observer.finalize(backend,
report_path)` at the end of the run to flush it. Finalising is
a separate step so the observer can capture events that arrive
during the persistence step (e.g. a write failure) and
decorate the report with the persisted path.
"""

from __future__ import annotations

import logging
from typing import Any

from deepagents.backends.protocol import BackendProtocol

from deep_apartment_finder.domain.run_report import RunReport
from deep_apartment_finder.ports.run_observer import RunObserver

logger = logging.getLogger(__name__)


class RecordingRunObserver(RunObserver):
    """A `RunObserver` that accumulates events into a `RunReport`.

    `run_id` defaults to a fresh UUID4; the CLI passes a
    pre-generated one so the report path is predictable.
    """

    def __init__(self, *, run_id: str | None = None) -> None:
        self.report: RunReport = RunReport(run_id=run_id)

    async def phase_start(self, name: str, **meta: Any) -> None:
        self.report.start_phase(name, **meta)

    async def phase_end(
        self,
        name: str,
        *,
        duration_ms: int,
        counts: dict[str, int] | None = None,
        errors: int = 0,
    ) -> None:
        self.report.end_phase(
            name, duration_ms=duration_ms, counts=counts, errors=errors
        )

    async def count(self, name: str, n: int = 1) -> None:
        self.report.add_count(name, n)

    async def waiting(self, label: str) -> None:
        self.report.note("waiting", label, "")

    async def decision(self, label: str, value: str) -> None:
        self.report.note("decision", label, value)

    async def warning(self, msg: str) -> None:
        self.report.note("warning", msg, "")

    async def error(self, msg: str, *, exc: BaseException | None = None) -> None:
        suffix = repr(exc) if exc is not None else ""
        self.report.note("error", msg, suffix)

    # --- finalisation ------------------------------------------------

    async def finalize(
        self,
        *,
        backend: BackendProtocol | None,
        report_path: str | None = None,
    ) -> RunReport:
        """Mark the report finished, optionally persist it, and return it.

        Persists to the configured `backend` (the agent's
        `CompositeBackend`) when both `backend` and `report_path`
        are provided. A write failure is logged but does not raise
        — the report is still returned for the CLI to print.
        """
        self.report.finish()
        if backend is not None and report_path is not None:
            try:
                await backend.awrite(
                    report_path, self.report.to_json()
                )
                self.report.report_path = report_path
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "RecordingRunObserver: failed to persist %s: %s",
                    report_path,
                    exc,
                )
        return self.report


__all__ = ["RecordingRunObserver"]
