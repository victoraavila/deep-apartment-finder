"""`CliRunObserver` — prints phase headers and counters to stderr
in real time as events arrive.

The operator sees progress while the run is happening, not only a
final blob. Phase headers look like `=== researcher ===`,
`=== scraper (fotocasa) ===`, etc.; counters are one-line
`scraper: fetched 12 pages, 47 cards, 41 inserted, 6 duplicates`;
waits are labelled in domain terms (`waiting on LLM`,
`waiting on Fotocasa HTTP`, `waiting on Postgres`, `waiting on SMTP`).

The adapter does not own a logger; it talks to `sys.stderr` so the
output is visually distinct from any captured stdout (e.g. the
final JSON). Tests inject a custom `write` callable so the
stderr output can be snapshot-tested without touching the real
stderr.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any


class CliRunObserver:
    """A `RunObserver` that streams events to stderr in real time."""

    def __init__(
        self,
        *,
        write: Callable[[str], None] | None = None,
    ) -> None:
        self._write: Callable[[str], None] = write or (
            lambda line: print(line, file=sys.stderr, flush=True)
        )
        # Per-phase counter accumulators; the phase_end line prints
        # the snapshot. Counters are shared across phases by name
        # (e.g. `cards`) so the operator sees a running total.
        self._phase_counters: dict[str, dict[str, int]] = {}

    def _phase_counters_view(self, name: str) -> dict[str, int]:
        return self._phase_counters.setdefault(name, {})

    async def phase_start(self, name: str, **meta: Any) -> None:
        meta_str = ""
        if meta:
            meta_str = "  " + ", ".join(f"{k}={v}" for k, v in meta.items())
        self._write(f"=== {name} ==={meta_str}")
        # Reset the per-phase counters for the new phase.
        self._phase_counters[name] = {}

    async def phase_end(
        self,
        name: str,
        *,
        duration_ms: int,
        counts: dict[str, int] | None = None,
        errors: int = 0,
    ) -> None:
        # Merge the explicit `counts` arg with what we accumulated
        # via `count(...)` during the phase.
        merged = dict(self._phase_counters_view(name))
        if counts:
            for k, v in counts.items():
                merged[k] = merged.get(k, 0) + v
        parts: list[str] = []
        for k in sorted(merged):
            parts.append(f"{k} {merged[k]}")
        if errors:
            parts.append(f"err {errors}")
        parts.append(f"{duration_ms} ms")
        suffix = "  ".join(parts) if parts else f"{duration_ms} ms"
        self._write(f"  -> {name}: {suffix}")

    async def count(self, name: str, n: int = 1) -> None:
        # The CLI shows counts in the phase_end line, so we don't
        # print per-`count` lines by default. We could, but the
        # operator would be drowned in noise on a 50-card run.
        # Track them in the latest-active phase instead.
        # We use the most recent `start_phase` as the active phase;
        # if no phase has been started, the count is dropped
        # silently (the recording observer keeps it regardless).
        if not self._phase_counters:
            return
        active = next(reversed(self._phase_counters))
        self._phase_counters[active][name] = (
            self._phase_counters[active].get(name, 0) + n
        )

    async def waiting(self, label: str) -> None:
        self._write(f"  waiting on {label}")

    async def decision(self, label: str, value: str) -> None:
        self._write(f"  {label}: {value}")

    async def warning(self, msg: str) -> None:
        self._write(f"  warning: {msg}")

    async def error(self, msg: str, *, exc: BaseException | None = None) -> None:
        suffix = f"  ({exc!r})" if exc is not None else ""
        self._write(f"  error: {msg}{suffix}")


__all__ = ["CliRunObserver"]
