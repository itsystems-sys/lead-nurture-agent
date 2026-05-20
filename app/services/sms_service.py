"""SMS dispatch.

Providers:
    * ``log``    - records the rendered SMS in logs.json (default).
    * ``twilio`` - sends via Twilio's REST API (sync HTTP).
    * ``none``   - hard-disables SMS (raises on attempt).

Deterministic selection via ``settings.sms_provider``.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from app.config import settings


class SmsSendError(RuntimeError):
    """Raised when an SMS send fails."""


_TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def send_sms(*, to: str, body: str) -> dict[str, str]:
    provider = settings.sms_provider

    if provider == "log":
        return {"provider": "log", "to": to, "status": "logged"}

    if provider == "none":
        raise SmsSendError("SMS provider is disabled")

    if provider == "twilio":
        if not (
            settings.twilio_account_sid
            and settings.twilio_auth_token
            and settings.twilio_from_number
        ):
            raise SmsSendError("twilio credentials are not configured")
        url = _TWILIO_URL.format(sid=settings.twilio_account_sid)
        form = urllib.parse.urlencode(
            {"To": to, "From": settings.twilio_from_number, "Body": body}
        ).encode("utf-8")
        creds = f"{settings.twilio_account_sid}:{settings.twilio_auth_token}".encode("ascii")
        auth = base64.b64encode(creds).decode("ascii")
        request = urllib.request.Request(
            url,
            data=form,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SmsSendError(f"twilio HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise SmsSendError(f"twilio network error: {exc}") from exc
        return {
            "provider": "twilio",
            "to": to,
            "status": payload.get("status", "queued"),
            "sid": payload.get("sid", ""),
        }

    raise SmsSendError(f"unsupported sms provider: {provider}")
