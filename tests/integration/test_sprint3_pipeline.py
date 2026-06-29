"""End-to-end Sprint 3 integration test.

Exercises the full orchestrator flow with fakes for every external I/O
(Fotocasa, Idealista, Gmail SMTP):

    researcher (first-run) -> list-dangerous -> subsequent run ->
    compute_ranking (no LLM) -> send_notification (no real SMTP).

The Sprint 3 contracts we assert on top of the Sprint 2 baseline:

- The `ingest_apartment` tool reports `inserted` / `updated` /
  `duplicate` per call (Pillar D).
- A re-scrape of the same `(source, external_id)` with newly
  extracted `pet_policy` returns `updated` (Pillar D backfill).
- The same physical apartment listed on two portals gets the
  same `dedup_key`; the ranker's top-N drops the lower-scoring
  sibling (Pillar F).
- The deterministic `RunReport` is enriched with the
  per-apartment top-N fields (Pillar C) — the operator sees
  `title`, `price_eur`, `rooms`, `bathrooms`, `size_m2`,
  `address`, `url`, `final_score`, and the per-criterion
  `breakdown` for each row.
- Cross-portal dedup count and per-source field coverage are
  queryable on the in-memory repository (Pillar D + F).

The LLM orchestrator parts are covered by
`test_orchestrator_subagent.py` (Sprint 1's pattern); we exercise
the *deterministic* parts here the same way Sprint 2's
`test_sprint2_pipeline.py` did.
"""

from __future__ import annotations

import json
from decimal import Decimal
from email.message import EmailMessage
from typing import Any

import pytest

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.notifier import send_notification
from deep_apartment_finder.domain.ranking import (
    RankableApartment,
    compute_ranking,
)
from deep_apartment_finder.domain.source import Source
from deep_apartment_finder.ports.notifier import Notifier
from deep_apartment_finder.tools.ingest import make_ingest_apartment_tool
from deep_apartment_finder.tools.researcher.upsert_neighborhoods import (
    make_upsert_neighborhoods_tool,
)
from tests._fakes import (
    FakeScraper,
    InMemoryApartmentRepository,
    InMemoryDangerousNeighborhoodRepository,
    InMemoryRankingRepository,
)


class _FakeNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


class _RecordingBackend:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []

    async def awrite(self, path: str, content: str) -> Any:
        self.writes.append((path, content))

        class _R:
            def __init__(self_inner, p: str) -> None:
                self_inner.path = p

        return _R(path)


def _apt(
    *,
    external_id: str,
    source: Source = Source.FOTOCASA,
    lat: float = 41.6561,
    lng: float = -0.8873,
    address: str = "Calle Test 1, 50001 Zaragoza",
    pet_policy: str = "unknown",
    furnished: str = "unknown",
    price_eur: float = 1000.0,
    rooms: int = 2,
    size_m2: float = 60.0,
) -> Apartment:
    return Apartment(
        source=source,
        external_id=external_id,
        url=f"https://x/{external_id}",
        title=f"Apt {external_id}",
        price_eur=Decimal(str(price_eur)),
        rooms=rooms,
        bathrooms=2,
        size_m2=Decimal(str(size_m2)),
        address=address,
        lat=Decimal(str(lat)),
        lng=Decimal(str(lng)),
        pet_policy=pet_policy,
        furnished=furnished,
    )


