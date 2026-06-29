"""Tests for the deterministic ranker (`compute_ranking`).

The ranker is pure-Python; we exercise it with the in-memory
`RankingRepository` from `tests._fakes`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.geo import DangerousNeighborhood
from deep_apartment_finder.domain.ranking import (
    RankableApartment,
    compute_ranking,
)
from deep_apartment_finder.domain.source import Source
from tests._fakes import InMemoryRankingRepository


def _apt(
    *,
    external_id: str = "x",
    lat: float | None = 41.65,
    lng: float | None = -0.88,
    pet_policy: str | None = "unknown",
    furnished: str | None = "unknown",
) -> Apartment:
    return Apartment(
        source=Source.FOTOCASA,
        external_id=external_id,
        url=f"https://x/{external_id}",
        lat=Decimal(str(lat)) if lat is not None else None,
        lng=Decimal(str(lng)) if lng is not None else None,
        pet_policy=pet_policy,
        furnished=furnished,
    )


@pytest.mark.asyncio
async def test_compute_ranking_writes_one_row_per_criterion_per_apartment():
    repo = InMemoryRankingRepository()
    rankables = [
        RankableApartment(apartment=_apt(external_id="a"), db_id=1),
        RankableApartment(apartment=_apt(external_id="b"), db_id=2),
    ]
    res = await compute_ranking(
        rankables=rankables,
        neighborhoods=[],
        ranking_repo=repo,
        weight_distance=0.5,
        weight_pet_policy=0.3,
        weight_furnished=0.2,
        max_distance_m=2000.0,
        top_n=5,
    )
    assert res["apartments_scored"] == 2
    assert res["scores_written"] == 6  # 2 apartments * 3 criteria
    assert len(repo.scores) == 6


@pytest.mark.asyncio
async def test_compute_ranking_top_is_sorted_desc_by_score():
    repo = InMemoryRankingRepository()
    neighborhoods = [
        DangerousNeighborhood(
            name="Delicias", center_lat=41.6517, center_lng=-0.9088, radius_m=600
        ),
    ]
    # A: inside the danger zone, no pets, no furniture.
    # B: far, pets allowed, furnished. Should win.
    # C: far, pets unknown, furniture unknown. Middle.
    rankables = [
        RankableApartment(
            apartment=_apt(external_id="A", lat=41.6517, lng=-0.9088,
                           pet_policy="not_allowed", furnished="false"),
            db_id=1,
        ),
        RankableApartment(
            apartment=_apt(external_id="B", lat=41.7000, lng=-0.9500,
                           pet_policy="allowed", furnished="true"),
            db_id=2,
        ),
        RankableApartment(
            apartment=_apt(external_id="C", lat=41.7000, lng=-0.9500,
                           pet_policy="unknown", furnished="unknown"),
            db_id=3,
        ),
    ]
    res = await compute_ranking(
        rankables=rankables,
        neighborhoods=neighborhoods,
        ranking_repo=repo,
        weight_distance=0.5,
        weight_pet_policy=0.3,
        weight_furnished=0.2,
        max_distance_m=2000.0,
        top_n=3,
    )
    top_ids = [row["apartment_id"] for row in res["top"]]
    # B should be first (high everything), A should be last (low everything).
    assert top_ids[0] == 2
    assert top_ids[-1] == 1


@pytest.mark.asyncio
async def test_compute_ranking_top_n_caps_result():
    repo = InMemoryRankingRepository()
    rankables = [
        RankableApartment(apartment=_apt(external_id=f"a{i}"), db_id=i + 1)
        for i in range(10)
    ]
    res = await compute_ranking(
        rankables=rankables,
        neighborhoods=[],
        ranking_repo=repo,
        weight_distance=0.5,
        weight_pet_policy=0.3,
        weight_furnished=0.2,
        max_distance_m=2000.0,
        top_n=3,
    )
    assert len(res["top"]) == 3
    assert res["apartments_scored"] == 10


@pytest.mark.asyncio
async def test_compute_ranking_uses_passed_run_id():
    repo = InMemoryRankingRepository()
    run_id = uuid.uuid4()
    rankables = [RankableApartment(apartment=_apt(), db_id=1)]
    res = await compute_ranking(
        rankables=rankables,
        neighborhoods=[],
        ranking_repo=repo,
        weight_distance=0.5,
        weight_pet_policy=0.3,
        weight_furnished=0.2,
        max_distance_m=2000.0,
        top_n=1,
        ranking_run_id=run_id,
    )
    assert res["ranking_run_id"] == run_id
    persisted = await repo.top_for_run(run_id, top_n=5)
    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_compute_ranking_handles_empty_input():
    repo = InMemoryRankingRepository()
    res = await compute_ranking(
        rankables=[],
        neighborhoods=[],
        ranking_repo=repo,
        weight_distance=0.5,
        weight_pet_policy=0.3,
        weight_furnished=0.2,
        max_distance_m=2000.0,
        top_n=5,
    )
    assert res["apartments_scored"] == 0
    assert res["scores_written"] == 0
    assert res["top"] == []
