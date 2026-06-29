"""Tests for the orchestrator's `RunObserver` wiring.

Sprint 3 (Pillar A) added a `RunObserver` injection point on the
deterministic steps. These tests exercise the wiring end-to-end
with a recording fake, asserting the observer receives
`phase_start` + `phase_end` events for the ranker and notifier
phases, with the right counts in the right order.

The `RunObserver` Protocol method is `phase_end`; an earlier
Sprint 3 PR accidentally used the `RunReport.end_phase` name in
the orchestrator, which broke the CLI on every run. These tests
exist to catch that class of typo.
"""

from __future__ import annotations

from typing import Any

import pytest

from deep_apartment_finder.adapters.observability.cli_observer import (
    CliRunObserver,
)
from deep_apartment_finder.adapters.observability.recording_observer import (
    RecordingRunObserver,
)
from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.geo import DangerousNeighborhood
from deep_apartment_finder.domain.ranking import RankableApartment
from deep_apartment_finder.domain.source import Source
from tests._fakes import (
    InMemoryApartmentRepository,
    InMemoryDangerousNeighborhoodRepository,
    InMemoryRankingRepository,
)


class _RecordingObserver:
    """A `RunObserver` that records every event for assertion.

    Implements the same surface as `CliRunObserver` and
    `RecordingRunObserver`, so a test failure here points to a
    bug in the orchestrator's call sites (or in the Protocol).
    """

    def __init__(self) -> None:
        self.phase_starts: list[tuple[str, dict[str, Any]]] = []
        self.phase_ends: list[dict[str, Any]] = []
        self.counts: list[tuple[str, int]] = []
        self.waitings: list[str] = []
        self.decisions: list[tuple[str, str]] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    async def phase_start(self, name: str, **meta: Any) -> None:
        self.phase_starts.append((name, dict(meta)))

    async def phase_end(
        self,
        name: str,
        *,
        duration_ms: int,
        counts: dict[str, int] | None = None,
        errors: int = 0,
    ) -> None:
        self.phase_ends.append(
            {
                "name": name,
                "duration_ms": duration_ms,
                "counts": dict(counts or {}),
                "errors": errors,
            }
        )

    async def count(self, name: str, n: int = 1) -> None:
        self.counts.append((name, n))

    async def waiting(self, label: str) -> None:
        self.waitings.append(label)

    async def decision(self, label: str, value: str) -> None:
        self.decisions.append((label, value))

    async def warning(self, msg: str) -> None:
        self.warnings.append(msg)

    async def error(self, msg: str, *, exc: BaseException | None = None) -> None:
        self.errors.append(msg)


def _build_orchestrator_with_observer(
    *, observer: Any
) -> Any:
    """Build an orchestrator with a real LLM stub and a fakes
    pipeline, wired to the given observer.

    We construct the orchestrator's `_DeterministicSteps` directly
    so the test doesn't need an LLM, a `create_deep_agent` call,
    or a Postgres pool.
    """
    from deep_apartment_finder.agent.orchestrator import _DeterministicSteps

    repo = InMemoryApartmentRepository()
    dangerous_repo = InMemoryDangerousNeighborhoodRepository()
    ranking_repo = InMemoryRankingRepository()
    return _DeterministicSteps(
        repo=repo,
        dangerous_repo=dangerous_repo,
        ranking_repo=ranking_repo,
        notifier=None,
        backend=type(
            "_B",
            (),
            {"awrite": staticmethod(lambda *args, **kw: _noop(*args, **kw))},
        )(),
        from_address=None,
        to_address=None,
        weight_distance=0.5,
        weight_pet_policy=0.3,
        weight_furnished=0.2,
        max_distance_m=2000.0,
        top_n=5,
        observer=observer,
    )


async def _noop(*args: Any, **kwargs: Any) -> Any:
    class _R:
        def __init__(self_inner, p: str) -> None:
            self_inner.path = p

    return _R(kwargs.get("path", "") or (args[0] if args else ""))


def _seed_apartments(
    repo: InMemoryApartmentRepository,
    dangerous_repo: InMemoryDangerousNeighborhoodRepository,
) -> tuple[RankableApartment, ...]:
    """Seed two apartments that pass the hard filters."""
    from decimal import Decimal

    apt_far = Apartment(
        source=Source.FOTOCASA,
        external_id="far",
        url="https://x/far",
        title="Far",
        price_eur=Decimal("900"),
        rooms=2,
        bathrooms=2,
        size_m2=Decimal("60"),
        address="Calle X, Zaragoza",
        lat=Decimal("41.7"),
        lng=Decimal("-0.95"),
        pet_policy="allowed",
        furnished="true",
    )
    apt_near = Apartment(
        source=Source.FOTOCASA,
        external_id="near",
        url="https://x/near",
        title="Near",
        price_eur=Decimal("900"),
        rooms=2,
        bathrooms=2,
        size_m2=Decimal("60"),
        address="Calle Y, Zaragoza",
        lat=Decimal("41.6517"),
        lng=Decimal("-0.9088"),
        pet_policy="not_allowed",
        furnished="false",
    )

    async def _do_seed() -> tuple[RankableApartment, ...]:
        await dangerous_repo.upsert_many(
            [DangerousNeighborhood("Delicias", 41.6517, -0.9088, 600)],
            source="test",
        )
        far_id = (await repo.upsert(apt_far)).apartment_id
        near_id = (await repo.upsert(apt_near)).apartment_id
        return (
            RankableApartment(apartment=apt_far, db_id=far_id),
            RankableApartment(apartment=apt_near, db_id=near_id),
        )

    return _do_seed()


