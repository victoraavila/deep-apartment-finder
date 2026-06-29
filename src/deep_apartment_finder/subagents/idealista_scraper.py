"""`idealista_scraper` subagent factory.

Sprint 3 second scraper (Pillar E). The shape mirrors
`build_fotocasa_scraper_subagent` exactly: a dict in the
`{name, description, system_prompt, tools}` shape Deep Agents
expects, with the four tools the subagent needs
(`search_listings`, `fetch_listing`, `ingest_apartment`,
`save_snapshot`).

Both subagents are stateless and ephemeral. Persistence is in
Postgres. The orchestrator's `task` tool calls them by name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from langchain_core.tools import BaseTool

from deep_apartment_finder.ports.apartment_repository import ApartmentRepository
from deep_apartment_finder.ports.scraper import ScraperPort
from deep_apartment_finder.tools.idealista.fetch_listing import make_fetch_listing_tool
from deep_apartment_finder.tools.idealista.save_snapshot import make_save_snapshot_tool
from deep_apartment_finder.tools.idealista.search_listings import make_search_listings_tool
from deep_apartment_finder.tools.ingest import make_ingest_apartment_tool

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    """Load a subagent's system prompt from disk."""
    path = _PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8")


def build_idealista_scraper_subagent(
    *,
    scraper: ScraperPort,
    repo: ApartmentRepository,
    backend: BackendProtocol,
) -> dict[str, Any]:
    """Build the `idealista_scraper` subagent descriptor."""
    tools: list[BaseTool] = [
        make_search_listings_tool(scraper),
        make_fetch_listing_tool(scraper),
        make_ingest_apartment_tool(repo),
        make_save_snapshot_tool(backend),
    ]
    return {
        "name": "idealista_scraper",
        "description": (
            "Scrapes Idealista for Zaragoza rentals, parses each listing, "
            "and persists new rows to the database. Returns a handoff "
            "summary with inserted/duplicate counts and any per-listing errors. "
            "Used in addition to (not in place of) fotocasa_scraper — the "
            "two scrapers run in a single run and their results are "
            "cross-portal-deduplicated downstream."
        ),
        "system_prompt": _load_prompt("idealista_scraper"),
        "tools": tools,
    }


__all__ = ["build_idealista_scraper_subagent"]
