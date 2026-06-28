"""Orchestrator — the agent the user invokes from the CLI.

Built with `create_deep_agent(...)`. Owns:
- the reasoning LLM (with the opencode-go fallback already wrapped in),
- a single subagent (`fotocasa_scraper`),
- a `CompositeBackend` that routes `/fotocasa_scraper/` and
  `/orchestrator/` to the persistent store and everything else to
  ephemeral state.

The orchestrator does not own a repository or a scraper directly. The
*subagent* does. The orchestrator is a planner and a summarizer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from langchain_core.language_models import BaseChatModel

from deep_apartment_finder.filesystem.routes import build_backend
from deep_apartment_finder.ports.apartment_repository import ApartmentRepository
from deep_apartment_finder.ports.scraper import ScraperPort
from deep_apartment_finder.subagents.fotocasa_scraper import build_fotocasa_scraper_subagent

_PROMPTS_DIR = Path(__file__).parent.parent / "subagents" / "prompts"


def _load_orchestrator_prompt() -> str:
    return (_PROMPTS_DIR / "orchestrator.md").read_text(encoding="utf-8")


def build_orchestrator(
    *,
    llm: BaseChatModel,
    scraper: ScraperPort,
    repo: ApartmentRepository,
) -> Any:
    """Build the compiled orchestrator graph.

    Returns a `CompiledStateGraph` from LangGraph. The caller invokes
    `agent.invoke({...})` to run it.
    """
    backend = build_backend()
    subagent = build_fotocasa_scraper_subagent(
        scraper=scraper,
        repo=repo,
        backend=backend,
    )
    return create_deep_agent(
        model=llm,
        tools=[],  # orchestrator only delegates; tools live on subagents
        system_prompt=_load_orchestrator_prompt(),
        subagents=[subagent],  # type: ignore[list-item]
        backend=backend,
        # No interrupt_on: this is an automated daily run, not a HITL
        # workflow. Sprint 2 may add approval for notifications.
    )
