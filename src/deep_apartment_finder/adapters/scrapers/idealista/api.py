"""Pure parsers for Idealista SSR'd search HTML.

Idealista's search results page is fully server-rendered: each
listing is an `<article class="item ...">` with the title, price,
address, photo, description, and a list of short detail badges
(rooms, m², floor, garage, etc.). There is no JSON-LD and no
`__NEXT_DATA__` blob on the search page; the only reliable way to
extract cards is via CSS selectors. The parsers here are pure
functions of `(html) -> list[ListingCard] | Apartment`. They do no
I/O. The scraper fetches; the parsers shape.

Field coverage on the search card:
- `external_id` — numeric tail of `/inmueble/<id>/`
- `url` — same path, absolute
- `title` — text of the `<a class="item-link">`
- `price_eur` — text of `<span class="item-price">` (e.g. `825 €/mes`)
- `address` — the `title=` attribute of the same `<a>`, which carries
  `"Piso en Calle ..., City"`
- `rooms`, `size_m2` — extracted from the detail badges
  (`"2 hab."`, `"115 m²"`)
- `bathrooms` — never on the search card; always `None` for Sprint 3
  (only on the detail page, which DataDome blocks for non-browser
  clients). The ranker treats missing values as a neutral 0.5.
- `lat`, `lng` — never on the card; always `None` for Sprint 3
  (same reason). Distance-to-dangerous will score 0.5 for every
  Idealista row until the detail-page upgrade lands.
"""

from __future__ import annotations

import re
from typing import Any

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.source import Source
from deep_apartment_finder.ports.scraper import ListingCard

# --- regexes used by the detail-badge parser --------------------------------

_ROOMS_RE = re.compile(r"(\d+)\s*hab\.?", re.IGNORECASE)
_M2_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m[²2]", re.IGNORECASE)
_BATHS_RE = re.compile(r"(\d+)\s*bañ[oa]s?", re.IGNORECASE)
_PRICE_RE = re.compile(r"([\d.,]+)")


def _normalize_price(raw: str | None) -> float | None:
    """`"1.400 €/mes"` -> `1400.0`, `"1200.50 €/mes"` -> `1200.50`.

    Idealista uses Spanish number conventions: `.` as thousands
    separator (`1.400`) and `,` as decimal separator (`1.400,50`).
    The only ambiguity is `"1200.50"`: it could be twelve-hundred-dot-
    fifty (decimal) or twelve-thousand-fifty (thousands-separator + a
    typo). We resolve the ambiguity by the number of digits after the
    separator: 3 digits = thousands; 1 or 2 digits = decimal. The
    combined case (`1.400,50`) is unambiguous.
    """
    if not raw:
        return None
    cleaned = raw.replace("€", "").replace("/mes", "").strip()
    m = _PRICE_RE.search(cleaned)
    if not m:
        return None
    digits = m.group(1)

    has_dot = "." in digits
    has_comma = "," in digits

    if has_dot and has_comma:
        # Spanish: 1.400,50 -> 1400.50. Always treat "." as thousands.
        digits = digits.replace(".", "").replace(",", ".")
    elif has_comma:
        # Comma-only: could be 1400,50 (decimal) or 1,400 (thousands).
        parts = digits.split(",")
        if len(parts) == 2 and len(parts[1]) == 3:
            # "1,400" -> thousands.
            digits = digits.replace(",", "")
        else:
            # "1400,50" -> decimal.
            digits = digits.replace(",", ".")
    elif has_dot:
        # Dot-only: could be 1.400 (thousands) or 1200.50 (decimal).
        parts = digits.split(".")
        if len(parts) == 2 and len(parts[1]) == 3:
            # "1.400" -> thousands.
            digits = digits.replace(".", "")
        else:
            # "1200.50" or "1200.5" -> decimal.
            pass
    try:
        return float(digits)
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


# --- public parser ---------------------------------------------------------


def parse_search_page(html: str) -> list[ListingCard]:
    """Extract every listing card on a search results page.

    Filters out cards without a usable `external_id` (e.g. branding
    slots the page inserts between organic results). Collapses
    duplicates by `external_id` defensively.
    """
    parser = HTMLParser(html)
    seen: dict[str, ListingCard] = {}
    for article in parser.css("article.item"):
        card = _card_from_article(article)
        if card is None or card.external_id in seen:
            continue
        seen[card.external_id] = card
    return list(seen.values())


def card_to_apartment(card: ListingCard) -> Apartment:
    """Promote a `ListingCard` to a full `Apartment`.

    The card already carries all the fielded data the Sprint 1 + 2
    ranker needs (title, price, address, rooms, m², partial
    description, photo). `bathrooms` and `lat`/`lng` are
    intentionally left as `None` — the dataDome-protected detail
    page would be the only source, and the SCRAPER layer has already
    decided to skip it. A future playwright upgrade (ADR-011) can
    backfill these fields.

    The `raw` blob carries the original `ListingCard.raw` dict so the
    repository can replay it. We additionally pass the search-time
    `description` as the apartment description; the ranker reads it
    for the pet_policy / furnished extraction.
    """
    raw = card.raw or {}
    return Apartment.from_raw_dict(
        Source.IDEALISTA,
        card.external_id,
        card.url,
        {
            "title": card.title,
            "price_eur": card.price_eur,
            "rooms": raw.get("rooms"),
            "bathrooms": raw.get("bathrooms"),
            "size_m2": raw.get("size_m2"),
            "address": raw.get("address"),
            "lat": None,
            "lng": None,
            "description": raw.get("description"),
            "raw": raw,
        },
    )


# --- internal helpers ------------------------------------------------------


def _card_from_article(article: Any) -> ListingCard | None:
    """Build a `ListingCard` from a single `<article class="item ...">`."""
    link = article.css_first("a.item-link")
    if link is None:
        return None
    href = link.attributes.get("href") if link.attributes else None
    if not href:
        return None
    # external_id from the path tail
    m = re.search(r"/inmueble/(\d+)/?", href)
    if not m:
        return None
    external_id = m.group(1)

    title = link.text(strip=True) or None
    address = link.attributes.get("title") if link.attributes else title

    price_node = article.css_first("span.item-price")
    price = _normalize_price(price_node.text(strip=True)) if price_node else None

    # Detail badges: <span class="item-detail">N hab.</span>, etc.
    badges = [
        s.text(strip=True)
        for s in article.css("span.item-detail")
        if s.text(strip=True)
    ]
    badge_text = " ".join(badges)
    rooms_m = _ROOMS_RE.search(badge_text)
    rooms = _parse_int(rooms_m.group(1)) if rooms_m else None
    size_m = _M2_RE.search(badge_text)
    size_m2 = _parse_float(size_m.group(1)) if size_m else None
    baths_m = _BATHS_RE.search(badge_text)
    bathrooms = _parse_int(baths_m.group(1)) if baths_m else None

    desc_node = article.css_first("p.ellipsis")
    description = desc_node.text(strip=True) if desc_node else None

    photo_node = article.css_first("img")
    photo = (
        photo_node.attributes.get("src")
        if photo_node and photo_node.attributes
        else None
    )

    url = href if href.startswith("http") else f"https://www.idealista.com{href}"

    return ListingCard(
        external_id=external_id,
        url=url,
        title=title,
        price_eur=price,
        raw={
            "address": address,
            "rooms": rooms,
            "bathrooms": bathrooms,
            "size_m2": size_m2,
            "description": description,
            "photo": photo,
            "badges": badges,
        },
    )


__all__ = ["parse_search_page", "card_to_apartment"]
