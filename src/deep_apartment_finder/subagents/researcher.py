"""Researcher subagent — bootstraps `dangerous_neighborhoods` on first run.

The first time the orchestrator runs, this subagent:
1. Performs a web search for "dangerous neighborhoods in Zaragoza" (or
   the configured query).
2. Parses the result into a list of `DangerousNeighborhood` records.
3. Persists them via the `DangerousNeighborhoodRepository.upsert_many`
   tool.

On subsequent runs the orchestrator checks `dangerous_neighborhoods`
first; if it has rows, the researcher subagent is not invoked at all
(no-op path).

The subagent's tools:
- `web_search` — calls a configured web search backend.
- `upsert_neighborhoods` — writes a JSON payload of proposed
  neighborhoods to the database.
- `save_snapshot` — saves the proposed payload to
  `/researcher/dangerous_neighborhoods/` for human review.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from deep_apartment_finder.domain.geo import DangerousNeighborhood
from deep_apartment_finder.ports.dangerous_neighborhood_repository import (
    DangerousNeighborhoodRepository,
)
from deep_apartment_finder.tools.researcher.upsert_neighborhoods import (
    make_upsert_neighborhoods_tool,
)
from deep_apartment_finder.tools.researcher.web_search import (
    SearchBackend,
    make_web_search_tool,
)

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt() -> str:
    return (_PROMPTS_DIR / "researcher.md").read_text(encoding="utf-8")


def _default_search_query() -> str:
    return os.environ.get(
        "RESEARCHER_QUERY",
        "barrios mas peligrosos Zaragoza statistics Spain safety",
    )


def build_researcher_subagent(
    *,
    repo: DangerousNeighborhoodRepository,
    backend: Any,
    search_backend: SearchBackend | None = None,
) -> dict[str, Any]:
    """Build the `researcher` subagent descriptor.

    The orchestrator decides whether to invoke this subagent at all;
    the descriptor's `description` is what the orchestrator sees when
    it considers the option.
    """
    tools: list[BaseTool] = [
        make_web_search_tool(search_backend),
        make_upsert_neighborhoods_tool(repo, backend=backend),
    ]
    return {
        "name": "researcher",
        "description": (
            "Researches Zaragoza's 'dangerous' neighborhoods from public "
            "data and persists a constants table. Invoked only on the "
            "first run (or when the constants table is empty). Returns "
            "the count of neighborhoods written."
        ),
        "system_prompt": _load_prompt(),
        "tools": tools,
    }


__all__ = ["build_researcher_subagent", "_default_search_query", "DangerousNeighborhood"]
