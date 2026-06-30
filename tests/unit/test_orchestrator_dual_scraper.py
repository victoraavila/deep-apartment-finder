"""Tests for the orchestrator's subagent registration.

The orchestrator must register BOTH the Fotocasa and the Idealista
scraper subagents in a single build (when an Idealista scraper is
provided), and only the Fotocasa one when it isn't. The tests
exercise the public `build_orchestrator` builder with fake adapters
for every external I/O and assert the subagent descriptors are in
the expected shape.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel

from deep_apartment_finder.agent.orchestrator import build_orchestrator
from tests._fakes import (
    FakeScraper,
    InMemoryApartmentRepository,
    InMemoryDangerousNeighborhoodRepository,
    InMemoryRankingRepository,
)


class _NoopChatModel(BaseChatModel):
    """Minimal stand-in for a `BaseChatModel` so the builder can run.

    Sprint 4: the orchestrator's `build_orchestrator` now passes
    `llm` to `create_sub_agent(...)` so the parallel
    `run_scrapers` tool can compile its subagent graphs at build
    time. `create_sub_agent` calls `resolve_model(...)` which
    takes the early-return path when the argument is already a
    `BaseChatModel` instance — so the test fakes must subclass
    `BaseChatModel` (the prior plain Python class failed the
    `isinstance` check and the build crashed).

    `_generate` and `_agenerate` are abstract; we provide
    minimal no-op implementations because the test patches
    `create_deep_agent` and never actually invokes the model.
    """

    @property
    def _llm_type(self) -> str:
        return "noop-test"

    def _generate(self, messages: Any, stop: Any = None, **kwargs: Any) -> Any:
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(generations=[ChatGeneration(message=None)])

    async def _agenerate(self, messages: Any, stop: Any = None, **kwargs: Any) -> Any:
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(generations=[ChatGeneration(message=None)])

    def bind_tools(self, *args: Any, **kwargs: Any) -> object:
        return self

    def with_fallbacks(self, *args: Any, **kwargs: Any) -> object:
        return self


def _patch_deep_agent_builder(monkeypatch: Any) -> dict[str, object]:
    """Patch `create_deep_agent` so we can inspect the subagents it received."""
    captured: dict[str, object] = {}

    def _fake_create_deep_agent(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "deep_apartment_finder.agent.orchestrator.create_deep_agent",
        _fake_create_deep_agent,
    )
    return captured


def _repos() -> tuple[
    InMemoryApartmentRepository,
    InMemoryDangerousNeighborhoodRepository,
    InMemoryRankingRepository,
]:
    return (
        InMemoryApartmentRepository(),
        InMemoryDangerousNeighborhoodRepository(),
        InMemoryRankingRepository(),
    )


def test_build_orchestrator_registers_fotocasa_only_when_idealista_is_none(
    monkeypatch: Any,
) -> None:
    """Sprint 1/2 behavior: only one scraper subagent."""
    repo, dangerous_repo, ranking_repo = _repos()
    captured = _patch_deep_agent_builder(monkeypatch)

    build_orchestrator(
        llm=_NoopChatModel(),  # type: ignore[arg-type]
        fotocasa_scraper=FakeScraper(),
        idealista_scraper=None,
        repo=repo,
        dangerous_repo=dangerous_repo,
        ranking_repo=ranking_repo,
        notifier=None,
    )

    subagents = captured.get("subagents") or []
    names = [s["name"] for s in subagents]
    assert "fotocasa_scraper" in names
    assert "idealista_scraper" not in names


def test_build_orchestrator_registers_both_scrapers_when_idealista_provided(
    monkeypatch: Any,
) -> None:
    """Sprint 3 behavior: both scraper subagents are registered."""
    repo, dangerous_repo, ranking_repo = _repos()
    captured = _patch_deep_agent_builder(monkeypatch)

    build_orchestrator(
        llm=_NoopChatModel(),  # type: ignore[arg-type]
        fotocasa_scraper=FakeScraper(),
        idealista_scraper=FakeScraper(),
        repo=repo,
        dangerous_repo=dangerous_repo,
        ranking_repo=ranking_repo,
        notifier=None,
    )

    subagents = captured.get("subagents") or []
    names = [s["name"] for s in subagents]
    assert "fotocasa_scraper" in names
    assert "idealista_scraper" in names


def test_idealista_scraper_subagent_carries_correct_prompt_and_tool_set(
    monkeypatch: Any,
) -> None:
    """The idealista subagent's prompt identifies it, and its tool set
    has exactly the four tools: search, fetch, ingest, save."""
    repo, dangerous_repo, ranking_repo = _repos()
    captured = _patch_deep_agent_builder(monkeypatch)

    build_orchestrator(
        llm=_NoopChatModel(),  # type: ignore[arg-type]
        fotocasa_scraper=FakeScraper(),
        idealista_scraper=FakeScraper(),
        repo=repo,
        dangerous_repo=dangerous_repo,
        ranking_repo=ranking_repo,
        notifier=None,
    )

    subagents = captured.get("subagents") or []
    idealista = next(s for s in subagents if s["name"] == "idealista_scraper")

    # Prompt identifies the subagent.
    assert idealista["system_prompt"].startswith("# idealista_scraper")
    # Tool set is exactly search + fetch + ingest + save.
    tool_names = sorted(t.name for t in idealista["tools"])
    assert tool_names == [
        "fetch_listing",
        "ingest_apartment",
        "save_snapshot",
        "search_listings",
    ]
