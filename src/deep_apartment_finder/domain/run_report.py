"""`RunReport` — the structured record of a single CLI `run` invocation.

The orchestrator and the CLI both accumulate events into this object
through the `RunObserver` port. At the end of the run the
`RecordingRunObserver` returns the `RunReport`, which is persisted to
disk (`/orchestrator/reports/<run-uuid>.json`) and to the
`run_reports` Postgres table for SQL inspection.

The shape is documented in `docs/SPRINT3.md` (Pillar A). Operators
can read the JSON to answer questions like "how many cards did the
Fotocasa scraper see on Tuesday's run?" or "did the notifier actually
send on Friday?" without re-running the pipeline.

Three event kinds flow into the report:

- **phase** — a phase boundary. `phase_start` records the start time
  and any metadata (e.g. the scraper source); `phase_end` records
  duration, counts, and an error count.
- **count** — an incremental counter. `count("cards", 12)` means
  "12 more cards seen". Counters are accumulated per name.
- **note** — a free-form human-readable annotation. The observer
  adapters split this into `decision` (state-transition) /
  `warning` / `error` / `waiting` semantically, but the domain
  `RunReport` doesn't distinguish them — it's a chronological
  log.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class PhaseEvent:
    """One phase boundary: start, end, duration, counts, errors."""

    name: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    counts: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "started_at": self.started_at.isoformat(),
        }
        if self.finished_at is not None:
            d["finished_at"] = self.finished_at.isoformat()
        if self.duration_ms is not None:
            d["duration_ms"] = self.duration_ms
        if self.counts:
            d["counts"] = dict(self.counts)
        if self.errors:
            d["errors"] = self.errors
        if self.meta:
            d["meta"] = dict(self.meta)
        return d


@dataclass(frozen=True, slots=True)
class NoteEvent:
    """A free-form human-readable annotation."""

    kind: str  # "decision" | "warning" | "error" | "waiting" | "info"
    label: str
    value: str
    at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "value": self.value,
            "at": self.at.isoformat(),
        }


class RunReport:
    """Accumulates the events of a single CLI `run` invocation.

    The report is mutated in place by the `RecordingRunObserver`.
    Methods that record an event return `None` so the call sites read
    like English (`observer.phase_start("ranker")`) instead of
    `await observer.phase_start("ranker")` when the observer is
    synchronous.
    """

    def __init__(self, *, run_id: str | None = None) -> None:
        self.run_id: str = run_id or str(uuid.uuid4())
        self.started_at: datetime = _utcnow()
        self.finished_at: datetime | None = None
        # Phases keyed by name. We allow at most one in-flight phase
        # per name; the observer uses an explicit `phase_end` to
        # finish a phase.
        self._phases: dict[str, PhaseEvent] = {}
        # Counter accumulators keyed by name.
        self.counts: Counter[str] = Counter()
        # Chronological note log.
        self.notes: list[NoteEvent] = []
        # Per-criterion score distribution (Sprint 3 Pillar B).
        self.criterion_distributions: dict[str, list[float]] = {}
        # Top-N apartments (Sprint 3 Pillar C). Filled in by the
        # deterministic ranker + the notifier.
        self.top_n: list[dict[str, Any]] = []
        # The dedup-dropped count from the ranker (Pillar F).
        self.dedup_dropped: int = 0
        # Persisted-rank cross-references (filled in at phase end).
        self.ranking_run_id: str | None = None
        self.notification_sent: bool = False
        self.notification_skipped_reason: str | None = None
        self.notification_subject: str | None = None
        # LangSmith trace URL (Pillar B).
        self.trace_url: str | None = None
        # Where the report was written on disk.
        self.report_path: str | None = None

    # --- phase --------------------------------------------------------

    def start_phase(self, name: str, **meta: Any) -> None:
        self._phases[name] = PhaseEvent(
            name=name, started_at=_utcnow(), meta=dict(meta)
        )

    def end_phase(
        self,
        name: str,
        *,
        duration_ms: int,
        counts: dict[str, int] | None = None,
        errors: int = 0,
    ) -> None:
        ev = self._phases.get(name)
        if ev is None:
            # Defensive: an end without a start. Create a placeholder.
            ev = PhaseEvent(name=name, started_at=_utcnow())
        finished = _utcnow()
        self._phases[name] = PhaseEvent(
            name=name,
            started_at=ev.started_at,
            finished_at=finished,
            duration_ms=duration_ms,
            counts=dict(counts or {}),
            errors=errors,
            meta=dict(ev.meta),
        )

    def phase(self, name: str) -> PhaseEvent | None:
        return self._phases.get(name)

    @property
    def phases(self) -> list[PhaseEvent]:
        # Return in insertion order so the JSON is stable.
        return list(self._phases.values())

    # --- counts -------------------------------------------------------

    def add_count(self, name: str, n: int = 1) -> None:
        self.counts[name] += n

    # --- notes --------------------------------------------------------

    def note(
        self, kind: str, label: str, value: str, at: datetime | None = None
    ) -> None:
        self.notes.append(
            NoteEvent(
                kind=kind,
                label=label,
                value=value,
                at=at or _utcnow(),
            )
        )

    # --- top-N --------------------------------------------------------

    def set_top_n(self, top_n: list[dict[str, Any]]) -> None:
        self.top_n = list(top_n)

    def set_dedup_dropped(self, dropped: int) -> None:
        self.dedup_dropped = int(dropped)

    def set_criterion_distribution(
        self, criterion: str, scores: list[float]
    ) -> None:
        self.criterion_distributions[criterion] = [float(s) for s in scores]

    # --- lifecycle ----------------------------------------------------

    def finish(self) -> None:
        self.finished_at = _utcnow()

    def duration_ms(self) -> int | None:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    # --- serialisation ------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms(),
            "phases": [p.to_dict() for p in self.phases],
            "counts": dict(self.counts),
            "notes": [n.to_dict() for n in self.notes],
        }
        if self.top_n:
            d["top_n"] = self.top_n
        if self.dedup_dropped:
            d["dedup_dropped"] = self.dedup_dropped
        if self.criterion_distributions:
            d["criterion_distributions"] = self.criterion_distributions
        if self.ranking_run_id is not None:
            d["ranking_run_id"] = self.ranking_run_id
        if self.notification_sent or self.notification_skipped_reason:
            d["notification"] = {
                "sent": self.notification_sent,
                "skipped_reason": self.notification_skipped_reason,
                "subject": self.notification_subject,
            }
        if self.trace_url is not None:
            d["trace_url"] = self.trace_url
        if self.report_path is not None:
            d["report_path"] = self.report_path
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    # --- convenience --------------------------------------------------

    def __iter__(self) -> Iterator[Any]:
        yield from ()


__all__ = ["NoteEvent", "PhaseEvent", "RunReport"]
