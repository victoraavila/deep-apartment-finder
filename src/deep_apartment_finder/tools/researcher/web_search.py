"""`web_search` tool — thin wrapper around the configured search backend.

Sprint 2 ships a backend-agnostic Protocol. The composition root
binds the actual backend; in production we use Exa (already a
candidate for S3, free tier). Tests can bind a fake.

The tool returns a list of `{title, url, snippet}` records. The
LLM uses them to propose neighborhoods.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from langchain_core.tools import BaseTool, tool


@runtime_checkable
class SearchBackend(Protocol):
    async def search(self, query: str, *, num_results: int = 8) -> list[dict[str, Any]]:
        """Return `[{title, url, snippet}, ...]`."""
        ...


class ExaSearchBackend:
    """Default `SearchBackend` using the Exa web search MCP-style API.

    We use Exa's `search` endpoint via httpx (no extra SDK). The
    `EXA_API_KEY` env var must be set. If unset, the backend raises
    a clear error at first use (not at import time) so the
    composition root can decide what to do.

    If we want to swap to Tavily / DuckDuckGo later, the port is
    identical.
    """

    _URL = "https://api.exa.ai/search"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def search(self, query: str, *, num_results: int = 8) -> list[dict[str, Any]]:
        import os

        import httpx

        api_key = self._api_key or os.environ.get("EXA_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "EXA_API_KEY is not set; the researcher's web_search tool "
                "needs it. Set it in .env or wire a different SearchBackend."
            )
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                self._URL,
                headers={"x-api-key": api_key, "Content-Type": "application/json"},
                json={"query": query, "numResults": num_results},
            )
            response.raise_for_status()
            data = response.json()
        results = data.get("results") or []
        out: list[dict[str, Any]] = []
        for r in results:
            out.append(
                {
                    "title": r.get("title") or "",
                    "url": r.get("url") or "",
                    "snippet": (r.get("text") or r.get("snippet") or "")[:600],
                }
            )
        return out


def make_web_search_tool(backend: SearchBackend | None = None) -> BaseTool:
    """Build the `web_search` tool. The default backend is `ExaSearchBackend`."""

    @tool
    async def web_search(query: str, num_results: int = 8) -> str:
        """Search the web for `query`. Returns a JSON object:
        `{"query", "count", "results": [...]}`. Each result has
        `title`, `url`, `snippet`. Returns `{"status": "error", ...}`
        when the search backend is not configured."""
        if backend is None:
            # Lazy default: read the env at call time so the tool
            # works even when the backend wasn't wired at startup.
            import os

            key = os.environ.get("EXA_API_KEY", "")
            if not key:
                import json

                return json.dumps(
                    {
                        "status": "error",
                        "message": (
                            "EXA_API_KEY is not set; the researcher subagent "
                            "needs it. Set it in .env or wire a different SearchBackend."
                        ),
                    }
                )
            active: SearchBackend = ExaSearchBackend(api_key=key)
        else:
            active = backend
        import json

        try:
            results = await active.search(query, num_results=num_results)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"status": "error", "message": str(exc)})

        return json.dumps({"query": query, "count": len(results), "results": results})

    return web_search


__all__ = ["ExaSearchBackend", "SearchBackend", "make_web_search_tool"]
