# ADR-008 — Gmail SMTP notifier

- Status: Accepted
- Sprint: 2
- Date: 2026-06-29

## Context

`SPRINT2.md` adds a daily notification: at most one email per day
with the top-5 ranked apartments. The notification is a
must-have for the "daily useful product" goal.

Free-tier candidates evaluated:

- **Resend** (email, 3k/month free). Pro: 5 lines of Python via
  the `resend` SDK, no SMTP plumbing, no SMTP credentials to
  manage. Con: third-party SaaS, free tier subject to change,
  account setup required.
- **Gmail SMTP** (email, free). Pro: tied to the operator's
  personal Gmail, no third party, no SDK, Python's stdlib
  `smtplib` is enough. Con: requires 2FA + App Password
  (operator setup), less robust for production (Gmail sending
  limits), ISP may block port 465.
- **Twilio** (SMS, paid-ish). Pro: SMS is harder to miss than
  email. Con: not free in practice, and Sprint 2's user is one
  person — they want email, not SMS.
- **Self-hosted SMTP** (postfix, etc.). Pro: zero external
  dep. Con: VPS / DNS / deliverability; out of scope for
  Sprint 2.

The user explicitly chose **Gmail SMTP** for Sprint 2.

## Decision

`GmailSmtpNotifier` implements the `Notifier` port. It uses
Python's stdlib `smtplib.SMTP_SSL` against `smtp.gmail.com:465`
with an App Password. No new runtime dependency.

The App Password is generated in the operator's Google account
security settings (2FA must be on). The address is the Gmail
login that owns the App Password. The recipient defaults to the
same address but can be overridden via `NOTIFY_TO_ADDRESS`.

The `Notifier` port accepts a pre-rendered `EmailMessage` (Python
stdlib type), so the renderer is decoupled from the SMTP dance.

## Consequences

- The CLI refuses to send without `GMAIL_SMTP_ADDRESS` and
  `GMAIL_SMTP_APP_PASSWORD` set. The `run` command still
  completes (it logs the SMTP error and continues), so a
  misconfigured notifier does not block the ranker.
- One `notifications` row per day is enforced by a partial
  unique index on `notifications(sent_on)`. Re-runs are
  safe: the notifier catches `NotificationAlreadySent` and
  logs "already notified today".
- Gmail's sending limits (≈500/day for a regular account) are
  not a concern for a daily-cadence personal agent.
- Swapping to Resend or Twilio is a single new class
  implementing `Notifier`; the orchestrator and the renderer
  do not change.
- App Passwords are sensitive. They live in `.env` (gitignored)
  and are never logged.

## Out of scope (deferred)

- Retry / backoff on SMTP failure. The cron re-fires the next day.
- Multi-recipient notifications. The Sprint 2 contract is
  one recipient.
- TLS certificate pinning. We trust the system trust store.
- Send-through-a-relay. If the operator moves to a VPS where
  port 465 is blocked, the port is one env var to change
  (`GMAIL_SMTP_HOST` / `GMAIL_SMTP_PORT`).
