"""JSON API for booking webhooks, leads, jobs, and workflows."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from app import scheduler, storage
from app.adapters import CrmAdapter, NormalizedWebhookEvent
from app.adapters.close import adapter as close_adapter
from app.config import settings
from app.models import Job, Lead, LeadCreate, LeadStatus, OpportunityStatus
from app.workflow import (
    WorkflowNotFoundError,
    apply_lead_status_transition,
    apply_opportunity_status_transition,
    list_workflows,
    load_workflow,
    schedule_workflow_for_lead,
)


router = APIRouter(tags=["api"])


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Webhook intake
# ---------------------------------------------------------------------------


class BookingWebhookPayload(LeadCreate):
    workflow: str = "Booked Call Sequence"


@router.post("/webhooks/booking", status_code=status.HTTP_201_CREATED)
def booking_webhook(payload: BookingWebhookPayload) -> dict[str, Any]:
    lead = Lead(
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        status=payload.status,
        call_time=payload.call_time,
        call_at=payload.call_at,
        calendar_link=payload.calendar_link,
        case_study_link=payload.case_study_link,
    )
    storage.upsert_lead(lead)
    storage.log("lead.created", lead_id=lead.id, message=f"lead created via webhook: {lead.email}")

    try:
        workflow = load_workflow(payload.workflow)
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    jobs = schedule_workflow_for_lead(workflow, lead)
    for job in jobs:
        scheduler.schedule_job(job)
    return {"lead": lead.model_dump(mode="json"), "jobs": [j.model_dump(mode="json") for j in jobs]}


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


@router.get("/leads", response_model=list[Lead])
def list_leads() -> list[Lead]:
    return storage.load_leads()


@router.get("/leads/{lead_id}", response_model=Lead)
def get_lead(lead_id: str) -> Lead:
    lead = storage.get_lead(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    return lead


@router.post("/leads", response_model=Lead, status_code=status.HTTP_201_CREATED)
def create_lead(payload: LeadCreate) -> Lead:
    lead = Lead(**payload.model_dump())
    storage.upsert_lead(lead)
    storage.log("lead.created", lead_id=lead.id, message=f"lead created: {lead.email}")
    return lead


@router.delete("/leads/{lead_id}")
def delete_lead(lead_id: str) -> Response:
    removed = storage.delete_lead(lead_id)
    if not removed:
        raise HTTPException(status_code=404, detail="lead not found")
    storage.log("lead.deleted", lead_id=lead_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


@router.get("/workflows")
def get_workflows() -> list[dict[str, Any]]:
    return [w.model_dump(mode="json") for w in list_workflows()]


class TriggerWorkflowRequest(BaseModel):
    lead_id: str
    workflow: str = "Booked Call Sequence"


@router.post("/workflows/trigger", status_code=status.HTTP_201_CREATED)
def trigger_workflow(payload: TriggerWorkflowRequest) -> dict[str, Any]:
    lead = storage.get_lead(payload.lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    try:
        workflow = load_workflow(payload.workflow)
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    jobs = schedule_workflow_for_lead(workflow, lead)
    for job in jobs:
        scheduler.schedule_job(job)
    return {"jobs": [j.model_dump(mode="json") for j in jobs]}


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@router.get("/jobs", response_model=list[Job])
def list_jobs() -> list[Job]:
    jobs = storage.load_jobs()
    jobs.sort(key=lambda j: j.run_at)
    return jobs


@router.get("/jobs/{job_id}", response_model=Job)
def get_job(job_id: str) -> Job:
    job = storage.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    if not scheduler.cancel_job(job_id):
        raise HTTPException(status_code=400, detail="job not cancellable")
    job = storage.get_job(job_id)
    return {"job": job.model_dump(mode="json") if job else None}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str) -> dict[str, Any]:
    if not scheduler.retry_job(job_id):
        raise HTTPException(status_code=400, detail="job not retryable")
    job = storage.get_job(job_id)
    return {"job": job.model_dump(mode="json") if job else None}


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


@router.get("/logs")
def get_logs(limit: int = 200) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 1000))
    return [entry.model_dump(mode="json") for entry in storage.load_logs(limit=limit)]


# ---------------------------------------------------------------------------
# Status transition (generic, callable from any source)
# ---------------------------------------------------------------------------


class LeadStatusChangeRequest(BaseModel):
    status: LeadStatus
    call_at: datetime | None = None
    call_time: str | None = None
    calendar_link: str | None = None
    timezone_name: str | None = None
    meeting_type: str | None = None
    mrr: float | None = None
    business_type: str | None = None


class OpportunityStatusChangeRequest(BaseModel):
    status: OpportunityStatus
    call_at: datetime | None = None
    call_time: str | None = None
    calendar_link: str | None = None
    timezone_name: str | None = None
    meeting_type: str | None = None
    mrr: float | None = None
    business_type: str | None = None


@router.post("/leads/{lead_id}/lead-status")
def change_lead_lifecycle_status(
    lead_id: str, payload: LeadStatusChangeRequest
) -> dict[str, Any]:
    lead = storage.get_lead(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    result = apply_lead_status_transition(
        lead,
        payload.status,
        call_at=payload.call_at,
        call_time=payload.call_time,
        calendar_link=payload.calendar_link,
        timezone_name=payload.timezone_name,
        meeting_type=payload.meeting_type,
        mrr=payload.mrr,
        business_type=payload.business_type,
    )
    return {"lead_id": lead_id, **result}


@router.post("/leads/{lead_id}/opportunity-status")
def change_opportunity_status(
    lead_id: str, payload: OpportunityStatusChangeRequest
) -> dict[str, Any]:
    lead = storage.get_lead(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    result = apply_opportunity_status_transition(
        lead,
        payload.status,
        call_at=payload.call_at,
        call_time=payload.call_time,
        calendar_link=payload.calendar_link,
        timezone_name=payload.timezone_name,
        meeting_type=payload.meeting_type,
        mrr=payload.mrr,
        business_type=payload.business_type,
    )
    return {"lead_id": lead_id, **result}


# ---------------------------------------------------------------------------
# CRM webhook handler (provider-agnostic)
# ---------------------------------------------------------------------------


async def _handle_crm_webhook(adapter: CrmAdapter, request: Request) -> dict[str, Any]:
    """Generic CRM webhook handler.

    Each CRM route delegates here with its own adapter. The adapter does all
    provider-specific work (signature verification, payload parsing, status
    mapping); this function only handles the engine's deterministic state
    machine response.
    """
    body = await request.body()
    if not adapter.verify_webhook(body, dict(request.headers)):
        raise HTTPException(status_code=401, detail=f"invalid {adapter.name} signature")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc

    event = adapter.parse_event(payload)
    if event.kind == "ignore":
        storage.log(
            f"{adapter.name}.webhook.ignored",
            level="INFO",
            message=event.reason,
            context={
                "raw_status_id": event.raw_status_id,
                "raw_status_label": event.raw_status_label,
            },
        )
        return {"status": "ignored", "reason": event.reason}

    if not event.external_id:
        raise HTTPException(status_code=400, detail="event has no external lead id")

    lead = storage.get_lead_by_external_id(event.external_id)
    if lead is None:
        name, email, phone = event.name, event.email, event.phone
        if not email:
            # Opportunity payloads don't include contacts; fetch from the CRM.
            fetched_name, fetched_email, fetched_phone = adapter.fetch_contact(event.external_id)
            name = name or fetched_name
            email = email or fetched_email
            phone = phone or fetched_phone
        if not email:
            storage.log(
                f"{adapter.name}.webhook.no_email",
                level="WARNING",
                message="no email available; skipping lead creation",
                context={"external_id": event.external_id},
            )
            return {"status": "ignored", "reason": "no email available"}
        lead = Lead(
            name=name or "Unknown",
            email=email,  # type: ignore[arg-type]
            phone=phone,
            external_id=event.external_id,
            status="potential",
        )
        storage.upsert_lead(lead)
        storage.log(
            f"{adapter.name}.webhook.lead_created",
            lead_id=lead.id,
            message=f"lead created from {adapter.name} event: {email}",
            context={"external_id": event.external_id},
        )

    common_kwargs = dict(
        call_at=event.call_at,
        call_time=event.call_time or None,
        calendar_link=event.calendar_link or None,
        timezone_name=event.lead_timezone or None,
        meeting_type=event.meeting_type or None,
        mrr=event.mrr,
        business_type=event.business_type or None,
    )
    if event.kind == "lead":
        result = apply_lead_status_transition(lead, event.new_status, **common_kwargs)  # type: ignore[arg-type]
    else:  # event.kind == "opportunity"
        result = apply_opportunity_status_transition(lead, event.new_status, **common_kwargs)  # type: ignore[arg-type]
    return {"status": "ok", "lead_id": lead.id, **result}


# ---------------------------------------------------------------------------
# Close webhook endpoints
# ---------------------------------------------------------------------------
# All three accept Close events; the generic dispatcher route accepts any
# object_type from a single subscription; the dedicated /lead and /opportunity
# routes are kept for clarity and for setups that prefer separate subscriptions.


@router.post("/webhooks/close")
async def close_webhook(request: Request) -> dict[str, Any]:
    return await _handle_crm_webhook(close_adapter, request)


@router.post("/webhooks/close/lead")
async def close_lead_webhook(request: Request) -> dict[str, Any]:
    return await _handle_crm_webhook(close_adapter, request)


@router.post("/webhooks/close/opportunity")
async def close_opportunity_webhook(request: Request) -> dict[str, Any]:
    return await _handle_crm_webhook(close_adapter, request)
