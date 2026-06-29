"""LLM-facing listing payload helpers.

The repository-facing `Apartment.to_ingest_dict()` intentionally carries
`raw_json` for persistence/debugging. Scraper tool outputs are fed back into
the model on every turn, so they need a slimmer shape: only the normalized
fields the subagent uses for soft extraction and ingest.
"""

from __future__ import annotations

from typing import Any

from deep_apartment_finder.domain.apartment import Apartment

AGENT_LISTING_FIELDS: tuple[str, ...] = (
    "source",
    "external_id",
    "url",
    "title",
    "price_eur",
    "rooms",
    "bathrooms",
    "size_m2",
    "address",
    "lat",
    "lng",
    "description",
    "pet_policy",
    "furnished",
)

_NUMERIC_FIELDS = {"price_eur", "size_m2", "lat", "lng"}


def apartment_to_agent_payload(apartment: Apartment) -> dict[str, Any]:
    """Return the model-visible listing payload for `fetch_listing`.

    This excludes raw scraper payloads (`raw_json` / `raw`) and timestamps.
    Numeric fields are converted from the JSON-safe strings produced by
    `Apartment.to_ingest_dict()` back to floats for compact, natural tool
    output.
    """
    data = apartment.to_ingest_dict()
    payload = {field: data.get(field) for field in AGENT_LISTING_FIELDS}
    for field in _NUMERIC_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value:
            try:
                payload[field] = float(value)
            except ValueError:
                pass
    return payload


__all__ = ["AGENT_LISTING_FIELDS", "apartment_to_agent_payload"]
