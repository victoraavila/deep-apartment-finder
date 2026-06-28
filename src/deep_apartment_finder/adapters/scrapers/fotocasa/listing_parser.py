"""HTML/JSON-LD parsers for Fotocasa search results and detail pages.

The parsers are pure functions: they take a string of HTML and the
selector set, and return dataclasses. They do no I/O. The scraper
layer is responsible for fetching the HTML and choosing between SSR
parsing and CSR / playwright fallback.

Robustness rules:
- Tolerate missing fields (return None, not raise).
- Prefer the JSON-LD blob when present; fall back to CSS selectors.
- For CSR pages, look for the next.js bootstrap (`__NEXT_DATA__`) and
  dig the listing out of it.
- `parse_search_page` yields a `ListingCard` per detected result;
  the dedup happens later in the repository.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.source import Source
from deep_apartment_finder.ports.scraper import ListingCard

_PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*|\d+)(?:[.,](\d{2}))?")


def _normalize_price(raw: str | None) -> float | None:
    if not raw:
        return None
    m = _PRICE_RE.search(raw)
    if not m:
        return None
    whole = m.group(1).replace(".", "").replace(",", "")
    cents = m.group(2)
    try:
        if cents:
            return float(f"{whole}.{cents}")
        return float(whole)
    except ValueError:
        return None


def _parse_float(s: str | None) -> float | None:
    if not s:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _parse_int(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r"\d+", s)
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None


def _text(node: Any) -> str | None:
    if node is None:
        return None
    t = node.text(separator=" ", strip=True)
    return t or None


def _href(node: Any) -> str | None:
    if node is None:
        return None
    attrs = node.attributes
    return attrs.get("href") if attrs else None


def _jsonld_blobs(html: str) -> Iterable[dict[str, Any]]:
    """Yield every JSON-LD object in the page, in order."""
    parser = HTMLParser(html)
    for tag in parser.css("script[type='application/ld+json']"):
        text = tag.text()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    yield item
        elif isinstance(data, dict):
            yield data


def _next_data_blob(html: str) -> dict[str, Any] | None:
    """Extract the Next.js bootstrap, when present (CSR pages)."""
    parser = HTMLParser(html)
    for tag in parser.css("script#__NEXT_DATA__"):
        text = tag.text()
        if not text:
            continue
        try:
            return json.loads(text)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            continue
    return None


def _is_residence(item: dict[str, Any]) -> bool:
    """Is this JSON-LD item a residence / apartment / single-family residence?"""
    t = item.get("@type")
    if isinstance(t, list):
        return any(_is_residence({"@type": x}) for x in t if isinstance(x, str))
    if not isinstance(t, str):
        return False
    return t in {
        "Apartment",
        "House",
        "SingleFamilyResidence",
        "Residence",
        "Product",
        "Accommodation",
    }


def _extract_rooms(text: str) -> int | None:
    m = re.search(
        r"(\d+)\s*(?:hab(?:itaciones?)?|dorm(?:itorios?)?|rooms?|beds?|bedrooms?)",
        text,
        re.I,
    )
    return _parse_int(m.group(1)) if m else None


def _extract_bathrooms(text: str) -> int | None:
    m = re.search(r"(\d+)\s*(?:bañ(?:os?)?|banos?|bath(?:rooms?)?)", text, re.I)
    return _parse_int(m.group(1)) if m else None


def _extract_size_m2(text: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*m[²2]", text, re.I)
    return _parse_float(m.group(1)) if m else None


def _card_from_jsonld(item: dict[str, Any]) -> ListingCard | None:
    """Build a ListingCard from a JSON-LD blob, if enough info is present."""
    url = item.get("url") or item.get("@id")
    if not url:
        return None
    name = item.get("name")
    offers = item.get("offers") or {}
    price = offers.get("price") if isinstance(offers, dict) else None
    try:
        price_f = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_f = None
    # External id: use the path tail if it looks like one, else None.
    ext_id: str | None = None
    m = re.search(r"/(?:vivienda|inmueble)/([^/?#]+)", str(url))
    if m:
        ext_id = m.group(1)
    return ListingCard(
        external_id=ext_id or str(url),
        url=str(url),
        title=name,
        price_eur=price_f,
        raw=item,
    )


def _apartment_from_jsonld(
    item: dict[str, Any], *, url: str, external_id: str
) -> Apartment | None:
    """Build an `Apartment` from a JSON-LD blob, if it is a residence."""
    if not _is_residence(item):
        return None
    name = item.get("name")
    description = item.get("description")
    address_obj = item.get("address") or {}
    address = None
    if isinstance(address_obj, dict):
        parts = [
            address_obj.get("streetAddress"),
            address_obj.get("addressLocality"),
            address_obj.get("addressRegion"),
            address_obj.get("postalCode"),
        ]
        address = ", ".join(p for p in parts if p) or None
    elif isinstance(address_obj, str):
        address = address_obj
    geo = item.get("geo") or {}
    lat = geo.get("latitude")
    lng = geo.get("longitude")
    try:
        lat_f = float(lat) if lat is not None else None
    except (TypeError, ValueError):
        lat_f = None
    try:
        lng_f = float(lng) if lng is not None else None
    except (TypeError, ValueError):
        lng_f = None
    offers = item.get("offers") or {}
    price_raw = offers.get("price") if isinstance(offers, dict) else None
    try:
        price = float(price_raw) if price_raw is not None else None
    except (TypeError, ValueError):
        price = None
    # Number of rooms / bathrooms is not in the standard JSON-LD schema;
    # we extract from the name/description heuristically.
    text = " ".join(str(x) for x in (name, description) if x)
    rooms = _extract_rooms(text)
    bathrooms = _extract_bathrooms(text)
    size_m2 = _extract_size_m2(text)
    return Apartment.from_raw_dict(
        Source.FOTOCASA,
        external_id,
        url,
        {
            "title": name,
            "price_eur": price,
            "rooms": rooms,
            "bathrooms": bathrooms,
            "size_m2": size_m2,
            "address": address,
            "lat": lat_f,
            "lng": lng_f,
            "description": description,
            "raw": item,
        },
    )


def parse_search_page(html: str) -> list[ListingCard]:
    """Extract every listing card on a search results page.

    Strategy: JSON-LD first (stable, structured). Fall back to CSS for
    SSR pages that don't embed JSON-LD. Cards that look like
    duplicates (same `external_id`) are collapsed.
    """
    seen: dict[str, ListingCard] = {}

    # 1) JSON-LD
    for item in _jsonld_blobs(html):
        if not _is_residence(item):
            continue
        card = _card_from_jsonld(item)
        if card and card.external_id not in seen:
            seen[card.external_id] = card

    if seen:
        return list(seen.values())

    # 2) CSS fallback (SSR)
    return _css_search_cards(html)


def _css_search_cards(html: str) -> list[ListingCard]:
    """Best-effort SSR card extraction via CSS selectors."""
    from deep_apartment_finder.adapters.scrapers.fotocasa.selectors import card_selector

    sel = card_selector()
    parser = HTMLParser(html)
    seen: dict[str, ListingCard] = {}
    for container in parser.css(sel.container):
        link = container.css_first(sel.link)
        if link is None:
            continue
        href = _href(link)
        if not href:
            continue
        title = _text(container.css_first(sel.title))
        price_text = _text(container.css_first(sel.price))
        price = _normalize_price(price_text) if price_text else None
        m = re.search(r"/(?:vivienda|inmueble)/([^/?#]+)", href)
        ext_id = m.group(1) if m else href
        if ext_id in seen:
            continue
        seen[ext_id] = ListingCard(
            external_id=ext_id,
            url=href,
            title=title,
            price_eur=price,
        )
    return list(seen.values())


def parse_detail_page(html: str, *, url: str, external_id: str) -> Apartment | None:
    """Extract a full `Apartment` from a detail page.

    Order:
    1. JSON-LD (if it is a residence).
    2. Next.js bootstrap (CSR pages).
    3. CSS selectors + regex on the rendered text (SSR).
    """
    # 1) JSON-LD
    for item in _jsonld_blobs(html):
        apt = _apartment_from_jsonld(item, url=url, external_id=external_id)
        if apt is not None:
            return apt

    # 2) __NEXT_DATA__
    bootstrap = _next_data_blob(html)
    if bootstrap:
        apt = _apartment_from_next_data(bootstrap, url=url, external_id=external_id)
        if apt is not None:
            return apt

    # 3) CSS fallback
    return _css_detail_apartment(html, url=url, external_id=external_id)


def _apartment_from_next_data(
    bootstrap: dict[str, Any], *, url: str, external_id: str
) -> Apartment | None:
    """Dig the listing out of a Next.js bootstrap.

    The exact shape varies; we walk a few common paths and accept whatever
    looks like an apartment. We collect candidates by recursion (depth-bounded)
    rather than guessing the schema.
    """
    seen: set[int] = set()
    candidates: list[dict[str, Any]] = []

    def _visit(node: Any, depth: int) -> None:
        if depth > 4 or not isinstance(node, dict) or id(node) in seen:
            return
        seen.add(id(node))
        candidates.append(node)
        for v in node.values():
            if isinstance(v, dict):
                _visit(v, depth + 1)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        _visit(item, depth + 1)

    _visit(bootstrap, 0)
    # Prefer the deepest, most-specific candidate (last visited).
    for cand in reversed(candidates):
        apt = _apartment_from_jsonld(cand, url=url, external_id=external_id)
        if apt is not None:
            return apt
    return None


def _css_detail_apartment(html: str, *, url: str, external_id: str) -> Apartment | None:
    from deep_apartment_finder.adapters.scrapers.fotocasa.selectors import detail_selector

    sel = detail_selector()
    parser = HTMLParser(html)
    title = _text(parser.css_first(sel.title))
    price_text = _text(parser.css_first(sel.price))
    description = _text(parser.css_first(sel.description))
    address = _text(parser.css_first(sel.address))
    if not (title or price_text or description or address):
        return None
    full_text = " ".join(x for x in (title, description) if x)
    return Apartment.from_raw_dict(
        Source.FOTOCASA,
        external_id,
        url,
        {
            "title": title,
            "price_eur": _normalize_price(price_text) if price_text else None,
            "rooms": _extract_rooms(full_text),
            "bathrooms": _extract_bathrooms(full_text),
            "size_m2": _extract_size_m2(full_text),
            "address": address,
            "description": description,
            "raw": {"html_excerpt": html[:2000]},
        },
    )
