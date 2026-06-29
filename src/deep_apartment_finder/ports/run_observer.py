"""`RunObserver` port — the single sink both the CLI and the
orchestrator emit events into.

The contract is documented in `docs/SPRINT3.md` (Pillar A). The
two adapters are `CliRunObserver` (prints phase headers and
counters to stderr in real time) and `RecordingRunObserver`
(collects every event into a `RunReport` that is persisted to
`/orchestrator/reports/<run-uuid>.json` and the `run_reports`
Postgres table at the end of the run).

The methods are async because the CLI always drives the
observer from an async context; the recording adapter ignores
the awaitable and the CLI adapter uses `print(...)` which is
non-blocking enough for stderr.

The observer does not own a logger. `logger.info` stays in
production code for low-level debug; the observer is the
operator-facing surface.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class RunObserver(Protocol):
    """The single sink for run-time events.

    Methods are `async` because the CLI runs in an async context,
    but a synchronous adapter can implement them as `async def
    method(...): return None`.
    """

    async def phase_start(self, name: str, **meta: Any) -> None:
        """Mark the start of a phase. Meta is free-form (e.g. the
        scraper source)."""
        ...

    async def phase_end(
        self,
        name: str,
        *,
        duration_ms: int,
        counts: dict[str, int] | None = None,
        errors: int = 0,
    ) -> None:
        """Mark the end of a phase. `counts` is the per-phase
        counter snapshot the operator sees in the run report."""
        ...

    async def count(self, name: str, n: int = 1) -> None:
        """Add `n` to a named counter. Same name across phases
        accumulates in the same counter."""
        ...

    async def waiting(self, label: str) -> None:
        """Annotate a wait. Renders as `  waiting on <label>`."""
        ...

    async def decision(self, label: str, value: str) -> None:
        """Annotate a state transition. Renders as
        `  <label>: <value>`."""
        ...

    async def warning(self, msg: str) -> None:
        """Non-fatal warning. Rendered with a `warning:` prefix."""
        ...

    async def error(self, msg: str, *, exc: BaseException | None = None) -> None:
        """Fatal error. Rendered with an `error:` prefix; `exc` is
        attached when present."""
        ...


__all__ = ["RunObserver"]
