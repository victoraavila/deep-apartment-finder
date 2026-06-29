"""Notifier port.

The notifier subagent renders an email body and asks the `Notifier`
port to send it. The concrete `GmailSmtpNotifier` lives in
`adapters/notifiers/gmail_smtp.py`. A future sprint can add
`ResendNotifier` / `TwilioNotifier` behind the same port.

The port's surface is *one* call: `send_email`. The notifier subagent
decides the subject, body, recipient. The port is intentionally not
prescriptive about MIME structure — we accept a pre-rendered
`EmailMessage` (Python's stdlib type) so the adapter doesn't have to
re-parse headers and bodies. The trade-off: the notifier is coupled
to the stdlib's email types, but the alternative (a dict shape) is
strictly worse for a Sprint 2 that's already on the stdlib path.
"""

from __future__ import annotations

from email.message import EmailMessage
from typing import Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    async def send(self, message: EmailMessage) -> None:
        """Send a pre-rendered email. Raises on SMTP failure."""
        ...


__all__ = ["Notifier"]
