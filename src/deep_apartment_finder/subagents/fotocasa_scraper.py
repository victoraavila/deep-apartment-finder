"""Subagent factories.

Each subagent is described as a dict (per the Deep Agents convention),
passed to `create_deep_agent(subagents=[...])`. The dict carries the
subagent's name, system prompt (loaded from disk), description (what
the orchestrator sees when deciding which subagent to call), and
tool set.

The orchestrator's `task` tool calls subagents by name. Adding a new
subagent is an additive change here + a new prompt file + a new
filesystem route in `filesystem/routes.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from langchain_core.tools import BaseTool

from deep_apartment_finder.ports.apartment_repository import ApartmentRepository
from deep_apartment_finder.ports.scraper import ScraperPort
from deep_apartment_finder.tools.fotocasa.fetch_listing import make_fetch_listing_tool
from deep_apartment_finder.tools.fotocasa.save_snapshot import make_save_snapshot_tool
from deep_apartment_finder.tools.fotocasa.search_listings import make_search_listings_tool
from deep_apartment_finder.tools.ingest import make_ingest_apartment_tool

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    """Load a subagent's system prompt from disk."""
    path = _PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8")


def build_fotocasa_scraper_subagent(
    *,
    scraper: ScraperPort,
    repo: ApartmentRepository,
    backend: BackendProtocol,
) -> dict[str, Any]:
    """Build the `fotocasa_scraper` subagent descriptor.

    Returns a dict in the shape Deep Agents expects:
    `{ name, description, system_prompt, tools }`.
    """
    tools: list[BaseTool] = [
        make_search_listings_tool(scraper),
        make_fetch_listing_tool(scraper),
        make_ingest_apartment_tool(repo),
        make_save_snapshot_tool(backend),
    ]
    return {
        "name": "fotocasa_scraper",
        "description": (
            "Scrapes Fotocasa for Zaragoza rentals, parses each listing, "
            "and persists new rows to the database. Returns a handoff "
            "summary with inserted/duplicate counts and any per-listing errors."
        ),
        "system_prompt": _load_prompt("fotocasa_scraper"),
        "tools": tools,
    }


__all__ = ["build_fotocasa_scraper_subagent"]
