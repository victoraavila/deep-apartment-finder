"""Gmail SMTP notifier.

Uses Python's stdlib `smtplib.SMTP_SSL` against `smtp.gmail.com:465`
with an App Password. The operator generates the App Password in
their Google account security settings (2FA must be on).

No new runtime dependency — stdlib only.

The notifier is intentionally thin: the caller (the notifier
subagent) renders the `EmailMessage` and hands it over. We only do
the SMTP dance.

`SMTP` is a tiny Protocol so tests can inject a fake without spinning
up `aiosmtpd`. The default `SmtpClient` opens the SSL connection,
authenticates, sends the message, and quits.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol, runtime_checkable

from deep_apartment_finder.config import Settings
from deep_apartment_finder.ports.notifier import Notifier

logger = logging.getLogger(__name__)


@runtime_checkable
class SmtpClient(Protocol):
    def send_message(self, message: EmailMessage) -> None: ...


class _GmailSmtpClient:
    """Default `SmtpClient`: opens an SSL connection, logs in, sends."""

    def __init__(self, host: str, port: int, username: str, password: str) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password

    def send_message(self, message: EmailMessage) -> None:
        with smtplib.SMTP_SSL(self._host, self._port) as smtp:
            smtp.login(self._username, self._password)
            smtp.send_message(message)


class GmailSmtpNotifier(Notifier):
    """Concrete `Notifier` backed by Gmail SMTP."""

    def __init__(
        self,
        *,
        settings: Settings,
        smtp: SmtpClient | None = None,
    ) -> None:
        self._settings = settings
        if smtp is None:
            if not settings.gmail_smtp_app_password or not settings.gmail_smtp_address:
                raise RuntimeError(
                    "Gmail SMTP notifier is not configured: set "
                    "GMAIL_SMTP_ADDRESS and GMAIL_SMTP_APP_PASSWORD in .env"
                )
            smtp = _GmailSmtpClient(
                host=settings.gmail_smtp_host,
                port=settings.gmail_smtp_port,
                username=settings.gmail_smtp_address,
                password=settings.gmail_smtp_app_password,
            )
        self._smtp = smtp

    async def send(self, message: EmailMessage) -> None:
        # `smtplib` is blocking; we run it in the default thread pool so
        # the async event loop isn't blocked. The operation is fast
        # enough (<1s typical) that this is fine.
        import asyncio

        await asyncio.to_thread(self._smtp.send_message, message)
        logger.info(
            "gmail smtp: sent message subject=%r to=%s",
            message.get("Subject"),
            message.get("To"),
        )


__all__ = ["GmailSmtpNotifier", "SmtpClient"]
