"""FotocasaScraper behaviour tests using a fake http client.

The real scraper is composed of three pieces:
- httpx client (we fake it)
- selectors (request body builder + location table) — covered separately
- api parser (item -> ListingCard / Apartment) — covered separately

The unit tests verify the composition: that the scraper calls the
right URL with the right body, paginates, dedupes via the item cache,
yields parsed cards in order, and surfaces HTTP errors as empty
iterations (not raised exceptions) so the subagent can carry on.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from deep_apartment_finder.adapters.scrapers.fotocasa.api import (
    decode_response_body,
    item_to_apartment,
    item_to_card,
)
from deep_apartment_finder.adapters.scrapers.fotocasa.scraper import FotocasaScraper
from deep_apartment_finder.adapters.scrapers.fotocasa.selectors import (
    FOTOCASA_SEARCH_URL,
    build_search_request_body,
    resolve_location,
)
from deep_apartment_finder.config import Settings
from deep_apartment_finder.domain.filters.hard import HardFilters
from deep_apartment_finder.ports.scraper import ListingCard

# ---------------------------------------------------------------------------
# Fake HTTP client that records calls and returns canned responses.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, *, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code
        self._json_body: dict[str, Any] | None = None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                "boom", request=httpx.Request("POST", FOTOCASA_SEARCH_URL), response=None  # type: ignore[arg-type]
            )

    def json(self) -> dict[str, Any]:
        return self._json_body or json.loads(self.text)


class FakeHttpClient:
    """Records every POST/GET and returns the canned response."""

    def __init__(self, responses: list[_FakeResponse] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def queue(self, *responses: _FakeResponse) -> None:
        self._responses.extend(responses)

    async def post(self, url: str, json: dict[str, Any] | None = None) -> _FakeResponse:
        self.calls.append((url, json or {}))
        if not self._responses:
            return _FakeResponse(text="{}", status_code=200)
        return self._responses.pop(0)

    async def get(self, url: str) -> _FakeResponse:
        self.calls.append((url, {}))
        if not self._responses:
            return _FakeResponse(text="", status_code=200)
        return self._responses.pop(0)

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**kwargs: Any) -> Settings:
    base = dict(
        scraper_user_agent="test-ua",
        scraper_delay_seconds=0.0,  # don't slow tests
    )
    base.update(kwargs)
    return Settings(**base)


def _search_response(*, total: int, ids: list[str], with_size: bool = True) -> dict[str, Any]:
    items = []
    for pid in ids:
        items.append(
            {
                "id": f"3_{pid}",
                "propertyId": pid,
                "transactionType": "3",
                "propertyType": "2",
                "propertySubtype": "1",
                "purchaseType": "2",
                "surface": 80,
                "rooms": 3,
                "baths": 2,
                "floor": "3",
                "addressVisibilityMode": "3",
                "zipCode": "50001",
                "description": "Bonito piso.",
                "transaction": {"type": "RENT", "price": 1100, "priceDrop": None},
                "location": {
                    "countryId": "724",
                    "countryName": "España",
                    "level5Name": "Zaragoza",
                    "level7Name": "Delicias",
                    "latitude": "41.65",
                    "longitude": "-0.88",
                },
                "agency": {"id": "x", "name": "Test"},
                "multimedia": [],
                "features": [],
                "uris": [
                    {
                        "language": "es_ES",
                        "value": f"/es/alquiler/vivienda/zaragoza-capital/calefaccion/{pid}/d",
                    }
                ],
                "dynamicFeatures": [],
            }
        )
    return {
        "totalItems": total,
        "page": {"number": 1, "size": len(items)} if with_size else {"number": 1, "size": 30},
        "items": items,
        "locationFacets": {},
        "purchaseTypeFacets": [],
        "containerCombinedLocation": {},
        "urlLocationSegments": {},
    }


# ---------------------------------------------------------------------------
# search_listings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_listings_posts_to_fotocasa_search_ads():
    settings = _settings()
    payload = _search_response(total=2, ids=["1", "2"])
    http = FakeHttpClient([_FakeResponse(text=json.dumps(payload))])
    scraper = FotocasaScraper(settings=settings, http_client=http, page_size=30)
    cards: list[ListingCard] = []
    async for c in scraper.search_listings(HardFilters()):
        cards.append(c)
    assert [c.external_id for c in cards] == ["1", "2"]
    assert len(http.calls) == 1
    url, body = http.calls[0]
    assert url == FOTOCASA_SEARCH_URL
    # Server-side filter contracts are not supported by the gateway
    # (any non-empty contracts array triggers a 400). Filtering
    # happens client-side; the body must carry an empty contracts
    # list.
    assert body["contracts"] == []
    assert body["pageNumber"] == 1
    assert body["size"] == 30
    assert body["transactionType"] == 3
    await scraper.close()


@pytest.mark.asyncio
async def test_search_listings_filters_client_side_by_hard_filters():
    """Cards that fail the hard filters are dropped before yielding."""
    settings = _settings()
    payload = _search_response(
        total=3,
        ids=["cheap-ok", "too-expensive", "too-small"],
    )
    # Override prices / surfaces per item.
    payload["items"][0]["transaction"]["price"] = 1100
    payload["items"][0]["surface"] = 80
    payload["items"][1]["transaction"]["price"] = 2000  # > max_price
    payload["items"][1]["surface"] = 80
    payload["items"][2]["transaction"]["price"] = 1100
    payload["items"][2]["surface"] = 30  # < min_size

    http = FakeHttpClient([_FakeResponse(text=json.dumps(payload))])
    scraper = FotocasaScraper(settings=settings, http_client=http, page_size=30)
    cards: list[ListingCard] = []
    async for c in scraper.search_listings(
        HardFilters(min_size_m2=50.0, max_price_eur=1200.0)
    ):
        cards.append(c)
    assert [c.external_id for c in cards] == ["cheap-ok"]
    await scraper.close()


@pytest.mark.asyncio
async def test_search_listings_respects_max_cards():
    settings = _settings()
    payload = _search_response(total=10, ids=[str(i) for i in range(10)])
    http = FakeHttpClient([_FakeResponse(text=json.dumps(payload))])
    scraper = FotocasaScraper(settings=settings, http_client=http, max_cards=3, page_size=30)
    cards: list[ListingCard] = []
    async for c in scraper.search_listings(HardFilters()):
        cards.append(c)
    assert len(cards) == 3
    # Only one HTTP call (the first page was enough to fill 3 cards).
    assert len(http.calls) == 1
    await scraper.close()


@pytest.mark.asyncio
async def test_search_listings_paginates_until_total_exhausted():
    settings = _settings()
    page1 = _search_response(total=5, ids=["1", "2", "3"])
    page2 = _search_response(total=5, ids=["4", "5"])
    http = FakeHttpClient(
        [
            _FakeResponse(text=json.dumps(page1)),
            _FakeResponse(text=json.dumps(page2)),
        ]
    )
    scraper = FotocasaScraper(settings=settings, http_client=http, page_size=3)
    cards: list[ListingCard] = []
    async for c in scraper.search_listings(HardFilters()):
        cards.append(c)
    assert [c.external_id for c in cards] == ["1", "2", "3", "4", "5"]
    assert len(http.calls) == 2
    assert http.calls[0][1]["pageNumber"] == 1
    assert http.calls[1][1]["pageNumber"] == 2
    await scraper.close()


@pytest.mark.asyncio
async def test_search_listings_swallows_http_error_and_returns_empty():
    import httpx

    class _BoomClient:
        async def post(self, url: str, json: dict[str, Any] | None = None) -> _FakeResponse:
            raise httpx.ConnectError("simulated")

        async def get(self, url: str) -> _FakeResponse:
            raise httpx.ConnectError("simulated")

        async def aclose(self) -> None:
            return None

    settings = _settings()
    scraper = FotocasaScraper(settings=settings, http_client=_BoomClient())
    cards: list[ListingCard] = []
    async for c in scraper.search_listings(HardFilters()):
        cards.append(c)
    assert cards == []


@pytest.mark.asyncio
async def test_search_listings_handles_base64_response():
    """The HAR shows the gateway sometimes returns base64; the scraper
    must accept that transparently without raising."""
    import base64

    settings = _settings()
    payload = _search_response(total=1, ids=["abc"])
    b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    http = FakeHttpClient([_FakeResponse(text=b64)])
    scraper = FotocasaScraper(settings=settings, http_client=http)
    cards: list[ListingCard] = []
    async for c in scraper.search_listings(HardFilters()):
        cards.append(c)
    assert [c.external_id for c in cards] == ["abc"]


@pytest.mark.asyncio
async def test_search_listings_warns_on_unknown_city():
    settings = _settings()
    http = FakeHttpClient()
    scraper = FotocasaScraper(settings=settings, http_client=http)
    cards: list[ListingCard] = []
    async for c in scraper.search_listings(HardFilters(city="Atlantis")):
        cards.append(c)
    assert cards == []
    assert http.calls == []  # no HTTP call made


# ---------------------------------------------------------------------------
# fetch_listing — uses the in-memory item cache populated by search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_listing_uses_cached_item_from_search():
    settings = _settings()
    payload = _search_response(total=1, ids=["99"])
    http = FakeHttpClient([_FakeResponse(text=json.dumps(payload))])
    scraper = FotocasaScraper(settings=settings, http_client=http)
    async for _ in scraper.search_listings(HardFilters()):
        pass
    url = "https://www.fotocasa.es/es/alquiler/vivienda/zaragoza-capital/calefaccion/99/d"
    apt = await scraper.fetch_listing(url)
    assert apt.external_id == "99"
    assert apt.title is not None and "zaragoza-capital" in apt.title
    assert apt.rooms == 3
    assert apt.bathrooms == 2
    assert apt.size_m2 is not None and float(apt.size_m2) == 80.0
    assert apt.price_eur is not None and float(apt.price_eur) == 1100.0


@pytest.mark.asyncio
async def test_close_is_idempotent():
    settings = _settings()
    http = FakeHttpClient()
    scraper = FotocasaScraper(settings=settings, http_client=http)
    await scraper.close()


# ---------------------------------------------------------------------------
# decode_response_body
# ---------------------------------------------------------------------------


def test_decode_response_body_accepts_plain_json():
    raw = json.dumps({"totalItems": 1, "items": []})
    out = decode_response_body(raw)
    assert out["totalItems"] == 1


def test_decode_response_body_accepts_base64():
    import base64

    raw = base64.b64encode(b'{"totalItems": 2, "items": []}').decode("ascii")
    out = decode_response_body(raw)
    assert out["totalItems"] == 2


def test_decode_response_body_raises_on_garbage():
    with pytest.raises(ValueError):
        decode_response_body("!!! not base64, not json !!!")


# ---------------------------------------------------------------------------
# item_to_card / item_to_apartment
# ---------------------------------------------------------------------------


def _sample_item(prop_id: str = "42", price: int = 950) -> dict[str, Any]:
    return {
        "id": f"3_{prop_id}",
        "propertyId": prop_id,
        "transaction": {"type": "RENT", "price": price},
        "surface": 75,
        "rooms": 2,
        "baths": 1,
        "description": "Piso reformado.",
        "addressVisibilityMode": "3",
        "zipCode": "50004",
        "location": {
            "level5Name": "Zaragoza",
            "level7Name": "Torrero",
            "latitude": "41.63",
            "longitude": "-0.88",
        },
        "uris": [
            {
                "language": "es_ES",
                "value": f"/es/alquiler/vivienda/zaragoza-capital/ascensor/{prop_id}/d",
            }
        ],
    }


def test_item_to_card_basic():
    card = item_to_card(_sample_item(), base_url="https://www.fotocasa.es")
    assert card is not None
    assert card.external_id == "42"
    assert card.url == "https://www.fotocasa.es/es/alquiler/vivienda/zaragoza-capital/ascensor/42/d"
    assert card.price_eur == 950.0
    assert card.title is not None and "zaragoza-capital" in card.title


def test_item_to_card_skips_rows_without_property_id():
    card = item_to_card({}, base_url="https://www.fotocasa.es")
    assert card is None


def test_item_to_apartment_basic():
    apt = item_to_apartment(_sample_item(), base_url="https://www.fotocasa.es")
    assert apt is not None
    assert apt.rooms == 2
    assert apt.bathrooms == 1
    assert apt.size_m2 is not None and float(apt.size_m2) == 75.0
    assert apt.address and "Zaragoza" in apt.address and "50004" in apt.address
    assert apt.lat is not None and abs(float(apt.lat) - 41.63) < 1e-6


def test_item_to_apartment_address_hidden_then_neighbourhood():
    item = _sample_item()
    item["addressVisibilityMode"] = "3"
    apt = item_to_apartment(item, base_url="https://www.fotocasa.es")
    assert apt is not None
    # No street — falls back to neighbourhood + city + zip.
    assert "Torrero" in (apt.address or "")
    assert "Zaragoza" in (apt.address or "")


# ---------------------------------------------------------------------------
# selectors — location resolution & request body
# ---------------------------------------------------------------------------


def test_resolve_location_zaragoza_capital():
    spec = resolve_location("Zaragoza")
    assert spec.combined_location_ids == "724,2,50,208,300,50297,0,0,0"


def test_resolve_location_zaragoza_provincia():
    spec = resolve_location("zaragoza-provincia")
    assert spec.combined_location_ids == "724,2,50,0,0,0,0,0,0"


def test_resolve_location_unknown_raises():
    with pytest.raises(KeyError):
        resolve_location("atlantis")


def test_build_search_request_body_with_all_filters():
    body = build_search_request_body(
        HardFilters(
            city="Zaragoza",
            min_rooms=2,
            min_bathrooms=2,
            min_size_m2=50.0,
            max_price_eur=1200.0,
        )
    )
    # Server-side filter contracts are not supported; the body must
    # carry an empty contracts list regardless of the hard filters.
    assert body["contracts"] == []
    assert body["transactionType"] == 3
    assert body["propertyType"] == 2
    assert body["combinedLocations"] == ["724,2,50,208,300,50297,0,0,0"]


def test_build_search_request_body_sorts_by_price_when_capped():
    body = build_search_request_body(
        HardFilters(city="Zaragoza", max_price_eur=1200.0),
        sort_by_price=True,
    )
    assert body["sortType"] == "price"
    assert body["sortOrderDesc"] is False


def test_build_search_request_body_default_sort_is_scoring():
    body = build_search_request_body(HardFilters(city="Zaragoza"))
    assert body["sortType"] == "scoring"
    assert body["sortOrderDesc"] is True


def test_build_search_request_body_no_filters():
    body = build_search_request_body(
        HardFilters(
            city="Zaragoza",
            min_rooms=None,
            min_bathrooms=None,
            min_size_m2=None,
            max_price_eur=None,
        )
    )
    assert body["contracts"] == []
    assert body["pageNumber"] == 1
    assert body["size"] == 30


def test_detail_url_from_item_prefers_spanish_uri():
    from deep_apartment_finder.adapters.scrapers.fotocasa.selectors import (
        detail_url_from_item,
    )

    url = detail_url_from_item(_sample_item("77"))
    assert url == "https://www.fotocasa.es/es/alquiler/vivienda/zaragoza-capital/ascensor/77/d"
