"""Composition root tests."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from deep_apartment_finder.agent.orchestrator import _DeterministicSteps
from deep_apartment_finder.config import Settings
from deep_apartment_finder.domain.geo import DangerousNeighborhood
from deep_apartment_finder.main import RunContext, build_orchestrator_for_cli
from tests._fakes import (
    FakeScraper,
    InMemoryApartmentRepository,
    InMemoryDangerousNeighborhoodRepository,
    InMemoryRankingRepository,
    make_apartment,
)


def test_build_orchestrator_for_cli_is_synchronous(monkeypatch: Any) -> None:
    """Deep Agents construction is sync; the CLI must not receive a coroutine."""

    sentinel_agent = object()

    def _fake_llm(settings: Settings) -> object:
        return object()

    def _fake_build_orchestrator(**kwargs: object) -> object:
        return sentinel_agent

    monkeypatch.setattr(
        "deep_apartment_finder.main.build_chat_model_with_fallback", _fake_llm
    )
    monkeypatch.setattr("deep_apartment_finder.main.build_orchestrator", _fake_build_orchestrator)

    ctx = RunContext(
        settings=Settings(groq_api_key="test"),
        pool=object(),  # type: ignore[arg-type]
        scraper=FakeScraper(),
        idealista_scraper=None,
        repo=InMemoryApartmentRepository(),
        dangerous_repo=InMemoryDangerousNeighborhoodRepository(),
        ranking_repo=InMemoryRankingRepository(),
    )

    result = build_orchestrator_for_cli(ctx)

    assert result is sentinel_agent
    assert not inspect.isawaitable(result)


def test_build_orchestrator_for_cli_wires_exa_key_to_researcher(monkeypatch: Any) -> None:
    captured: dict[str, object] = {}

    def _fake_llm(settings: Settings) -> object:
        return object()

    def _fake_build_orchestrator(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "deep_apartment_finder.main.build_chat_model_with_fallback", _fake_llm
    )
    monkeypatch.setattr("deep_apartment_finder.main.build_orchestrator", _fake_build_orchestrator)

    ctx = RunContext(
        settings=Settings(groq_api_key="test", exa_api_key="exa-test"),
        pool=object(),  # type: ignore[arg-type]
        scraper=FakeScraper(),
        idealista_scraper=None,
        repo=InMemoryApartmentRepository(),
        dangerous_repo=InMemoryDangerousNeighborhoodRepository(),
        ranking_repo=InMemoryRankingRepository(),
    )

    build_orchestrator_for_cli(ctx)

    assert captured["researcher_search_backend"] is not None


class _RecordingBackend:
    async def awrite(self, path: str, content: str) -> object:
        return object()


@pytest.mark.asyncio
async def test_deterministic_ranker_skips_apartments_failing_hard_filters() -> None:
    repo = InMemoryApartmentRepository()
    dangerous_repo = InMemoryDangerousNeighborhoodRepository()
    ranking_repo = InMemoryRankingRepository()
    await dangerous_repo.upsert_many(
        [DangerousNeighborhood("Delicias", 41.6517, -0.9088, 600)],
        source="test",
    )
    await repo.upsert(make_apartment(external_id="pass", price_eur=1000.0))
    await repo.upsert(make_apartment(external_id="fail", price_eur=1400.0))

    steps = _DeterministicSteps(
        repo=repo,
        dangerous_repo=dangerous_repo,
        ranking_repo=ranking_repo,
        notifier=None,
        backend=_RecordingBackend(),
        from_address=None,
        to_address=None,
        weight_distance=0.5,
        weight_pet_policy=0.3,
        weight_furnished=0.2,
        max_distance_m=2000.0,
        top_n=5,
    )

    result = await steps.run()

    assert result["apartments_scored"] == 1
    assert result["ranking"]["scores_written"] == 3
