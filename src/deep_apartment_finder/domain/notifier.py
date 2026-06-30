"""Email rendering and sending (no LLM).

Per `docs/SPRINT2.md`, the notifier subagent is **deterministic**
in the hot path — no LLM call. The `send_notification` function:
1. Renders the body from the top-N list.
2. Asks the `Notifier` port to send.
3. Records the send (or catches `NotificationAlreadySent`).

The function is the single entry point the orchestrator uses after
the ranker. The LLM-driven subagent that wraps it (if we ever add
one) is just a thin shell that calls this function.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.message import EmailMessage
from typing import Any

from deepagents.backends.protocol import BackendProtocol

from deep_apartment_finder.domain.ranking import RankableApartment
from deep_apartment_finder.ports.notifier import Notifier
from deep_apartment_finder.ports.ranking_repository import (
    NotificationAlreadySent,
    RankingRepository,
)


def _now_utc() -> datetime:
    return datetime.now(UTC)


logger = logging.getLogger(__name__)

_OUTBOX_PREFIX = "/notifier/"


@dataclass(frozen=True, slots=True)
class NotificationResult:
    """What `send_notification` returns.

    - `sent`: True if a real email went out
    - `skipped_reason`: set when no email was sent (already sent today,
      empty top, etc.)
    - `subject`: rendered subject (for the run report)
    - `ranking_run_id`: the ranking run the top-N came from
    - `apartment_ids`: ids that would have been / were sent
    - `outbox_txt_path`, `outbox_html_path`: where the rendered body
      lives on disk
    """

    sent: bool
    skipped_reason: str | None
    subject: str
    ranking_run_id: str
    apartment_ids: list[int]
    outbox_txt_path: str
    outbox_html_path: str


async def send_notification(
    *,
    ranking: dict[str, Any],
    apartments_by_id: dict[int, RankableApartment],
    ranking_repo: RankingRepository,
    notifier: Notifier,
    backend: BackendProtocol,
    from_address: str,
    to_address: str,
    subject_prefix: str = "DAF",
    sent_on: date | None = None,
) -> NotificationResult:
    """Render the top-N email, send it (if appropriate), and record the send.

    Args:
        ranking: the dict returned by `compute_ranking`.
        apartments_by_id: map of db_id -> RankableApartment, used to
            enrich the email body with the apartment's fields.
        ranking_repo: for `record_send`.
        notifier: for `send`.
        backend: for writing the outbox.
        from_address, to_address: SMTP From/To.
        subject_prefix: prefix for the email subject.
        sent_on: the date the notification is recorded under; defaults
            to today in the process-local timezone.
    """
    sent_on = sent_on or date.today()
    top: list[dict[str, Any]] = list(ranking.get("top") or [])

    subject = f"{subject_prefix} top {len(top)} for {sent_on.isoformat()}"
    txt_path = f"{_OUTBOX_PREFIX}outbox/{sent_on.isoformat()}.txt"
    html_path = f"{_OUTBOX_PREFIX}outbox/{sent_on.isoformat()}.html"

    # Enrich each top row with the apartment fields the renderer needs.
    enriched: list[dict[str, Any]] = []
    for row in top:
        db_id = int(row["apartment_id"])
        rankable = apartments_by_id.get(db_id)
        if rankable is None:
            logger.warning("notifier: missing rankable for db_id=%s", db_id)
            continue
        apt = rankable.apartment
        enriched.append(
            {
                "apartment_id": db_id,
                "score": row["score"],
                "breakdown": row.get("breakdown") or [],
                "apartment": {
                    "url": apt.url,
                    "title": apt.title,
                    "price_eur": float(apt.price_eur) if apt.price_eur is not None else None,
                    "size_m2": float(apt.size_m2) if apt.size_m2 is not None else None,
                    "rooms": apt.rooms,
                    "bathrooms": apt.bathrooms,
                    "address": apt.address,
                },
            }
        )

    plain = _format_plain(enriched, sent_on)
    html = _format_html(enriched, sent_on)

    ranking_run_id = ranking["ranking_run_id"]
    apartment_ids = [int(row["apartment_id"]) for row in top]

    # Always write the outbox; the operator wants a copy on disk
    # even when no email goes out.
    try:
        await backend.awrite(txt_path, plain)
        await backend.awrite(html_path, html)
    except Exception as exc:  # noqa: BLE001
        logger.warning("notifier: outbox write failed: %s", exc)

    # Always append a log line; it goes to the persistent store so
    # the operator can grep across days.
    try:
        log_path = f"{_OUTBOX_PREFIX}logs/{sent_on.isoformat()}.log"
        log_line = (
            f"{_now_utc().isoformat()}\t"
            f"to={to_address}\t"
            f"subject={subject!r}\t"
            f"ranking_run_id={ranking_run_id}\t"
            f"apartment_ids={apartment_ids}\n"
        )
        existing = ""
        try:
            existing_obj = await backend.aread(log_path)
            existing = (
                existing_obj.content
                if hasattr(existing_obj, "content")
                else str(existing_obj)
            )
        except Exception:  # noqa: BLE001
            existing = ""
        await backend.awrite(log_path, existing + log_line)
    except Exception as exc:  # noqa: BLE001
        logger.warning("notifier: log write failed: %s", exc)

    if not apartment_ids:
        return NotificationResult(
            sent=False,
            skipped_reason="empty top",
            subject=subject,
            ranking_run_id=str(ranking_run_id),
            apartment_ids=[],
            outbox_txt_path=txt_path,
            outbox_html_path=html_path,
        )

    # Dedup: check the partial unique index BEFORE we hit SMTP. The
    # index is the source of truth, but checking it first avoids
    # sending a second email on a same-day re-run.
    try:
        await ranking_repo.record_send(
            ranking_run_id=ranking_run_id,
            sent_on=sent_on,
            apartment_ids=apartment_ids,
        )
    except NotificationAlreadySent:
        logger.info("notifier: already sent today (%s); skipping", sent_on)
        return NotificationResult(
            sent=False,
            skipped_reason="already sent today",
            subject=subject,
            ranking_run_id=str(ranking_run_id),
            apartment_ids=apartment_ids,
            outbox_txt_path=txt_path,
            outbox_html_path=html_path,
        )

    # Send the email.
    message = _build_message(
        from_address=from_address,
        to_address=to_address,
        subject=subject,
        plain=plain,
        html=html,
    )
    try:
        await notifier.send(message)
    except Exception as exc:  # noqa: BLE001
        logger.error("notifier: SMTP send failed: %s", exc)
        # Roll back the dedup row so a re-run can retry. We use the
        # partial unique index on `sent_on`; deleting by `sent_on`
        # is enough to free today's slot.
        try:
            await _delete_send(ranking_repo, sent_on)
        except Exception as cleanup_exc:  # noqa: BLE001
            logger.warning(
                "notifier: failed to roll back dedup row: %s", cleanup_exc
            )
        return NotificationResult(
            sent=False,
            skipped_reason=f"smtp error: {exc}",
            subject=subject,
            ranking_run_id=str(ranking_run_id),
            apartment_ids=apartment_ids,
            outbox_txt_path=txt_path,
            outbox_html_path=html_path,
        )

    return NotificationResult(
        sent=True,
        skipped_reason=None,
        subject=subject,
        ranking_run_id=str(ranking_run_id),
        apartment_ids=apartment_ids,
        outbox_txt_path=txt_path,
        outbox_html_path=html_path,
    )


async def _delete_send(ranking_repo: RankingRepository, sent_on: date) -> None:
    """Delete the dedup row for `sent_on`. Sprint 2 ships this only on
    the SMTP-failure path; a real retry-orchestrator can be added in
    a future sprint without changing the public API.
    """
    if hasattr(ranking_repo, "delete_send_for_date"):
        await ranking_repo.delete_send_for_date(sent_on)


def _build_message(
    *, from_address: str, to_address: str, subject: str, plain: str, html: str
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_address
    msg["To"] = to_address
    msg["Subject"] = subject
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    return msg


def _format_plain(top: list[dict[str, Any]], sent_on: date) -> str:
    lines: list[str] = []
    lines.append(f"Deep Apartment Finder — top {len(top)} for {sent_on.isoformat()}")
    lines.append("=" * 60)
    lines.append("")
    for i, row in enumerate(top, start=1):
        apt = row.get("apartment") or {}
        score = row.get("score")
        url = apt.get("url") or "(no url)"
        title = (apt.get("title") or "(no title)").strip()
        price = apt.get("price_eur")
        size = apt.get("size_m2")
        rooms = apt.get("rooms")
        bathrooms = apt.get("bathrooms")
        address = apt.get("address") or ""
        breakdown = row.get("breakdown") or []

        head = f"#{i}  score={score:.2f}  {title}"
        lines.append(head)
        if price is not None:
            lines.append(f"     price: EUR {price}")
        if size is not None:
            lines.append(f"     size: {size} m^2")
        if rooms is not None or bathrooms is not None:
            lines.append(f"     rooms/bathrooms: {rooms}/{bathrooms}")
        if address:
            lines.append(f"     address: {address}")
        lines.append(f"     url: {url}")
        for b in breakdown:
            det = b.get("details") or {}
            det_str = " ".join(f"{k}={v}" for k, v in det.items()) if det else ""
            lines.append(
                f"       - {b.get('criterion')}: {b.get('score'):.2f} "
                f"(w={b.get('weight'):.2f}) {det_str}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _format_html(top: list[dict[str, Any]], sent_on: date) -> str:
    parts: list[str] = []
    parts.append(
        f"<html><body><h2>Deep Apartment Finder &mdash; "
        f"top {len(top)} for {sent_on.isoformat()}</h2>"
    )
    for i, row in enumerate(top, start=1):
        apt = row.get("apartment") or {}
        score = row.get("score")
        url = apt.get("url") or "#"
        title = (apt.get("title") or "(no title)").strip()
        price = apt.get("price_eur")
        size = apt.get("size_m2")
        rooms = apt.get("rooms")
        bathrooms = apt.get("bathrooms")
        address = apt.get("address") or ""
        breakdown = row.get("breakdown") or []

        parts.append("<hr/>")
        parts.append(f"<h3>#{i} &mdash; score {score:.2f} &mdash; {title}</h3>")
        parts.append("<ul>")
        if price is not None:
            parts.append(f"<li>price: EUR {price}</li>")
        if size is not None:
            parts.append(f"<li>size: {size} m^2</li>")
        if rooms is not None or bathrooms is not None:
            parts.append(f"<li>rooms/bathrooms: {rooms}/{bathrooms}</li>")
        if address:
            parts.append(f"<li>address: {address}</li>")
        parts.append(f'<li><a href="{url}">listing</a></li>')
        parts.append("</ul>")
        parts.append("<ul>")
        for b in breakdown:
            det = b.get("details") or {}
            det_str = " ".join(f"{k}={v}" for k, v in det.items()) if det else ""
            parts.append(
                f"<li>{b.get('criterion')}: {b.get('score'):.2f} "
                f"(w={b.get('weight'):.2f}) {det_str}</li>"
            )
        parts.append("</ul>")
    parts.append("</body></html>")
    return "".join(parts)


__all__ = ["NotificationResult", "send_notification"]
