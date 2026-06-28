"""`save_snapshot` tool — writes a debug snapshot to the subagent's
own subtree, **forcing the prefix** (ADR-005 boundary layer).

The tool takes a name and content; it writes the file via the
`FilesystemBackend` so the snapshot ends up in the persistent
`StoreBackend` if the path is under `/fotocasa_scraper/`, or the
ephemeral `StateBackend` otherwise. Writes *outside* the subagent's
subtree are rejected.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.tools import BaseTool, tool

# Allowed prefix for this subagent. ADR-005 layer 2.
_ALLOWED_PREFIX = "/fotocasa_scraper/"
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def make_save_snapshot_tool(filesystem_backend_factory: Any) -> BaseTool:
    """Build the `save_snapshot` tool.

    `filesystem_backend_factory` is a callable `(runtime_config) -> Backend`
    — typically the same callable passed to `create_deep_agent(backend=...)`.
    The runtime config is the standard Deep Agents runtime config; the
    backend exposes `awrite(path, content) -> WriteResult`.
    """

    @tool
    async def save_snapshot(name: str, content: str) -> str:
        """Save a debug snapshot (raw HTML, raw JSON, etc) under
        `/fotocasa_scraper/raw/{name}`. Names are restricted to
        `[A-Za-z0-9._-]+`. Returns the path written."""
        if not _NAME_RE.match(name):
            return f"error: invalid name {name!r}"
        path = f"{_ALLOWED_PREFIX}raw/{name}"
        try:
            backend = filesystem_backend_factory({})
            result = await backend.awrite(path, content)
            return f"ok: {result.path if hasattr(result, 'path') else path}"
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"

    return save_snapshot
