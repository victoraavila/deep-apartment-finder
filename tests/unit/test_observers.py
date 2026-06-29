"""`CliRunObserver` and `RecordingRunObserver` tests.

The CLI observer snapshots the stderr lines for a canned event
sequence so the operator-visible output stays stable. The
recording observer asserts the in-memory `RunReport` carries
every event the operator asked for.
"""

from __future__ import annotations

import json

import pytest

from deep_apartment_finder.adapters.observability.cli_observer import (
    CliRunObserver,
)
from deep_apartment_finder.adapters.observability.recording_observer import (
    RecordingRunObserver,
)
from deep_apartment_finder.ports.run_observer import RunObserver

# --- CliRunObserver --------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_observer_writes_phase_headers_on_phase_start() -> None:
    lines: list[str] = []
    obs = CliRunObserver(write=lines.append)
    await obs.phase_start("researcher")
    await obs.phase_end("researcher", duration_ms=42)
    assert lines[0] == "=== researcher ==="
    assert lines[1] == "  -> researcher: 42 ms"


@pytest.mark.asyncio
async def test_cli_observer_phase_start_carries_meta() -> None:
    lines: list[str] = []
    obs = CliRunObserver(write=lines.append)
    await obs.phase_start("scraper", source="fotocasa")
    assert lines[0] == "=== scraper ===  source=fotocasa"


@pytest.mark.asyncio
async def test_cli_observer_count_is_aggregated_per_phase() -> None:
    lines: list[str] = []
    obs = CliRunObserver(write=lines.append)
    await obs.phase_start("scraper")
    await obs.count("cards", 12)
    await obs.count("cards", 5)
    await obs.count("inserted", 17)
    await obs.phase_end("scraper", duration_ms=1000)
    # The phase_end line shows the aggregated counter snapshot.
    assert "cards 17" in lines[-1]
    assert "inserted 17" in lines[-1]
    assert "1000 ms" in lines[-1]


@pytest.mark.asyncio
async def test_cli_observer_phase_end_includes_error_count() -> None:
    lines: list[str] = []
    obs = CliRunObserver(write=lines.append)
    await obs.phase_start("scraper")
    await obs.phase_end("scraper", duration_ms=200, errors=2)
    assert "err 2" in lines[-1]


@pytest.mark.asyncio
async def test_cli_observer_waiting_decision_warning_error() -> None:
    lines: list[str] = []
    obs = CliRunObserver(write=lines.append)
    await obs.waiting("LLM")
    await obs.decision("researcher skipped", "already populated (n=6)")
    await obs.warning("partial sync")
    await obs.error("SMTP failed", exc=RuntimeError("conn refused"))
    assert lines == [
        "  waiting on LLM",
        "  researcher skipped: already populated (n=6)",
        "  warning: partial sync",
        "  error: SMTP failed  (RuntimeError('conn refused'))",
    ]


@pytest.mark.asyncio
async def test_cli_observer_can_be_typed_as_run_observer() -> None:
    """The observer must satisfy the `RunObserver` Protocol so the
    CLI can hold a list of observers and treat them uniformly."""
    obs: RunObserver = CliRunObserver(write=lambda _line: None)
    assert isinstance(obs, RunObserver)


# --- RecordingRunObserver -------------------------------------------------


@pytest.mark.asyncio
async def test_recording_observer_records_phase_events() -> None:
    obs = RecordingRunObserver(run_id="r-1")
    await obs.phase_start("ranker")
    await obs.count("apartments_scored", 10)
    await obs.phase_end("ranker", duration_ms=500, counts={"apartments_scored": 10})
    phases = obs.report.phases
    assert len(phases) == 1
    assert phases[0].name == "ranker"
    assert phases[0].duration_ms == 500
    assert obs.report.counts["apartments_scored"] == 10


@pytest.mark.asyncio
async def test_recording_observer_records_notes() -> None:
    obs = RecordingRunObserver()
    await obs.waiting("SMTP")
    await obs.decision("notifier skipped", "already sent today")
    await obs.warning("partial sync")
    await obs.error("SMTP failed", exc=RuntimeError("boom"))
    notes = obs.report.notes
    assert notes[0].kind == "waiting"
    assert notes[1].kind == "decision"
    assert notes[2].kind == "warning"
    assert notes[3].kind == "error"
    assert "RuntimeError" in notes[3].value


@pytest.mark.asyncio
async def test_recording_observer_finalize_persists_to_backend() -> None:
    class _FakeBackend:
        def __init__(self) -> None:
            self.writes: list[tuple[str, str]] = []

        async def awrite(self, path: str, content: str) -> object:
            self.writes.append((path, content))
            return object()

    backend = _FakeBackend()
    obs = RecordingRunObserver(run_id="r-1")
    await obs.phase_start("a")
    await obs.phase_end("a", duration_ms=10)
    report = await obs.finalize(backend=backend, report_path="/x/r-1.json")
    assert report.finished_at is not None
    assert report.report_path == "/x/r-1.json"
    assert len(backend.writes) == 1
    path, content = backend.writes[0]
    assert path == "/x/r-1.json"
    parsed = json.loads(content)
    assert parsed["run_id"] == "r-1"


@pytest.mark.asyncio
async def test_recording_observer_finalize_without_backend() -> None:
    obs = RecordingRunObserver()
    await obs.phase_start("a")
    await obs.phase_end("a", duration_ms=10)
    report = await obs.finalize(backend=None, report_path=None)
    assert report.finished_at is not None
    assert report.report_path is None


@pytest.mark.asyncio
async def test_recording_observer_finalize_swallows_write_errors() -> None:
    class _BoomBackend:
        async def awrite(self, path: str, content: str) -> object:
            raise RuntimeError("disk full")

    obs = RecordingRunObserver()
    await obs.phase_start("a")
    await obs.phase_end("a", duration_ms=10)
    report = await obs.finalize(backend=_BoomBackend(), report_path="/x/y.json")
    # The observer logs the failure but does not raise; the report
    # is still returned.
    assert report.finished_at is not None
    assert report.report_path is None


@pytest.mark.asyncio
async def test_recording_observer_can_be_typed_as_run_observer() -> None:
    obs: RunObserver = RecordingRunObserver()
    assert isinstance(obs, RunObserver)


@pytest.mark.asyncio
async def test_cli_fanout_records_cli_owned_phases() -> None:
    """CLI-owned phases must be persisted, not only printed to stderr."""
    from deep_apartment_finder.cli import _FanOutObserver

    lines: list[str] = []
    cli = CliRunObserver(write=lines.append)
    recording = RecordingRunObserver(run_id="r-1")
    fanout = _FanOutObserver([cli, recording])

    await fanout.phase_start("setup", run_id="r-1")
    await fanout.decision("LangSmith tracing", "on")
    await fanout.phase_end("setup", duration_ms=7)

    assert lines[0] == "=== setup ===  run_id=r-1"
    setup = recording.report.phase("setup")
    assert setup is not None
    assert setup.duration_ms == 7
    assert setup.meta == {"run_id": "r-1"}
    assert recording.report.notes[0].label == "LangSmith tracing"
