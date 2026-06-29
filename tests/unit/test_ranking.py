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


# --- Sprint 3: top-N dedup by `dedup_key` ----------------------------------


@pytest.mark.asyncio
async def test_compute_ranking_drops_lower_scoring_dedup_sibling() -> None:
    """When two apartments share a `dedup_key`, only the higher-scoring
    one survives in the top-N; the lower-scoring sibling is dropped."""
    repo = InMemoryRankingRepository()
    rankables = [
        RankableApartment(
            # Apartment A — better score (far from danger, pets allowed,
            # furnished). Carries the dedup_key.
            apartment=Apartment(
                source=Source.FOTOCASA,
                external_id="A",
                url="https://x/A",
                lat=Decimal("41.7"),
                lng=Decimal("-0.95"),
                pet_policy="allowed",
                furnished="true",
                raw={"dedup_key": "shared-key-1"},
            ),
            db_id=1,
        ),
        RankableApartment(
            # Same dedup_key, much worse — would normally be ranked 2nd.
            apartment=Apartment(
                source=Source.FOTOCASA,
                external_id="A-prime",
                url="https://x/A-prime",
                lat=Decimal("41.65"),
                lng=Decimal("-0.88"),
                pet_policy="not_allowed",
                furnished="false",
                raw={"dedup_key": "shared-key-1"},
            ),
            db_id=2,
        ),
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
    assert res["dedup_dropped"] == 1
    # Apartment 1 (A, the better one) is in the top; apartment 2 is dropped.
    assert [row["apartment_id"] for row in res["top"]] == [1]


@pytest.mark.asyncio
async def test_compute_ranking_dedup_pass_does_not_drop_unique_keys() -> None:
    """When apartments have unique (or NULL) dedup_keys, the top is unchanged."""
    repo = InMemoryRankingRepository()
    rankables = [
        RankableApartment(
            apartment=_apt(external_id="A", lat=41.7, lng=-0.95,
                           pet_policy="allowed", furnished="true"),
            db_id=1,
        ),
        RankableApartment(
            apartment=_apt(external_id="B", lat=41.65, lng=-0.88,
                           pet_policy="not_allowed", furnished="false"),
            db_id=2,
        ),
        RankableApartment(
            # No dedup_key (Sprint 1/2 row).
            apartment=_apt(external_id="C", lat=41.70, lng=-0.95,
                           pet_policy="unknown", furnished="unknown"),
            db_id=3,
        ),
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
    assert res["dedup_dropped"] == 0
    assert len(res["top"]) == 3


@pytest.mark.asyncio
async def test_compute_ranking_dedup_drops_only_after_higher_scored_sibling() -> None:
    """The first occurrence of a `dedup_key` in the sorted top-N is kept;
    later ones (lower-scoring) are dropped."""
    from deep_apartment_finder.domain.apartment import Apartment

    repo = InMemoryRankingRepository()
    rankables = [
        RankableApartment(
            # Sibling B-prime (worse) — higher scored due to a "lucky" combo.
            apartment=Apartment(
                source=Source.FOTOCASA,
                external_id="B-prime",
                url="https://x/B-prime",
                lat=Decimal("41.7"),
                lng=Decimal("-0.95"),
                pet_policy="allowed",
                furnished="true",
                raw={"dedup_key": "shared-key-1"},
            ),
            db_id=1,
        ),
        RankableApartment(
            # Better: this is the A entry, will rank lower.
            apartment=Apartment(
                source=Source.FOTOCASA,
                external_id="A",
                url="https://x/A",
                lat=Decimal("41.5"),
                lng=Decimal("-0.85"),
                pet_policy="not_allowed",
                furnished="false",
                raw={"dedup_key": "shared-key-1"},
            ),
            db_id=2,
        ),
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
    assert res["dedup_dropped"] == 1
    # The first one seen (B-prime, the higher score) is kept.
    assert [row["apartment_id"] for row in res["top"]] == [1]
