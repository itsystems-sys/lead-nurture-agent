"""Close CRM adapter.

Implements :class:`app.adapters.base.CrmAdapter`. Everything Close-specific
lives here: signature verification, webhook payload parsing, status mapping,
custom-field extraction, and lead-detail fetches against Close's REST API.

Reference: https://developer.close.com/
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Mapping

from app.adapters.base import CrmAdapter, NormalizedWebhookEvent
from app.config import settings
from app.models import LeadStatus, OpportunityStatus


# ---------------------------------------------------------------------------
# Status label fallbacks (used when stat_xxx IDs aren't configured in .env)
# ---------------------------------------------------------------------------


CLOSE_OPP_LABEL_MAP: dict[str, OpportunityStatus] = {
    "call booked": "call_booked",
    "call rescheduled": "call_rescheduled",
    "call cancelled": "call_cancelled",
    "call canceled": "call_cancelled",
    "call completed": "call_completed",
    "contact sent": "contact_sent",
    "won": "won",
    "lost": "lost",
    "follow up": "follow_up",
}


CLOSE_LEAD_LABEL_MAP: dict[str, LeadStatus] = {
    "potential/contacted": "potential",
    "potential": "potential",
    "contacted": "potential",
    "nurture": "nurture",
    "qualified": "qualified",
    "client": "client",
    "unqualified": "unqualified",
    "lost": "lost",
    "bad fit": "bad_fit",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}


def _opportunity_status_from_close(status_id: str, status_label: str) -> OpportunityStatus | None:
    """Map Close opportunity (id, label) → our OpportunityStatus."""
    id_map: dict[str, OpportunityStatus] = {
        settings.close_status_call_booked: "call_booked",
        settings.close_status_call_rescheduled: "call_rescheduled",
        settings.close_status_call_cancelled: "call_cancelled",
        settings.close_status_call_completed: "call_completed",
        settings.close_status_contact_sent: "contact_sent",
        settings.close_status_won: "won",
        settings.close_status_opportunity_lost: "lost",
        settings.close_status_follow_up: "follow_up",
    }
    if status_id and status_id in id_map and id_map[status_id]:
        return id_map[status_id]
    if status_label:
        return CLOSE_OPP_LABEL_MAP.get(status_label.lower())
    return None


def _lead_status_from_close(status_id: str, status_label: str) -> LeadStatus | None:
    """Map Close lead (id, label) → our LeadStatus."""
    id_map: dict[str, LeadStatus] = {
        settings.close_lead_status_potential: "potential",
        settings.close_lead_status_nurture: "nurture",
        settings.close_lead_status_qualified: "qualified",
        settings.close_lead_status_client: "client",
        settings.close_lead_status_unqualified: "unqualified",
        settings.close_lead_status_lost: "lost",
        settings.close_lead_status_bad_fit: "bad_fit",
        settings.close_lead_status_cancelled: "cancelled",
    }
    if status_id and status_id in id_map and id_map[status_id]:
        return id_map[status_id]
    if status_label:
        return CLOSE_LEAD_LABEL_MAP.get(status_label.lower())
    return None


# ---------------------------------------------------------------------------
# Custom-field extraction
# ---------------------------------------------------------------------------


def _extract_custom_field(obj: dict[str, Any], field_id: str) -> Any:
    """Tolerate both ``custom: {cf_xxx: val}`` and ``custom.cf_xxx: val`` shapes."""
    if not field_id:
        return None
    custom = obj.get("custom") or {}
    if field_id in custom:
        return custom[field_id]
    return obj.get(f"custom.{field_id}")


def _extract_booking_datetime(obj: dict[str, Any]) -> datetime | None:
    raw = _extract_custom_field(obj, settings.close_cf_booking_date)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_text(obj: dict[str, Any], field_id: str) -> str:
    raw = _extract_custom_field(obj, field_id)
    return str(raw).strip() if raw else ""


def _extract_float(obj: dict[str, Any], field_id: str) -> float | None:
    raw = _extract_custom_field(obj, field_id)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Contact extraction (for lead-shaped payloads)
# ---------------------------------------------------------------------------


def _extract_contact(lead_data: dict[str, Any]) -> tuple[str, str, str]:
    """Pull (name, email, phone) from a Close lead object's ``contacts`` array."""
    name = lead_data.get("display_name") or lead_data.get("name") or ""
    email = ""
    phone = ""
    contacts = lead_data.get("contacts") or []
    if contacts:
        primary = contacts[0]
        if not name:
            name = primary.get("name") or ""
        emails = primary.get("emails") or []
        phones = primary.get("phones") or []
        if emails:
            email = emails[0].get("email", "")
        if phones:
            phone = phones[0].get("phone", "")
    return name, email, phone


# ---------------------------------------------------------------------------
# REST API client
# ---------------------------------------------------------------------------


def _basic_auth_header() -> str:
    creds = f"{settings.close_api_key}:".encode("ascii")
    return "Basic " + base64.b64encode(creds).decode("ascii")


