"""Pure parsers for Fotocasa's JSON search/ads API response.

The API returns a base64-encoded JSON blob in some clients and plain
JSON in others. We accept both shapes transparently. Per-item fields
are normalised into our domain `ListingCard` and `Apartment` types.

The parsers do not touch the network. The scraper fetches; the parsers
shape. They are pure functions of `(item_dict) -> dataclass`.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any, cast

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.source import Source
from deep_apartment_finder.ports.scraper import ListingCard

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response-level helpers
# ---------------------------------------------------------------------------


def decode_response_body(raw: str | bytes) -> dict[str, Any]:
    """Parse a `/v1/search/ads` response body.

    Fotocasa's gateway sometimes returns a base64-encoded JSON blob and
    sometimes plain JSON (it appears to depend on the transport, the
    client, and the day of the week). We try plain-JSON first; if that
    fails we try base64; if that also fails we raise.

    The decoded dict has top-level keys:
    `totalItems`, `page`, `items[]`, `locationFacets`, `purchaseTypeFacets`,
    `containerCombinedLocation`, `urlLocationSegments`, `adjacentAds`,
    `superTopAd`.
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            return _loads_dict(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raw = base64.b64decode(bytes(raw)).decode("utf-8")
            return _loads_dict(raw)

    text = raw.strip()
    if not text:
        raise ValueError("empty response body")
    try:
        return _loads_dict(text)
    except json.JSONDecodeError:
        pass
    # Try base64. We do not strip whitespace because Fotocasa's blob is
    # one long unbroken string; if there is whitespace it is JSON.
    try:
        decoded = base64.b64decode(text, validate=True).decode("utf-8")
        return _loads_dict(decoded)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"could not parse response as JSON or base64: {exc}") from exc


def _loads_dict(text: str) -> dict[str, Any]:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("response body is not a JSON object")
    return cast(dict[str, Any], data)


# ---------------------------------------------------------------------------
# Per-item -> ListingCard
# ---------------------------------------------------------------------------


def item_to_card(item: dict[str, Any], *, base_url: str) -> ListingCard | None:
    """Build a `ListingCard` from one item in the search response.

    Returns `None` if the item doesn't carry a `propertyId` (the
    dedupe key) — those are non-listing rows the API sneaks in
    (e.g. `superTopAd`, ad placeholders).
    """
    prop_id = item.get("propertyId") or item.get("id")
    if not prop_id:
        return None
    prop_id = str(prop_id)
    transaction = item.get("transaction") or {}
    price = transaction.get("price")
    try:
        price_f = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_f = None

    url = _detail_url_from_item(item, base_url=base_url)
    title = _title_from_item(item)
    return ListingCard(
        external_id=prop_id,
        url=url,
        title=title,
        price_eur=price_f,
        raw=item,
    )


# ---------------------------------------------------------------------------
# Per-item -> Apartment
# ---------------------------------------------------------------------------


def item_to_apartment(item: dict[str, Any], *, base_url: str) -> Apartment | None:
    """Build a full `Apartment` from one item in the search response.

    The search response is rich enough that we don't need a separate
    detail-page fetch: rooms, baths, surface, floor, full description,
    address (when visible), geo, agency, photos, features, uris — all
    present in the search payload. The `Apartment.from_raw_dict` is
    the canonical entry point that coerces strings/ints/floats to
    Decimals safely.
    """
    prop_id = item.get("propertyId") or item.get("id")
    if not prop_id:
        return None
    prop_id = str(prop_id)

    location = item.get("location") or {}
    try:
        lat = float(location["latitude"]) if location.get("latitude") else None
    except (TypeError, ValueError):
        lat = None
    try:
        lng = float(location["longitude"]) if location.get("longitude") else None
    except (TypeError, ValueError):
        lng = None

    transaction = item.get("transaction") or {}
    price = transaction.get("price")
    try:
        price_f = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_f = None

    address = _address_from_item(item, location)
    title = _title_from_item(item)

    return Apartment.from_raw_dict(
        Source.FOTOCASA,
        prop_id,
        _detail_url_from_item(item, base_url=base_url),
        {
            "title": title,
            "price_eur": price_f,
            "rooms": item.get("rooms"),
            "bathrooms": item.get("baths"),
            "size_m2": item.get("surface"),
            "address": address,
            "lat": lat,
            "lng": lng,
            "description": item.get("description"),
            "raw": item,
        },
    )


# ---------------------------------------------------------------------------
# Internal field helpers
# ---------------------------------------------------------------------------


def _detail_url_from_item(item: dict[str, Any], *, base_url: str) -> str:
    """Pick the canonical detail-page URL out of an item's `uris`."""
    for u in item.get("uris", []) or []:
        if isinstance(u, dict) and u.get("language") == "es_ES":
            value = u.get("value")
            if isinstance(value, str) and value.startswith("/"):
                return f"{base_url}{value}"
    pid = item.get("propertyId") or item.get("id", "")
    return f"{base_url}/es/alquiler/vivienda/{pid}/d"


def _title_from_item(item: dict[str, Any]) -> str | None:
    """Apartment title.

    The API doesn't have a single "title" field on items — it has the
    `uris` slug ("/es/alquiler/vivienda/<city>/<features>/<id>/d")
    which is human-readable. We prefer the slug as a title because it
    names the city and the key features in plain Spanish.
    """
    for u in item.get("uris", []) or []:
        if isinstance(u, dict) and u.get("language") == "es_ES":
            value = u.get("value")
            if isinstance(value, str):
                # /es/alquiler/vivienda/zaragoza-capital/<features>/<id>/d
                # -> "Piso en zaragoza-capital: <features>"
                parts = [p for p in value.split("/") if p]
                if len(parts) >= 4:
                    city = parts[3]
                    features = parts[4] if len(parts) > 4 else ""
                    if features:
                        return f"Piso en {city}: {features.replace('-', ', ')}"
                    return f"Piso en {city}"
    return None


def _address_from_item(item: dict[str, Any], location: dict[str, Any]) -> str | None:
    """Compose an address from the location hierarchy.

    `addressVisibilityMode` is a Fotocasa-specific enum:
    - "1": street visible
    - "2": street hidden, neighbourhood visible
    - "3": only zipCode + city

    We only emit the street when the API says it's visible; otherwise
    we fall back to neighbourhood / city / zip.
    """
    visibility = item.get("addressVisibilityMode")
    if visibility == "1":
        street = item.get("street")
        number = item.get("number")
        zip_code = item.get("zipCode")
        parts = [p for p in (street, number) if p]
        street_str = " ".join(parts) if parts else None
        if street_str and zip_code:
            return f"{street_str}, {zip_code} {location.get('level5Name', '')}".strip(", ")
        if street_str:
            return street_str
    zip_code = item.get("zipCode")
    neighbourhood = location.get("level8Name") or location.get("level7Name")
    city = location.get("level5Name") or location.get("level4Name")
    parts = [p for p in (neighbourhood, city, zip_code) if p]
    return ", ".join(str(p) for p in parts) if parts else None
