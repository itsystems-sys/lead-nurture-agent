"""Typed domain models for leads, workflows, jobs, and logs."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid4())


# ---------------------------------------------------------------------------
# Lead
# ---------------------------------------------------------------------------

LeadStatus = Literal[
    # Lead lifecycle (mirrors Close's LEAD statuses)
    "potential",          # potential/contacted: form submission, no call yet
    "nurture",            # re-engage track
    "qualified",          # sales-verified, taken over manually
    "client",             # converted to customer
    "unqualified",        # not a fit / disqualified
    "lost",               # didn't close
    "bad_fit",            # off-ICP
    "cancelled",          # withdrew
]


OpportunityStatus = Literal[
    # Per-opportunity state (mirrors Close's OPPORTUNITY statuses)
    "call_booked",
    "call_rescheduled",
    "call_cancelled",
    "call_completed",
    "contact_sent",
    "won",
    "lost",
    "follow_up",
]


class Lead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=_new_id)
    external_id: str = ""  # opaque ID from upstream CRM (e.g. Close lead_xxx)
    name: str
    email: EmailStr
    phone: str = ""
    status: LeadStatus = "potential"                       # Close LEAD status
    opportunity_status: OpportunityStatus | None = None    # latest OPPORTUNITY status

    # Optional booking context used by templates
    call_time: str = ""  # human-readable display string ("Thu May 21, 2:00 PM PT")
    call_at: datetime | None = None  # machine-readable timestamp for reminder scheduling
    calendar_link: str = ""
    case_study_link: str = ""

    # Personalization fields (used as Jinja variables and for TZ-aware rendering).
    timezone: str = ""              # IANA name, e.g. "America/Los_Angeles"
    meeting_type: str = ""          # e.g. "Demo Call", "Discovery Call"
    mrr: float = 0.0                # monthly recurring revenue (0 = unknown)
    business_type: str = ""         # e.g. "Agency", "Coaching", "B2B service"

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class LeadCreate(BaseModel):
    """Incoming payload for lead creation (webhook or manual)."""

    model_config = ConfigDict(extra="ignore")

    name: str
    email: EmailStr
    phone: str = ""
    status: LeadStatus = "potential"
    opportunity_status: OpportunityStatus | None = None
    external_id: str = ""
    call_time: str = ""
    call_at: datetime | None = None
    calendar_link: str = ""
    case_study_link: str = ""
    timezone: str = ""
    meeting_type: str = ""
    mrr: float = 0.0
    business_type: str = ""


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

StepType = Literal["email", "sms"]


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: StepType
    template: str  # template name (file stem under templates/)
    subject: str = ""  # only used for email

    # Exactly one anchor is used:
    #   delay_minutes        -> run_at = trigger_time + delay_minutes
    #   before_call_minutes  -> run_at = lead.call_at  - before_call_minutes
    # If both are omitted, behaves as delay_minutes=0 (fires immediately).
    delay_minutes: int | None = None
    before_call_minutes: int | None = None

    @model_validator(mode="after")
    def _exactly_one_anchor(self) -> "WorkflowStep":
        if self.delay_minutes is not None and self.before_call_minutes is not None:
            raise ValueError(
                "WorkflowStep must set either delay_minutes or before_call_minutes, not both"
            )
        return self


class Workflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    steps: list[WorkflowStep]


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


class JobStatus(str, Enum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=_new_id)
    lead_id: str
    workflow_name: str
    step_index: int
    step_type: StepType
    template: str
    subject: str = ""
    run_at: datetime
    status: JobStatus = JobStatus.SCHEDULED
    attempts: int = 0
    last_error: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# Log entry
# ---------------------------------------------------------------------------

LogLevel = Literal["INFO", "WARNING", "ERROR"]


class LogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=_new_id)
    ts: datetime = Field(default_factory=_utcnow)
    level: LogLevel = "INFO"
    event: str
    lead_id: str = ""
    job_id: str = ""
    message: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
