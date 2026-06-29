"""Tests for the `upsert_neighborhoods` and `count_neighborhoods`
tools. We exercise the tools' tool-arena glue (JSON parsing,
prefix-forced writes, repo delegation) without involving a real
LLM.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from deep_apartment_finder.domain.geo import DangerousNeighborhood
from deep_apartment_finder.tools.researcher.count_neighborhoods import (
    make_count_dangerous_neighborhoods_tool,
)
from deep_apartment_finder.tools.researcher.upsert_neighborhoods import (
    make_upsert_neighborhoods_tool,
)
from tests._fakes import InMemoryDangerousNeighborhoodRepository


class _RecordingBackend:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []

    async def awrite(self, path: str, content: str) -> Any:
        self.writes.append((path, content))

        class _R:
            def __init__(self_inner, p: str) -> None:
                self_inner.path = p

        return _R(path)


# --- count_dangerous_neighborhoods ----------------------------------------


@pytest.mark.asyncio
async def test_count_tool_returns_zero_on_empty_repo():
    repo = InMemoryDangerousNeighborhoodRepository()
    tool = make_count_dangerous_neighborhoods_tool(repo)
    out = await tool.arun({})
    data = json.loads(out)
    assert data == {"count": 0}


@pytest.mark.asyncio
async def test_count_tool_returns_existing_count():
    repo = InMemoryDangerousNeighborhoodRepository()
    await repo.upsert_many(
        [
            DangerousNeighborhood("A", 41.65, -0.88, 500),
            DangerousNeighborhood("B", 41.70, -0.90, 500),
        ],
        source="test",
    )
    tool = make_count_dangerous_neighborhoods_tool(repo)
    out = await tool.arun({})
    data = json.loads(out)
    assert data == {"count": 2}


# --- upsert_neighborhoods -------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_tool_writes_snapshot_and_db_rows():
    repo = InMemoryDangerousNeighborhoodRepository()
    backend = _RecordingBackend()
    tool = make_upsert_neighborhoods_tool(repo, backend=backend)  # type: ignore[arg-type]
    rows = json.dumps(
        [
            {"name": "Delicias", "center_lat": 41.6517, "center_lng": -0.9088, "radius_m": 600},
            {"name": "El Gancho", "center_lat": 41.654, "center_lng": -0.881, "radius_m": 400},
        ]
    )
    out = await tool.arun({"rows_json": rows})
    data = json.loads(out)
    assert data["status"] == "ok"
    assert data["written"] == 2
    assert data["snapshot"].startswith("/researcher/dangerous_neighborhoods/")
    assert await repo.count() == 2
    assert any(p.startswith("/researcher/") for p, _ in backend.writes)


@pytest.mark.asyncio
async def test_upsert_tool_rejects_empty_list():
    repo = InMemoryDangerousNeighborhoodRepository()
    backend = _RecordingBackend()
    tool = make_upsert_neighborhoods_tool(repo, backend=backend)  # type: ignore[arg-type]
    out = await tool.arun({"rows_json": "[]"})
    data = json.loads(out)
    assert data["status"] == "error"
    assert "empty" in data["message"]


@pytest.mark.asyncio
async def test_upsert_tool_rejects_invalid_json():
    repo = InMemoryDangerousNeighborhoodRepository()
    backend = _RecordingBackend()
    tool = make_upsert_neighborhoods_tool(repo, backend=backend)  # type: ignore[arg-type]
    out = await tool.arun({"rows_json": "not json"})
    data = json.loads(out)
    assert data["status"] == "error"


@pytest.mark.asyncio
async def test_upsert_tool_rejects_malformed_row():
    repo = InMemoryDangerousNeighborhoodRepository()
    backend = _RecordingBackend()
    tool = make_upsert_neighborhoods_tool(repo, backend=backend)  # type: ignore[arg-type]
    # Missing 'radius_m'
    rows = json.dumps([{"name": "X", "center_lat": 41.65, "center_lng": -0.88}])
    out = await tool.arun({"rows_json": rows})
    data = json.loads(out)
    assert data["status"] == "error"
    assert "invalid row" in data["message"]


@pytest.mark.asyncio
async def test_upsert_tool_rejects_invalid_snapshot_name():
    repo = InMemoryDangerousNeighborhoodRepository()
    backend = _RecordingBackend()
    tool = make_upsert_neighborhoods_tool(repo, backend=backend)  # type: ignore[arg-type]
    rows = json.dumps([{"name": "X", "center_lat": 41.65, "center_lng": -0.88, "radius_m": 100}])
    out = await tool.arun(
        {"rows_json": rows, "snapshot_name": "../escape.json"}
    )
    data = json.loads(out)
    assert data["status"] == "error"
    assert "snapshot_name" in data["message"]
