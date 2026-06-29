"""Tests for the `web_search` tool's tool-glue layer (no real HTTP).

We inject a fake `SearchBackend` and assert the JSON output shape.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from deep_apartment_finder.tools.researcher.web_search import (
    make_web_search_tool,
)


class _FakeBackend:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self._results = results
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, *, num_results: int = 8) -> list[dict[str, Any]]:
        self.calls.append((query, num_results))
        return self._results


@pytest.mark.asyncio
async def test_web_search_returns_json_array_of_results():
    backend = _FakeBackend(
        [
            {"title": "A", "url": "https://a", "snippet": "snippet A"},
            {"title": "B", "url": "https://b", "snippet": "snippet B"},
        ]
    )
    tool = make_web_search_tool(backend=backend)  # type: ignore[arg-type]
    out = await tool.arun({"query": "dangerous neighborhoods Zaragoza", "num_results": 5})
    data = json.loads(out)
    assert data["query"] == "dangerous neighborhoods Zaragoza"
    assert data["count"] == 2
    assert data["results"][0]["title"] == "A"
    assert backend.calls == [("dangerous neighborhoods Zaragoza", 5)]


@pytest.mark.asyncio
async def test_web_search_returns_error_when_no_backend_and_no_key(monkeypatch: pytest.MonkeyPatch):
    """Without a backend and without EXA_API_KEY, the tool returns an error string."""
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    tool = make_web_search_tool()
    out = await tool.arun({"query": "x"})
    data = json.loads(out)
    assert "error" in out.lower() or data.get("status") == "error"