@pytest.mark.asyncio
async def test_orchestrator_phase_end_is_called_with_correct_method_name() -> None:
    """Regression test: the orchestrator must call
    `observer.phase_end(...)`, not `observer.end_phase(...)`. The
    `RunObserver` Protocol exposes `phase_end`; the `RunReport`
    domain object has its own `end_phase` (different class, no
    relation). The CLI used to call `end_phase` on the observer
    and crashed on every run.
    """
    obs = _RecordingObserver()
    steps = _build_orchestrator_with_observer(observer=obs)
    await _seed_apartments(steps._repo, steps._dangerous_repo)

    result = await steps.run()
    assert result["apartments_scored"] == 2

    # The orchestrator's `_phase_ranker` and `_phase_notifier`
    # both fire `phase_end`. If the implementation is calling
    # `end_phase` instead (the original bug), this attribute
    # access raises `AttributeError` and the test fails.
    names = [p["name"] for p in obs.phase_ends]
    assert "ranker_setup" in names
    assert "ranker" in names
    # The notifier phase is skipped when notifier is None, but
    # the `_phase_notifier` still fires `phase_end` with an
    # empty counts dict.
    assert "notifier" in names

    # The ranker phase carries the headline counts the operator
    # sees in the stderr line.
    ranker = next(p for p in obs.phase_ends if p["name"] == "ranker")
    assert ranker["counts"]["apartments_scored"] == 2
    assert ranker["counts"]["scores_written"] == 6
    assert ranker["counts"]["top_n_returned"] == 2


@pytest.mark.asyncio
async def test_orchestrator_observer_is_called_in_phase_order() -> None:
    """The phases fire in the documented order: ranker_setup
    -> ranker -> notifier."""
    obs = _RecordingObserver()
    steps = _build_orchestrator_with_observer(observer=obs)
    await _seed_apartments(steps._repo, steps._dangerous_repo)
    await steps.run()

    start_names = [name for name, _ in obs.phase_starts]
    end_names = [p["name"] for p in obs.phase_ends]
    assert start_names == ["ranker_setup", "ranker", "notifier"]
    assert end_names == ["ranker_setup", "ranker", "notifier"]


@pytest.mark.asyncio
async def test_orchestrator_observer_receives_decisions() -> None:
    """The orchestrator emits a `decision` annotation when the
    dangerous_neighborhoods table is empty, so the operator
    sees the distance-criterion behaviour change in the CLI."""
    obs = _RecordingObserver()
    steps = _build_orchestrator_with_observer(observer=obs)
    # No neighborhoods seeded.
    await steps._repo.upsert(  # type: ignore[attr-defined]
        Apartment(
            source=Source.FOTOCASA,
            external_id="a1",
            url="https://x/a1",
            title="Apt",
        )
    )
    await steps.run()
    decision_labels = [label for label, _ in obs.decisions]
    assert "ranker" in decision_labels


@pytest.mark.asyncio
async def test_real_observers_satisfy_run_observer_protocol() -> None:
    """`CliRunObserver` and `RecordingRunObserver` must implement
    every method the orchestrator calls. If a method is renamed
    on the Protocol but the adapters don't pick it up, this test
    fails immediately instead of crashing the CLI on first run.
    """
    from deep_apartment_finder.ports.run_observer import RunObserver

    for obs in (CliRunObserver(), RecordingRunObserver()):
        assert isinstance(obs, RunObserver)
        # Every method the orchestrator/CLI call must exist.
        for name in (
            "phase_start",
            "phase_end",
            "count",
            "waiting",
            "decision",
            "warning",
            "error",
        ):
            assert hasattr(obs, name), f"{type(obs).__name__} missing {name}"
            assert callable(getattr(obs, name))


@pytest.mark.asyncio
async def test_recording_observer_phase_end_records_real_counts() -> None:
    """The recording observer's `phase_end` must persist the
    counts so the run report shows them. Regression: the
    `phase_end` method on the recorder used to be named
    `end_phase` (the RunReport method, not the observer method),
    so the report never had phase-level counts.
    """
    obs = RecordingRunObserver(run_id="r-1")
    await obs.phase_start("ranker")
    await obs.count("apartments_scored", 10)
    await obs.phase_end(
        "ranker",
        duration_ms=500,
        counts={"apartments_scored": 10, "scores_written": 30},
    )
    ranker = obs.report.phase("ranker")
    assert ranker is not None
    assert ranker.counts == {"apartments_scored": 10, "scores_written": 30}
    assert ranker.duration_ms == 500
    assert ranker.errors == 0


# Module-level marker for tooling that greps for the public surface.
__all__ = ["test_orchestrator_phase_end_is_called_with_correct_method_name"]