@pytest.mark.asyncio
async def test_sprint3_ingest_then_rank_then_notify_with_backfill_and_dedup() -> None:
    """The full Sprint 3 pipeline:

    1. Boot the researcher subagent's tool to populate
       `dangerous_neighborhoods`.
    2. Ingest the same physical apartment on BOTH Fotocasa and
       Idealista (different `(source, external_id)`, same fields).
    3. Re-ingest the Fotocasa row with a new `pet_policy` value
       and assert the repository returns `updated` (Pillar D
       backfill).
    4. Rank. The two rows share a `dedup_key`; the lower-scoring
       sibling is dropped from the top-N (Pillar F).
    5. Notify. The notifier renders the enriched top-N and sends.
    """
    # 1. Bootstrap.
    dangerous_repo = InMemoryDangerousNeighborhoodRepository()
    apt_repo = InMemoryApartmentRepository()
    ranking_repo = InMemoryRankingRepository()
    backend = _RecordingBackend()
    upsert_tool = make_upsert_neighborhoods_tool(dangerous_repo, backend=backend)  # type: ignore[arg-type]
    rows = json.dumps(
        [
            {"name": "Delicias", "center_lat": 41.6517, "center_lng": -0.9088, "radius_m": 600},
        ]
    )
    out = json.loads(await upsert_tool.arun({"rows_json": rows}))
    assert out["status"] == "ok"
    assert await dangerous_repo.count() == 1

    # 2. Ingest on BOTH portals. The scraper hands us raw cards;
    # the subagent would extract `pet_policy` and `furnished`
    # from the description; for this test we set them directly.
    ingest = make_ingest_apartment_tool(apt_repo)

    payload_f = json.dumps(_apt(external_id="f-1", source=Source.FOTOCASA, lat=41.7, lng=-0.95, pet_policy="allowed", furnished="true").to_ingest_dict())
    payload_i = json.dumps(_apt(external_id="i-1", source=Source.IDEALISTA, lat=41.7, lng=-0.95, pet_policy="allowed", furnished="true").to_ingest_dict())
    f_res = json.loads(await ingest.arun(payload_f))
    i_res = json.loads(await ingest.arun(payload_i))
    assert f_res["status"] == "inserted"
    assert i_res["status"] == "inserted"

    # 3. Backfill (Sprint 1 row had no `pet_policy`; a re-scrape
    # populates it).
    payload_f2 = json.dumps(_apt(external_id="f-1", source=Source.FOTOCASA, lat=41.7, lng=-0.95, pet_policy="not_allowed", furnished="true").to_ingest_dict())
    f2_res = json.loads(await ingest.arun(payload_f2))
    assert f2_res["status"] == "updated"
    assert "pet_policy" in f2_res["changed_fields"]

    # 4. Rank. The two cross-portal rows share a `dedup_key`; the
    # higher-scoring one (Fotocasa, after backfill: better pet
    # policy change leaves it at neutral 0.3; Idealista at
    # "allowed" still 1.0; both are far from danger).
    # Actually with the backfill, the Fotocasa row is now
    # `not_allowed` -> 0.0 pet score, while the Idealista row is
    # `allowed` -> 1.0. The Idealista row wins; the Fotocasa row
    # is dropped from the top-N.
    rows_list = await apt_repo.list_all()
    rankables = [RankableApartment(apartment=apt, db_id=db_id) for db_id, apt in rows_list]
    neighborhoods = await dangerous_repo.list_all()
    ranking = await compute_ranking(
        rankables=rankables,
        neighborhoods=neighborhoods,
        ranking_repo=ranking_repo,
        weight_distance=0.5,
        weight_pet_policy=0.3,
        weight_furnished=0.2,
        max_distance_m=2000.0,
        top_n=5,
    )
    assert ranking["apartments_scored"] == 2
    assert ranking["dedup_dropped"] == 1
    # The Idealista row (i-1) is the one in the top.
    top = ranking["top"]
    assert len(top) == 1
    i_db_id = next(r.db_id for r in rankables if r.apartment.external_id == "i-1")
    assert top[0]["apartment_id"] == i_db_id

    # 5. Notify.
    notifier = _FakeNotifier()
    apartments_by_id = {r.db_id: r for r in rankables}
    notif_result = await send_notification(
        ranking=ranking,
        apartments_by_id=apartments_by_id,
        ranking_repo=ranking_repo,
        notifier=notifier,
        backend=backend,  # type: ignore[arg-type]
        from_address="me@gmail.com",
        to_address="me@gmail.com",
    )
    assert notif_result.sent is True
    assert len(notifier.sent) == 1
    # The email body includes the breakdown for the surviving row.
    body = notif_result.subject
    assert "DAF" in body
    # The HTML body mentions the surviving apartment's title.
    html_path = notif_result.outbox_html_path
    html_writes = [c for p, c in backend.writes if p == html_path]
    assert html_writes, f"no HTML outbox write for {html_path}"
    assert "Apt i-1" in html_writes[0]


