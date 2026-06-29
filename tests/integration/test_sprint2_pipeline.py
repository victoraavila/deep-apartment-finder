"""End-to-end Sprint 2 integration test.

Exercises the full orchestrator flow with fakes:

    researcher (first-run) -> list-dangerous -> subsequent run ->
    compute_ranking (no LLM) -> send_notification (no real SMTP).

We don't drive the LLM at all; we exercise the *deterministic*
parts and the researcher subagent's `upsert_neighborhoods` tool.
The LLM orchestrator parts are covered by `test_orchestrator_subagent.py`
(Sprint 1's pattern).

The test asserts the acceptance-criterion 4 contract: re-sending
on the same day is a no-op (no second SMTP call, no second DB row).
"""

from __future__ import annotations

import json
from email.message import EmailMessage
from typing import Any

import pytest

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.geo import DangerousNeighborhood
from deep_apartment_finder.domain.notifier import send_notification
from deep_apartment_finder.domain.ranking import (
    RankableApartment,
    compute_ranking,
)
from deep_apartment_finder.domain.source import Source
from deep_apartment_finder.ports.notifier import Notifier
from deep_apartment_finder.tools.researcher.upsert_neighborhoods import (
    make_upsert_neighborhoods_tool,
)
from tests._fakes import (
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


def _apt(*, external_id: str, lat: float, lng: float,
         pet_policy: str = "unknown", furnished: str = "unknown") -> Apartment:
    from decimal import Decimal

    return Apartment(
        source=Source.FOTOCASA,
        external_id=external_id,
        url=f"https://x/{external_id}",
        title=f"Apt {external_id}",
        price_eur=Decimal("1000"),
        rooms=2,
        bathrooms=2,
        size_m2=Decimal("60"),
        address="Test",
        lat=Decimal(str(lat)),
        lng=Decimal(str(lng)),
        pet_policy=pet_policy,
        furnished=furnished,
    )


@pytest.mark.asyncio
async def test_first_run_bootstrap_then_full_pipeline_with_fakes():
    # 1. First-run state: empty dangerous-neighborhoods table.
    dangerous_repo = InMemoryDangerousNeighborhoodRepository()
    apt_repo = InMemoryApartmentRepository()
    ranking_repo = InMemoryRankingRepository()
    backend = _RecordingBackend()

    # 2. Researcher subagent's tool: bootstrap a few rows.
    upsert_tool = make_upsert_neighborhoods_tool(dangerous_repo, backend=backend)  # type: ignore[arg-type]
    rows = json.dumps(
        [
            {"name": "Delicias", "center_lat": 41.6517, "center_lng": -0.9088, "radius_m": 600},
            {"name": "El Gancho", "center_lat": 41.654, "center_lng": -0.881, "radius_m": 400},
        ]
    )
    out = json.loads(await upsert_tool.arun({"rows_json": rows}))
    assert out["status"] == "ok"
    assert out["written"] == 2
    assert await dangerous_repo.count() == 2

    # 3. Subsequent run: ingest a few apartments directly (we don't
    #    drive the LLM scraper here; that path is tested in S1).
    await apt_repo.upsert(
        _apt(external_id="a", lat=41.6517, lng=-0.9088, pet_policy="not_allowed", furnished="false")
    )
    await apt_repo.upsert(
        _apt(external_id="b", lat=41.7000, lng=-0.9500, pet_policy="allowed", furnished="true")
    )

    rows_list = await apt_repo.list_all()
    rankables = [RankableApartment(apartment=apt, db_id=db_id) for db_id, apt in rows_list]
    neighborhoods = await dangerous_repo.list_all()

    # 4. Run the ranker.
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
    assert ranking["scores_written"] == 6
    assert len(ranking["top"]) == 2
    # 'b' (allowed pets, furnished, far) should beat 'a' (in danger, no pets).
    top_ids = {row["apartment_id"] for row in ranking["top"]}
    assert top_ids == {r.db_id for r in rankables}
    b_db_id = next(r.db_id for r in rankables if r.apartment.external_id == "b")
    assert ranking["top"][0]["apartment_id"] == b_db_id

    # 5. Send the notification.
    notifier = _FakeNotifier()
    apartments_by_id = {r.db_id: r for r in rankables}
    result = await send_notification(
        ranking=ranking,
        apartments_by_id=apartments_by_id,
        ranking_repo=ranking_repo,
        notifier=notifier,
        backend=backend,  # type: ignore[arg-type]
        from_address="me@gmail.com",
        to_address="me@gmail.com",
    )
    assert result.sent is True
    assert len(notifier.sent) == 1

    # 6. Re-run the same day: notifier should be a no-op.
    ranking2 = await compute_ranking(
        rankables=rankables,
        neighborhoods=neighborhoods,
        ranking_repo=ranking_repo,
        weight_distance=0.5,
        weight_pet_policy=0.3,
        weight_furnished=0.2,
        max_distance_m=2000.0,
        top_n=5,
    )
    result2 = await send_notification(
        ranking=ranking2,
        apartments_by_id=apartments_by_id,
        ranking_repo=ranking_repo,
        notifier=notifier,
        backend=backend,  # type: ignore[arg-type]
        from_address="me@gmail.com",
        to_address="me@gmail.com",
    )
    assert result2.sent is False
    assert result2.skipped_reason == "already sent today"
    assert len(notifier.sent) == 1  # unchanged from the first send


@pytest.mark.asyncio
async def test_acceptance_3_empty_top_does_not_send_email():
    """When the ranker scores 0 apartments, the notifier is a no-op
    and no row is recorded in `notifications`."""
    dangerous_repo = InMemoryDangerousNeighborhoodRepository()
    ranking_repo = InMemoryRankingRepository()
    backend = _RecordingBackend()
    notifier = _FakeNotifier()

    await dangerous_repo.upsert_many(
        [DangerousNeighborhood("X", 41.65, -0.88, 500)], source="test"
    )
    ranking = await compute_ranking(
        rankables=[],
        neighborhoods=await dangerous_repo.list_all(),
        ranking_repo=ranking_repo,
        weight_distance=0.5,
        weight_pet_policy=0.3,
        weight_furnished=0.2,
        max_distance_m=2000.0,
        top_n=5,
    )
    result = await send_notification(
        ranking=ranking,
        apartments_by_id={},
        ranking_repo=ranking_repo,
        notifier=notifier,
        backend=backend,  # type: ignore[arg-type]
        from_address="me@gmail.com",
        to_address="me@gmail.com",
    )
    assert result.sent is False
    assert result.skipped_reason == "empty top"
    assert notifier.sent == []
    assert len(ranking_repo.notifications) == 0
