"""`ingest_apartment` tool.

The orchestrator's `fotocasa_scraper` subagent calls this to persist
a normalized listing. The tool returns a small JSON object that the
LLM can use to track progress: the resulting id on insert, or the
external_id on duplicate (acceptance criterion 3).
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool, tool

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.filters.hard import HardFilters
from deep_apartment_finder.domain.source import Source
from deep_apartment_finder.ports.apartment_repository import ApartmentRepository


def make_ingest_apartment_tool(repo: ApartmentRepository) -> BaseTool:
    """Build the `ingest_apartment` tool bound to a specific repository.

    The tool accepts a JSON-stringified apartment payload (the parser
    produces one) and persists it. It never raises on duplicate; it
    returns `{ "status": "duplicate", ... }` instead.
    """

    @tool
    async def ingest_apartment(
        payload: str,
        min_rooms: int | None = 2,
        min_bathrooms: int | None = 2,
        min_size_m2: float | None = 50.0,
        max_price_eur: float | None = 1200.0,
        city: str = "Zaragoza",
    ) -> str:
        """Persist a normalized apartment to the database.

        `payload` is a JSON string with the same shape as
        `Apartment.from_raw_dict`'s input: `title`, `price_eur`, `rooms`,
        `bathrooms`, `size_m2`, `address`, `lat`, `lng`, `description`,
        `pet_policy`, and `raw`. Required: `source`, `external_id`, `url`.

        Returns a JSON object:
            {"status": "inserted", "id": <int>}
            {"status": "duplicate", "external_id": <str>}
            {"status": "filtered", "external_id": <str>, "reason": <str>}
        """
        data: dict[str, Any] = json.loads(payload)
        try:
            source = Source(data["source"])
        except (KeyError, ValueError) as exc:
            return json.dumps({"status": "error", "message": f"invalid source: {exc}"})
        try:
            external_id = str(data["external_id"])
            url = str(data["url"])
        except KeyError as exc:
            return json.dumps({"status": "error", "message": f"missing field: {exc}"})

        apartment = Apartment.from_raw_dict(source, external_id, url, data)
        filters = HardFilters(
            city=city,
            min_rooms=min_rooms,
            min_bathrooms=min_bathrooms,
            min_size_m2=min_size_m2,
            max_price_eur=max_price_eur,
        )
        if not filters.passes(apartment):
            return json.dumps(
                {
                    "status": "filtered",
                    "external_id": apartment.external_id,
                    "reason": "failed Sprint 1 hard filters",
                }
            )
        result = await repo.upsert(apartment)
        if hasattr(result, "apartment_id"):
            return json.dumps({"status": "inserted", "id": result.apartment_id})
        return json.dumps({"status": "duplicate", "external_id": result.external_id})

    return ingest_apartment
