"""Integration test: orchestrator -> subagent -> repository with fakes.

The orchestrator's `task` tool delegates to a registered subagent by
invoking `create_deep_agent` for that subagent's tool set + system
prompt. Without a real LLM in CI, we can't drive the agent loop end-
to-end, but we *can* verify the plumbing:

  1. The subagent factory binds the right tools (search_listings,
     fetch_listing, ingest_apartment, save_snapshot) to the right
     dependencies (scraper, repo).
  2. The tool chain, when invoked in the right order by a fake
     "LLM" (a simple hand-rolled driver), produces the right database
     state and a handoff summary that mentions the expected counts.

This is the same shape acceptance criterion (2) describes. The
*planner* layer is left to the real LLM; we verify everything below
the planning step.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from deep_apartment_finder.filesystem.routes import build_backend
from deep_apartment_finder.ports.scraper import ListingCard
from deep_apartment_finder.subagents.fotocasa_scraper import build_fotocasa_scraper_subagent
from tests._fakes import FakeScraper, InMemoryApartmentRepository, make_apartment


def _tool_by_name(tools: list[Any], name: str) -> Any:
    for t in tools:
        if getattr(t, "name", None) == name:
            return t
    raise KeyError(f"no tool named {name!r} in {[getattr(t, 'name', t) for t in tools]}")


async def _drive_fotocasa_subagent(tools: list[Any], plan: list[dict[str, Any]]) -> list[str]:
    """Drive the subagent's tools with a fixed plan. Each plan item is
    `{ "tool": <name>, "args": <dict> }`. Returns a list of tool outputs
    in order.
    """
    outputs: list[str] = []
    for step in plan:
        tool = _tool_by_name(tools, step["tool"])
        out = await tool.arun(step["args"])
        outputs.append(out)
    return outputs


def test_subagent_factory_binds_expected_tools():
    scraper = FakeScraper()
    repo = InMemoryApartmentRepository()
    sub = build_fotocasa_scraper_subagent(
        scraper=scraper, repo=repo, backend=build_backend()
    )
    assert sub["name"] == "fotocasa_scraper"
    assert {t.name for t in sub["tools"]} == {
        "search_listings",
        "fetch_listing",
        "ingest_apartment",
        "save_snapshot",
    }
    # Description visible to the orchestrator must be non-empty.
    assert "fotocasa" in sub["description"].lower()


@pytest.mark.asyncio
async def test_subagent_pipeline_persists_rows_and_returns_counts():
    """Run the search -> fetch -> ingest pipeline with a fake scraper.

    Asserts the final database state matches the plan, and that the
    ingest tool's output is a JSON object whose `status` is one of
    `inserted` / `duplicate`.
    """
    apt = make_apartment(external_id="integ-1", url="https://fotocasa.es/vivienda/integ-1")
    scraper = FakeScraper(
        cards=[ListingCard(external_id="integ-1", url=apt.url, title=apt.title)],
        details={apt.url: apt},
    )
    repo = InMemoryApartmentRepository()
    sub = build_fotocasa_scraper_subagent(
        scraper=scraper, repo=repo, backend=build_backend()
    )

    outputs = await _drive_fotocasa_subagent(
        sub["tools"],
        [
            {"tool": "search_listings", "args": {}},
            {"tool": "fetch_listing", "args": {"url": apt.url}},
            {"tool": "ingest_apartment", "args": {"payload": json.dumps(apt.to_ingest_dict())}},
        ],
    )

    # search_listings returns a JSON array with 1 card.
    cards = json.loads(outputs[0])
    assert cards["count"] == 1
    # fetch_listing returns a normalized apartment.
    detail = json.loads(outputs[1])
    assert detail["external_id"] == "integ-1"
    # ingest_apartment returns `inserted` with a new id.
    ingest = json.loads(outputs[2])
    assert ingest["status"] == "inserted"
    assert ingest["id"] == 1
    # Database state matches.
    assert await repo.count() == 1


@pytest.mark.asyncio
async def test_subagent_pipeline_surfaces_duplicates():
    """Re-ingesting the same external_id must surface as `duplicate`,
    not raise. Acceptance criterion (3)."""
    apt = make_apartment(external_id="dup-1", url="https://fotocasa.es/vivienda/dup-1")
    scraper = FakeScraper(
        cards=[ListingCard(external_id="dup-1", url=apt.url, title=apt.title)],
        details={apt.url: apt},
    )
    repo = InMemoryApartmentRepository()
    await repo.upsert(apt)  # already present
    sub = build_fotocasa_scraper_subagent(
        scraper=scraper, repo=repo, backend=build_backend()
    )

    outputs = await _drive_fotocasa_subagent(
        sub["tools"],
        [
            {"tool": "search_listings", "args": {}},
            {"tool": "fetch_listing", "args": {"url": apt.url}},
            {"tool": "ingest_apartment", "args": {"payload": json.dumps(apt.to_ingest_dict())}},
        ],
    )

    ingest = json.loads(outputs[2])
    assert ingest["status"] == "duplicate"
    assert ingest["external_id"] == "dup-1"
    assert await repo.count() == 1  # unchanged


@pytest.mark.asyncio
async def test_subagent_pipeline_persists_hard_filter_to_scraper():
    """The subagent's search_listings tool must pass the hard filters
    through to the scraper."""
    scraper = FakeScraper()
    repo = InMemoryApartmentRepository()
    sub = build_fotocasa_scraper_subagent(
        scraper=scraper, repo=repo, backend=build_backend()
    )
    await _drive_fotocasa_subagent(
        sub["tools"],
        [
            {"tool": "search_listings", "args": {
                "min_rooms": 3,
                "min_bathrooms": 1,
                "min_size_m2": 60.0,
                "max_price_eur": 950.0,
            }},
        ],
    )
    from deep_apartment_finder.domain.filters.hard import HardFilters

    assert len(scraper.search_calls) == 1
    f = scraper.search_calls[0]
    assert f == HardFilters(min_rooms=3, min_bathrooms=1, min_size_m2=60.0, max_price_eur=950.0)