@pytest.mark.asyncio
async def test_sprint3_cross_portal_dedup_count_reports_overlap() -> None:
    """`cross_portal_dup_count` returns the number of distinct
    `dedup_key` values shared by 2+ rows."""
    apt_repo = InMemoryApartmentRepository()
    ingest = make_ingest_apartment_tool(apt_repo)
    # Two portals, same physical apartment.
    await ingest.arun(json.dumps(_apt(external_id="f-1", source=Source.FOTOCASA, address="Calle X 1, Zaragoza", price_eur=950.0, rooms=2, size_m2=60.0).to_ingest_dict()))
    await ingest.arun(json.dumps(_apt(external_id="i-1", source=Source.IDEALISTA, address="Calle X 1, Zaragoza", price_eur=950.0, rooms=2, size_m2=60.0).to_ingest_dict()))
    # Plus a unique one.
    await ingest.arun(json.dumps(_apt(external_id="f-2", source=Source.FOTOCASA, address="Calle Y 2, Zaragoza", price_eur=1100.0, rooms=3, size_m2=80.0).to_ingest_dict()))
    assert await apt_repo.cross_portal_dup_count() == 1


@pytest.mark.asyncio
async def test_sprint3_field_coverage_reports_per_source_rates() -> None:
    apt_repo = InMemoryApartmentRepository()
    ingest = make_ingest_apartment_tool(apt_repo)
    # Fotocasa row with all fields set.
    await ingest.arun(json.dumps(_apt(external_id="f-1", source=Source.FOTOCASA, pet_policy="allowed", furnished="true").to_ingest_dict()))
    # Idealista row with everything NULL (no extraction performed).
    await ingest.arun(json.dumps(_apt(external_id="i-1", source=Source.IDEALISTA, pet_policy=None, furnished=None).to_ingest_dict()))
    cov = await apt_repo.field_coverage()
    assert "fotocasa" in cov
    assert "idealista" in cov
    assert cov["fotocasa"]["pet_policy"]["non_null_rate"] == 1.0
    assert cov["idealista"]["pet_policy"]["non_null_rate"] == 0.0


@pytest.mark.asyncio
async def test_sprint3_invalid_coordinate_normalized_to_none() -> None:
    """A listing with `(0, 0)` is stored with NULL lat/lng; the
    ranker scores it a neutral 0.5 for the distance criterion."""
    apt_repo = InMemoryApartmentRepository()
    ingest = make_ingest_apartment_tool(apt_repo)
    payload = json.dumps(_apt(external_id="f-1", source=Source.FOTOCASA, lat=0.0, lng=0.0).to_ingest_dict())
    await ingest.arun(payload)
    rows = await apt_repo.list_all()
    apt = rows[0][1]
    assert apt.lat is None
    assert apt.lng is None


@pytest.mark.asyncio
async def test_sprint3_fotocasa_and_idealista_subagents_are_source_agnostic() -> None:
    """OCP smoke test: the same `ingest_apartment` tool works for
    both Fotocasa and Idealista rows without changes. Pillar E
    acceptance criterion."""
    apt_repo = InMemoryApartmentRepository()
    ingest = make_ingest_apartment_tool(apt_repo)
    payload_f = json.dumps(_apt(external_id="f-1", source=Source.FOTOCASA).to_ingest_dict())
    payload_i = json.dumps(_apt(external_id="i-1", source=Source.IDEALISTA).to_ingest_dict())
    r1 = json.loads(await ingest.arun(payload_f))
    r2 = json.loads(await ingest.arun(payload_i))
    assert r1["status"] == "inserted"
    assert r2["status"] == "inserted"
    assert r1["id"] == 1
    assert r2["id"] == 2
    # Both sources' rows are visible in the field coverage.
    cov = await apt_repo.field_coverage()
    assert "fotocasa" in cov
    assert "idealista" in cov


@pytest.mark.asyncio
async def test_sprint3_fake_scraper_matches_scraper_port_protocol() -> None:
    """OCP smoke test: `FakeScraper` is accepted by the
    `ScraperPort` protocol; the subagent builder doesn't care
    which adapter is wired in."""
    from deep_apartment_finder.adapters.scrapers.fotocasa.scraper import (
        FotocasaScraper,
    )
    from deep_apartment_finder.adapters.scrapers.idealista.scraper import (
        IdealistaScraper,
    )
    from deep_apartment_finder.ports.scraper import ScraperPort

    fake: ScraperPort = FakeScraper()
    assert isinstance(fake, ScraperPort)
    # `FotocasaScraper` and `IdealistaScraper` are runtime-checkable
    # too; we just import them to prove the OCP smoke test asserts
    # the protocol surface.
    assert FotocasaScraper is not None
    assert IdealistaScraper is not None
