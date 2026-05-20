"""Email dispatch.

Two providers:
    * ``log``  - records the rendered email to logs.json (default, used in dev/test).
    * ``smtp`` - sends via configured SMTP relay.

The choice is deterministic: it comes from ``settings.email_provider``.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.config import settings


class EmailSendError(RuntimeError):
    """Raised when an email send fails."""


def send_email(
    *, to: str, subject: str, body: str, body_html: str | None = None
) -> dict[str, str]:
    """Send an email via the configured provider.

    ``body`` is always the plain-text version. When ``body_html`` is provided,
    the message is sent as ``multipart/alternative`` so clients render the
    HTML and fall back to plain text where needed.

    Returns a small dict describing the dispatch for log/audit purposes.
    Raises :class:`EmailSendError` on failure (so the scheduler can retry).
    """
    provider = settings.email_provider

    if provider == "log":
        return {"provider": "log", "to": to, "subject": subject, "status": "logged"}

    if provider == "smtp":
        if not settings.smtp_host:
            raise EmailSendError("smtp_host is not configured")
        message = EmailMessage()
        message["From"] = settings.smtp_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        if body_html is not None:
            message.add_alternative(body_html, subtype="html")

        # Port 465 = implicit TLS (SMTPS); anything else with smtp_use_tls=true uses STARTTLS.
        use_implicit_tls = settings.smtp_port == 465
        try:
            if use_implicit_tls:
                smtp_cls = smtplib.SMTP_SSL
                smtp = smtp_cls(settings.smtp_host, settings.smtp_port, timeout=30)
            else:
                smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
            with smtp:
                if not use_implicit_tls and settings.smtp_use_tls:
                    smtp.starttls()
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise EmailSendError(f"SMTP send failed: {exc}") from exc
        return {"provider": "smtp", "to": to, "subject": subject, "status": "sent"}

    raise EmailSendError(f"unsupported email provider: {provider}")
