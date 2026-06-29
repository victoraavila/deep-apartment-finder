"""`upsert_neighborhoods` tool — persists a researcher's proposals.

Input: a JSON array of proposed neighborhoods. The tool:
1. Validates each row (name, center_lat, center_lng, radius_m).
2. Saves a snapshot to `/researcher/dangerous_neighborhoods/` (forced
   prefix, ADR-005 layer 2) so the human can inspect the proposed
   list before the second `run`.
3. Calls `repo.upsert_many(rows, source=...)` to write to Postgres.

The tool refuses to write if the list is empty (it logs and returns
an error). The orchestrator treats an empty result as "researcher
failed to bootstrap" and stops the run (SPRINT2 first-run gate).
"""

from __future__ import annotations

import json
import re

from deepagents.backends.protocol import BackendProtocol
from langchain_core.tools import BaseTool, tool

from deep_apartment_finder.domain.geo import DangerousNeighborhood
from deep_apartment_finder.ports.dangerous_neighborhood_repository import (
    DangerousNeighborhoodRepository,
)

_ALLOWED_PREFIX = "/researcher/"
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def make_upsert_neighborhoods_tool(
    repo: DangerousNeighborhoodRepository,
    *,
    backend: BackendProtocol,
) -> BaseTool:
    @tool
    async def upsert_neighborhoods(
        rows_json: str,
        source: str = "researcher:web",
        snapshot_name: str = "proposed.json",
    ) -> str:
        """Persist a JSON list of proposed dangerous neighborhoods.

        `rows_json` must be a JSON array; each element has
        `name`, `center_lat`, `center_lng`, `radius_m`. The tool
        saves a copy under `/researcher/dangerous_neighborhoods/`
        (forced prefix) and then writes to the database. Returns
        `{"status": "ok", "written": <int>}` on success.
        """
        try:
            data = json.loads(rows_json)
        except json.JSONDecodeError as exc:
            return json.dumps({"status": "error", "message": f"invalid json: {exc}"})

        if not isinstance(data, list):
            return json.dumps({"status": "error", "message": "rows_json must be a JSON array"})

        if not data:
            return json.dumps({"status": "error", "message": "rows_json is empty"})

        if not _NAME_RE.match(snapshot_name):
            return json.dumps(
                {"status": "error", "message": f"invalid snapshot_name: {snapshot_name!r}"}
            )

        proposed: list[DangerousNeighborhood] = []
        for r in data:
            try:
                proposed.append(
                    DangerousNeighborhood(
                        name=str(r["name"]).strip(),
                        center_lat=float(r["center_lat"]),
                        center_lng=float(r["center_lng"]),
                        radius_m=int(r["radius_m"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                return json.dumps(
                    {
                        "status": "error",
                        "message": f"invalid row {r!r}: {exc}",
                    }
                )

        # Save a snapshot before writing, so a human can inspect what
        # the researcher proposed.
        snapshot_path = f"{_ALLOWED_PREFIX}dangerous_neighborhoods/{snapshot_name}"
        try:
            await backend.awrite(
                snapshot_path,
                json.dumps({"source": source, "rows": data}, indent=2),
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"status": "error", "message": f"snapshot write failed: {exc}"})

        written = await repo.upsert_many(proposed, source=source)
        return json.dumps({"status": "ok", "written": written, "snapshot": snapshot_path})

    return upsert_neighborhoods


__all__ = ["make_upsert_neighborhoods_tool"]
