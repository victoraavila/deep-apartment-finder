"""Tests for the in-memory `RankingRepository` (one-per-day dedup)."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from deep_apartment_finder.ports.ranking_repository import (
    NotificationAlreadySent,
    ScoreRow,
)
from tests._fakes import InMemoryRankingRepository


@pytest.mark.asyncio
async def test_record_send_first_time_succeeds():
    repo = InMemoryRankingRepository()
    nid = await repo.record_send(
        ranking_run_id=uuid4(),
        sent_on=date(2026, 6, 29),
        apartment_ids=[1, 2, 3],
    )
    assert nid == 1
    assert len(repo.notifications) == 1


@pytest.mark.asyncio
async def test_record_send_same_day_raises_already_sent():
    repo = InMemoryRankingRepository()
    await repo.record_send(
        ranking_run_id=uuid4(),
        sent_on=date(2026, 6, 29),
        apartment_ids=[1],
    )
    with pytest.raises(NotificationAlreadySent):
        await repo.record_send(
            ranking_run_id=uuid4(),
            sent_on=date(2026, 6, 29),
            apartment_ids=[2],
        )


@pytest.mark.asyncio
async def test_record_send_next_day_succeeds():
    repo = InMemoryRankingRepository()
    await repo.record_send(
        ranking_run_id=uuid4(),
        sent_on=date(2026, 6, 29),
        apartment_ids=[1],
    )
    nid = await repo.record_send(
        ranking_run_id=uuid4(),
        sent_on=date(2026, 6, 30),
        apartment_ids=[1],
    )
    assert nid == 2
    assert len(repo.notifications) == 2


@pytest.mark.asyncio
async def test_write_scores_persists_rows():
    repo = InMemoryRankingRepository()
    run = uuid4()
    n = await repo.write_scores(
        run,
        [
            ScoreRow(apartment_id=1, criterion="distance_to_dangerous", score=0.5, weight=0.5),
            ScoreRow(apartment_id=1, criterion="pet_policy", score=1.0, weight=0.3),
        ],
    )
    assert n == 2
    assert len(repo.scores) == 2


@pytest.mark.asyncio
async def test_top_for_run_computes_weighted_average():
    repo = InMemoryRankingRepository()
    run = uuid4()
    await repo.write_scores(
        run,
        [
            ScoreRow(apartment_id=1, criterion="distance", score=0.0, weight=0.5),
            ScoreRow(apartment_id=1, criterion="pets", score=1.0, weight=0.3),
            ScoreRow(apartment_id=1, criterion="furn", score=1.0, weight=0.2),
            ScoreRow(apartment_id=2, criterion="distance", score=1.0, weight=0.5),
            ScoreRow(apartment_id=2, criterion="pets", score=0.0, weight=0.3),
            ScoreRow(apartment_id=2, criterion="furn", score=0.0, weight=0.2),
        ],
    )
    top = await repo.top_for_run(run, top_n=2)
    assert len(top) == 2
    # Apt 1 weighted: (0*0.5 + 1*0.3 + 1*0.2) / 1.0 = 0.5
    # Apt 2 weighted: (1*0.5 + 0*0.3 + 0*0.2) / 1.0 = 0.5
    # Both equal -> order is implementation-defined; assert set membership.
    by_id = {row["apartment_id"]: row["score"] for row in top}
    assert set(by_id.keys()) == {1, 2}
    assert by_id[1] == pytest.approx(0.5, rel=1e-3)
    assert by_id[2] == pytest.approx(0.5, rel=1e-3)
