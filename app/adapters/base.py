"""Adapter contract for CRM integrations.

A :class:`CrmAdapter` is responsible for three things, and only three:

1. **Verifying the inbound webhook** (signature/HMAC/bearer auth).
2. **Parsing the CRM's payload** into a :class:`NormalizedWebhookEvent` that
   the engine's state machine can consume directly. This includes mapping the
   CRM's status labels/IDs onto our canonical :data:`LeadStatus` and
   :data:`OpportunityStatus` enums and extracting personalization custom
   fields.
3. **Fetching contact details** for a lead when the webhook didn't carry them
   (e.g. Close's opportunity webhooks). Returns ``(name, email, phone)``.

Everything downstream of the adapter — the state machine, scheduler, workflows,
templates — is provider-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Mapping, Protocol


EventKind = Literal["lead", "opportunity", "ignore"]


@dataclass
class NormalizedWebhookEvent:
    """Canonical representation of an inbound CRM webhook event.

    ``kind`` drives the dispatch:
        * ``"lead"``         → :func:`app.workflow.apply_lead_status_transition`
        * ``"opportunity"``  → :func:`app.workflow.apply_opportunity_status_transition`
        * ``"ignore"``       → ack and exit (use ``reason`` to explain)
    """

    kind: EventKind
    external_id: str = ""           # CRM lead identifier (e.g. Close "lead_xxx")
    # Status fields. ``new_status`` is the canonical enum value (str) already
    # mapped from the CRM-specific value; empty/None means no status change.
    new_status: str | None = None
    raw_status_id: str = ""
    raw_status_label: str = ""
    # Contact details (may be empty when only the opportunity is in the payload).
    name: str = ""
    email: str = ""
    phone: str = ""
    # Personalization carried in custom fields. Adapter pre-extracts these so
    # the routing layer doesn't have to know CRM-specific field IDs.
    call_at: datetime | None = None
    call_time: str = ""
    calendar_link: str = ""
    meeting_type: str = ""
    mrr: float | None = None
    business_type: str = ""
    lead_timezone: str = ""
    # Diagnostic / audit.
    reason: str = ""                 # human-readable reason for kind="ignore"
    raw: dict = field(default_factory=dict, repr=False)


class CrmAdapter(Protocol):
    """All CRM adapters MUST implement this protocol."""

    name: str  # short identifier, lowercase ("close", "hubspot", "salesforce")

    def verify_webhook(self, body: bytes, headers: Mapping[str, str]) -> bool:
        """Return True if the inbound webhook signature is valid (or skip-checking)."""
        ...

    def parse_event(self, payload: dict) -> NormalizedWebhookEvent:
        """Translate the CRM's webhook payload into a NormalizedWebhookEvent."""
        ...

    def fetch_contact(self, external_id: str) -> tuple[str, str, str]:
        """Fetch ``(name, email, phone)`` for ``external_id`` from the CRM API.

        Return empty strings when the CRM doesn't have the data or the API is
        unavailable. Should not raise on transient failures — the caller will
        decide whether to skip the event.
        """
        ...