def _fetch_lead(close_lead_id: str) -> dict[str, Any] | None:
    """GET /lead/{id}/ from Close. Returns None on failure (caller logs)."""
    if not (settings.close_api_key and close_lead_id):
        return None
    url = f"{settings.close_base_url.rstrip('/')}/lead/{close_lead_id}/"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": _basic_auth_header(),
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None


# ---------------------------------------------------------------------------
# Adapter implementation
# ---------------------------------------------------------------------------


class CloseAdapter:
    """Close CRM adapter."""

    name = "close"

    def verify_webhook(self, body: bytes, headers: Mapping[str, str]) -> bool:
        """Verify Close's HMAC-SHA256 signature.

        Close sends ``Close-Sig-Signature`` and ``Close-Sig-Timestamp`` headers.
        The signing payload is ``timestamp + body``. If
        :data:`settings.close_webhook_secret` is empty, signature checking is
        treated as advisory and returns True (callers should log).
        """
        if not settings.close_webhook_secret:
            return True
        sig = headers.get("Close-Sig-Signature") or headers.get("close-sig-signature") or ""
        ts = headers.get("Close-Sig-Timestamp") or headers.get("close-sig-timestamp") or ""
        if not (sig and ts):
            return False
        message = (ts + body.decode("utf-8", errors="replace")).encode("utf-8")
        expected = hmac.new(
            settings.close_webhook_secret.encode("utf-8"),
            message,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, sig)

    def parse_event(self, payload: dict) -> NormalizedWebhookEvent:
        event = payload.get("event") or {}
        object_type = event.get("object_type")
        action = event.get("action")
        data = event.get("data") or {}
        changed_fields = event.get("changed_fields") or []

        # Only act on create/update for leads + opportunities.
        if object_type not in ("lead", "opportunity") or action not in ("created", "updated"):
            return NormalizedWebhookEvent(
                kind="ignore",
                reason=f"unhandled event {object_type}.{action}",
                raw=payload,
            )

        status_id = (data.get("status_id") or "").strip()
        status_label = (data.get("status_label") or "").strip()

        if not status_id and not status_label:
            return NormalizedWebhookEvent(kind="ignore", reason="no status on payload", raw=payload)

        # Skip non-status edits on updates (avoid re-triggering workflows when
        # only a custom field changed).
        if (
            action == "updated"
            and changed_fields
            and not any(f in changed_fields for f in ("status_id", "status_label"))
        ):
            return NormalizedWebhookEvent(
                kind="ignore",
                reason="status not in changed_fields",
                raw=payload,
            )

        # Common personalization extraction (works on both lead and opp payloads).
        booking_at = _extract_booking_datetime(data)
        call_time = _extract_text(data, settings.close_cf_call_time)
        calendar_link = _extract_text(data, settings.close_cf_calendly_link)
        lead_timezone = _extract_text(data, settings.close_cf_lead_timezone)
        meeting_type = _extract_text(data, settings.close_cf_meeting_type)
        mrr = _extract_float(data, settings.close_cf_mrr)
        business_type = _extract_text(data, settings.close_cf_business_type)

        if object_type == "opportunity":
            new_status = _opportunity_status_from_close(status_id, status_label)
            if new_status is None:
                return NormalizedWebhookEvent(
                    kind="ignore",
                    reason=f"unmapped opportunity status (id={status_id!r}, label={status_label!r})",
                    raw_status_id=status_id,
                    raw_status_label=status_label,
                    raw=payload,
                )
            close_lead_id = data.get("lead_id") or ""
            return NormalizedWebhookEvent(
                kind="opportunity",
                external_id=close_lead_id,
                new_status=new_status,
                raw_status_id=status_id,
                raw_status_label=status_label,
                call_at=booking_at,
                call_time=call_time,
                calendar_link=calendar_link,
                meeting_type=meeting_type,
                mrr=mrr,
                business_type=business_type,
                lead_timezone=lead_timezone,
                raw=payload,
            )

        # lead.* event
        new_status = _lead_status_from_close(status_id, status_label)
        if new_status is None:
            return NormalizedWebhookEvent(
                kind="ignore",
                reason=f"unmapped lead status (id={status_id!r}, label={status_label!r})",
                raw_status_id=status_id,
                raw_status_label=status_label,
                raw=payload,
            )

        close_lead_id = data.get("id") or ""
        # Lead payloads include contacts inline.
        name, email, phone = _extract_contact(data)
        return NormalizedWebhookEvent(
            kind="lead",
            external_id=close_lead_id,
            new_status=new_status,
            raw_status_id=status_id,
            raw_status_label=status_label,
            name=name,
            email=email,
            phone=phone,
            call_at=booking_at,
            call_time=call_time,
            calendar_link=calendar_link,
            meeting_type=meeting_type,
            mrr=mrr,
            business_type=business_type,
            lead_timezone=lead_timezone,
            raw=payload,
        )

    def fetch_contact(self, external_id: str) -> tuple[str, str, str]:
        lead = _fetch_lead(external_id)
        if lead is None:
            return ("", "", "")
        return _extract_contact(lead)


adapter: CloseAdapter = CloseAdapter()
