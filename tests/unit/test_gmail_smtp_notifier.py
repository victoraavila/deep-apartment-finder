"""Tests for `GmailSmtpNotifier` (with a fake SMTP client).

We avoid spinning up `aiosmtpd` in unit tests; the SMTP dance is
a thin wrapper around Python's stdlib, so a recording fake is
enough. The contract: `notifier.send(message)` calls the injected
`SmtpClient.send_message` exactly once with the same message.
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from deep_apartment_finder.adapters.notifiers.gmail_smtp import GmailSmtpNotifier
from deep_apartment_finder.config import Settings


class _FakeSmtp:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send_message(self, message: EmailMessage) -> None:
        self.sent.append(message)


@pytest.mark.asyncio
async def test_gmail_smtp_notifier_delegates_to_injected_smtp_client():
    fake = _FakeSmtp()
    notifier = GmailSmtpNotifier(
        settings=Settings(
            gmail_smtp_address="me@gmail.com",
            gmail_smtp_app_password="abcdabcdabcdabcd",
        ),
        smtp=fake,  # type: ignore[arg-type]
    )
    msg = EmailMessage()
    msg["From"] = "me@gmail.com"
    msg["To"] = "me@gmail.com"
    msg["Subject"] = "test"
    msg.set_content("hello")

    await notifier.send(msg)

    assert fake.sent == [msg]


def test_gmail_smtp_notifier_refuses_to_construct_without_credentials():
    """No `GMAIL_SMTP_ADDRESS` / `GMAIL_SMTP_APP_PASSWORD` -> no SMTP client."""
    with pytest.raises(RuntimeError, match="not configured"):
        GmailSmtpNotifier(settings=Settings())


@pytest.mark.asyncio
async def test_gmail_smtp_notifier_uses_settings_defaults_when_no_injection():
    """The default SMTP client is built from `settings`. We assert
    construction succeeds with valid settings; we don't actually
    open a connection."""
    notifier = GmailSmtpNotifier(
        settings=Settings(
            gmail_smtp_address="me@gmail.com",
            gmail_smtp_app_password="abcdabcdabcdabcd",
        ),
    )
    # `send` will fail because we don't have a real Gmail account, but
    # the notifier must construct cleanly and the call must not raise
    # a `RuntimeError` about missing config.
    assert notifier is not None
