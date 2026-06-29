"""Tests for the deterministic notifier (`send_notification`).

We use:
- a fake `Notifier` that records the message it was asked to send
  (no real SMTP),
- a recording `BackendProtocol` (the `CompositeBackend`'s
  persistent routes sit on top of an `InMemoryStore`; we
  short-circuit at the tool layer).

The acceptance-criterion 4 contract: re-sending on the same day
is a no-op (no SMTP call, no second DB row).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from typing import Any
from uuid import UUID, uuid4

import pytest

from deep_apartment_finder.domain.apartment import Apartment
from deep_apartment_finder.domain.notifier import send_notification
from deep_apartment_finder.domain.ranking import RankableApartment
from deep_apartment_finder.domain.source import Source
from deep_apartment_finder.ports.notifier import Notifier
from tests._fakes import InMemoryRankingRepository


@dataclass
class _CapturedSend:
    message: EmailMessage


class _FakeNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


class _RecordingBackend:
    """Minimal stand-in for `BackendProtocol` — records `awrite` and `aread`."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []
        self._files: dict[str, str] = {}

    async def awrite(self, path: str, content: str) -> Any:
        self.writes.append((path, content))
        self._files[path] = content

        class _R:
            def __init__(self_inner, p: str) -> None:
                self_inner.path = p

        return _R(path)

    async def aread(self, path: str) -> Any:
        class _R:
            def __init__(self_inner, c: str) -> None:
                self_inner.content = c

        return _R(self._files.get(path, ""))


def _rankable(external_id: str = "x", db_id: int = 1) -> RankableApartment:
    apt = Apartment(
        source=Source.FOTOCASA,
        external_id=external_id,
        url=f"https://x/{external_id}",
        title=f"Apt {external_id}",
        price_eur=1000,
        rooms=2,
        bathrooms=2,
        size_m2=60,
        address="Test 1, Zaragoza",
        lat=41.65,
        lng=-0.88,
        description="x",
    )
    return RankableApartment(apartment=apt, db_id=db_id)


def _ranking(
    *, run_id: UUID | None = None, top_ids: list[int] | None = None
) -> dict[str, Any]:
    run_id = run_id or uuid4()
    top_ids = top_ids if top_ids is not None else [1]
    return {
        "ranking_run_id": run_id,
        "apartments_scored": len(top_ids),
        "scores_written": len(top_ids) * 3,
        "top": [
            {
                "apartment_id": apt_id,
                "score": 0.9 - 0.1 * i,
                "breakdown": [
                    {
                        "criterion": "distance_to_dangerous",
                        "score": 0.9,
                        "weight": 0.5,
                        "details": {"nearest_m": 1234},
                    },
                    {"criterion": "pet_policy", "score": 1.0, "weight": 0.3, "details": {}},
                    {"criterion": "furnished", "score": 0.8, "weight": 0.2, "details": {}},
                ],
            }
            for i, apt_id in enumerate(top_ids)
        ],
        "scores": [],
    }


@pytest.mark.asyncio
async def test_send_notification_sends_email_and_records_send():
    notifier = _FakeNotifier()
    ranking_repo = InMemoryRankingRepository()
    backend = _RecordingBackend()
    ranking = _ranking()
    rankable = _rankable()

    result = await send_notification(
        ranking=ranking,
        apartments_by_id={1: rankable},
        ranking_repo=ranking_repo,
        notifier=notifier,
        backend=backend,  # type: ignore[arg-type]
        from_address="me@gmail.com",
        to_address="me@gmail.com",
    )

    assert result.sent is True
    assert result.skipped_reason is None
    assert len(notifier.sent) == 1
    assert notifier.sent[0]["From"] == "me@gmail.com"
    assert "top 1" in notifier.sent[0]["Subject"]
    assert len(ranking_repo.notifications) == 1


@pytest.mark.asyncio
async def test_send_notification_skips_when_already_sent_today():
    notifier = _FakeNotifier()
    ranking_repo = InMemoryRankingRepository()
    backend = _RecordingBackend()
    ranking = _ranking()
    rankable = _rankable()

    # Pre-record a notification for today.
    await ranking_repo.record_send(
        ranking_run_id=uuid4(),
        sent_on=date.today(),
        apartment_ids=[1],
    )

    result = await send_notification(
        ranking=ranking,
        apartments_by_id={1: rankable},
        ranking_repo=ranking_repo,
        notifier=notifier,
        backend=backend,  # type: ignore[arg-type]
        from_address="me@gmail.com",
        to_address="me@gmail.com",
    )

    assert result.sent is False
    assert result.skipped_reason == "already sent today"
    assert notifier.sent == []  # no SMTP call
    assert len(ranking_repo.notifications) == 1  # unchanged


@pytest.mark.asyncio
async def test_send_notification_returns_empty_top_when_ranking_has_none():
    notifier = _FakeNotifier()
    ranking_repo = InMemoryRankingRepository()
    backend = _RecordingBackend()
    ranking = _ranking(top_ids=[])

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


@pytest.mark.asyncio
async def test_send_notification_writes_outbox_files():
    notifier = _FakeNotifier()
    ranking_repo = InMemoryRankingRepository()
    backend = _RecordingBackend()
    ranking = _ranking()
    rankable = _rankable()

    await send_notification(
        ranking=ranking,
        apartments_by_id={1: rankable},
        ranking_repo=ranking_repo,
        notifier=notifier,
        backend=backend,  # type: ignore[arg-type]
        from_address="me@gmail.com",
        to_address="me@gmail.com",
    )

    paths_written = [p for p, _ in backend.writes]
    assert any(p.endswith(".txt") for p in paths_written)
    assert any(p.endswith(".html") for p in paths_written)
    assert any(p.endswith(".log") for p in paths_written)


@pytest.mark.asyncio
async def test_send_notification_handles_smtp_error_gracefully():
    class _FailingNotifier(Notifier):
        async def send(self, message: EmailMessage) -> None:
            raise RuntimeError("smtp down")

    ranking_repo = InMemoryRankingRepository()
    backend = _RecordingBackend()
    ranking = _ranking()
    rankable = _rankable()

    result = await send_notification(
        ranking=ranking,
        apartments_by_id={1: rankable},
        ranking_repo=ranking_repo,
        notifier=_FailingNotifier(),
        backend=backend,  # type: ignore[arg-type]
        from_address="me@gmail.com",
        to_address="me@gmail.com",
    )

    assert result.sent is False
    assert result.skipped_reason and "smtp error" in result.skipped_reason
    # The dedup row was written pre-SMTP and rolled back on failure.
    assert len(ranking_repo.notifications) == 0
    # A retry should now be allowed: no row for today.
    from datetime import date

    from deep_apartment_finder.ports.ranking_repository import NotificationAlreadySent

    try:
        await ranking_repo.record_send(
            ranking_run_id=uuid4(),
            sent_on=date.today(),
            apartment_ids=[1],
        )
    except NotificationAlreadySent:
        pytest.fail("retry should be allowed after rollback")
