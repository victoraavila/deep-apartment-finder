"""`count_dangerous_neighborhoods` — read-only tool the orchestrator
uses to decide whether to invoke the `researcher` subagent.

Returns `{"count": <int>}`.
"""

from __future__ import annotations

import json

from langchain_core.tools import BaseTool, tool

from deep_apartment_finder.ports.dangerous_neighborhood_repository import (
    DangerousNeighborhoodRepository,
)


def make_count_dangerous_neighborhoods_tool(
    repo: DangerousNeighborhoodRepository,
) -> BaseTool:
    @tool
    async def count_dangerous_neighborhoods() -> str:
        """Return the number of rows in `dangerous_neighborhoods`."""
        n = await repo.count()
        return json.dumps({"count": n})

    return count_dangerous_neighborhoods


__all__ = ["make_count_dangerous_neighborhoods_tool"]
