"""`save_snapshot` tool — Idealista variant.

Same contract as the Fotocasa one: write a debug snapshot to the
subagent's own subtree, forcing the prefix `/idealista_scraper/`
(ADR-005 boundary layer).
"""

from __future__ import annotations

import re

from deepagents.backends.protocol import BackendProtocol
from langchain_core.tools import BaseTool, tool

# Allowed prefix for this subagent. ADR-005 layer 2.
_ALLOWED_PREFIX = "/idealista_scraper/"
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def make_save_snapshot_tool(backend: BackendProtocol) -> BaseTool:
    """Build the `save_snapshot` tool.

    `backend` is the `BackendProtocol` instance shared with the agent
    graph (built by `filesystem.routes.build_backend`). It exposes
    `awrite(path, content) -> WriteResult`.
    """

    @tool
    async def save_snapshot(name: str, content: str) -> str:
        """Save a debug snapshot (raw HTML, raw JSON, etc) under
        `/idealista_scraper/raw/{name}`. Names are restricted to
        `[A-Za-z0-9._-]+`. Returns the path written."""
        if not _NAME_RE.match(name):
            return f"error: invalid name {name!r}"
        path = f"{_ALLOWED_PREFIX}raw/{name}"
        try:
            result = await backend.awrite(path, content)
            return f"ok: {result.path if hasattr(result, 'path') else path}"
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"

    return save_snapshot
