"""`RunReport` tests.

The `RunReport` is the structured record a single CLI run produces.
Tests cover the event-accumulation contract, the JSON
serialisation, and the lifecycle hooks.
"""

from __future__ import annotations

import json

from deep_apartment_finder.domain.run_report import (
    NoteEvent,
    PhaseEvent,
    RunReport,
)


def test_run_report_starts_with_empty_state() -> None:
    r = RunReport()
    assert r.phases == []
    assert r.counts == {}
    assert r.notes == []
    assert r.finished_at is None
    assert r.top_n == []


def test_run_report_accepts_a_provided_run_id() -> None:
    r = RunReport(run_id="abc-123")
    assert r.run_id == "abc-123"


def test_run_report_start_phase_creates_event() -> None:
    r = RunReport()
    r.start_phase("scraper", source="fotocasa")
    assert len(r.phases) == 1
    ev = r.phases[0]
    assert ev.name == "scraper"
    assert ev.meta == {"source": "fotocasa"}
    assert ev.finished_at is None
    assert ev.duration_ms is None


def test_run_report_end_phase_sets_duration_and_counts() -> None:
    r = RunReport()
    r.start_phase("scraper")
    r.end_phase("scraper", duration_ms=1234, counts={"cards": 10}, errors=0)
    ev = r.phase("scraper")
    assert ev is not None
    assert ev.finished_at is not None
    assert ev.duration_ms == 1234
    assert ev.counts == {"cards": 10}
    assert ev.errors == 0


def test_run_report_end_phase_is_safe_without_start() -> None:
    """A defensive `end_phase` without a matching `start_phase` should
    not raise; the operator sees a placeholder phase in the report."""
    r = RunReport()
    r.end_phase("scraper", duration_ms=10)
    ev = r.phase("scraper")
    assert ev is not None
    assert ev.name == "scraper"


def test_run_report_counts_accumulate() -> None:
    r = RunReport()
    r.add_count("cards", 5)
    r.add_count("cards", 3)
    r.add_count("inserted", 4)
    assert r.counts["cards"] == 8
    assert r.counts["inserted"] == 4


def test_run_report_note_records_event() -> None:
    r = RunReport()
    r.note("decision", "researcher skipped", "already populated")
    r.note("warning", "partial sync", "timeout on page 3")
    assert len(r.notes) == 2
    assert r.notes[0].kind == "decision"
    assert r.notes[1].value == "timeout on page 3"


def test_run_report_finish_records_finished_at() -> None:
    r = RunReport()
    r.finish()
    assert r.finished_at is not None
    assert r.duration_ms() is not None
    assert r.duration_ms() >= 0


def test_run_report_to_dict_includes_all_event_kinds() -> None:
    r = RunReport(run_id="r-1")
    r.start_phase("scraper")
    r.add_count("cards", 12)
    r.end_phase("scraper", duration_ms=2000, counts={"cards": 12})
    r.note("decision", "x", "y")
    r.set_top_n([{"apartment_id": 1, "score": 0.9}])
    r.set_dedup_dropped(1)
    r.set_criterion_distribution("distance_to_dangerous", [0.5, 0.7])
    r.ranking_run_id = "uuid-1"
    r.notification_sent = True
    r.trace_url = "https://smith.langchain.com/r/abc"
    r.report_path = "/orchestrator/reports/r-1.json"
    r.finish()

    d = r.to_dict()
    assert d["run_id"] == "r-1"
    assert d["finished_at"] is not None
    assert d["duration_ms"] is not None
    assert len(d["phases"]) == 1
    assert d["phases"][0]["name"] == "scraper"
    assert d["phases"][0]["duration_ms"] == 2000
    assert d["counts"]["cards"] == 12
    assert d["notes"][0]["label"] == "x"
    assert d["top_n"] == [{"apartment_id": 1, "score": 0.9}]
    assert d["dedup_dropped"] == 1
    assert d["criterion_distributions"]["distance_to_dangerous"] == [0.5, 0.7]
    assert d["ranking_run_id"] == "uuid-1"
    assert d["notification"]["sent"] is True
    assert d["trace_url"] == "https://smith.langchain.com/r/abc"
    assert d["report_path"] == "/orchestrator/reports/r-1.json"


def test_run_report_to_json_is_valid_json() -> None:
    r = RunReport(run_id="r-1")
    r.start_phase("a")
    r.end_phase("a", duration_ms=10)
    parsed = json.loads(r.to_json())
    assert parsed["run_id"] == "r-1"
    assert parsed["phases"][0]["name"] == "a"


def test_run_report_drops_empty_optional_fields() -> None:
    """`to_dict` should not include keys whose value is `None`/empty,
    so the persisted JSON stays small."""
    r = RunReport()
    r.finish()
    d = r.to_dict()
    assert "top_n" not in d
    assert "dedup_dropped" not in d
    assert "criterion_distributions" not in d
    assert "ranking_run_id" not in d
    assert "notification" not in d
    assert "trace_url" not in d
    assert "report_path" not in d


def test_phase_event_to_dict_is_stable() -> None:
    from datetime import UTC, datetime

    ev = PhaseEvent(
        name="x",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        finished_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        duration_ms=1000,
        counts={"a": 1},
        errors=2,
        meta={"k": "v"},
    )
    d = ev.to_dict()
    assert d["name"] == "x"
    assert d["started_at"].startswith("2026-01-01")
    assert d["duration_ms"] == 1000
    assert d["counts"] == {"a": 1}
    assert d["errors"] == 2
    assert d["meta"] == {"k": "v"}


def test_note_event_to_dict() -> None:
    from datetime import UTC, datetime

    ev = NoteEvent(
        kind="waiting", label="waiting on SMTP", value="connecting...",
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    d = ev.to_dict()
    assert d["kind"] == "waiting"
    assert d["label"] == "waiting on SMTP"
    assert d["value"] == "connecting..."
    assert d["at"].startswith("2026-01-01")
