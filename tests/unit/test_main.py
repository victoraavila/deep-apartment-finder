"""Composition root tests."""

from __future__ import annotations

import inspect
from typing import Any

from deep_apartment_finder.config import Settings
from deep_apartment_finder.main import RunContext, build_orchestrator_for_cli
from tests._fakes import FakeScraper, InMemoryApartmentRepository


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
        repo=InMemoryApartmentRepository(),
    )

    result = build_orchestrator_for_cli(ctx)

    assert result is sentinel_agent
    assert not inspect.isawaitable(result)
