"""Workflow engine.

Responsibilities:
    * Load workflow definitions from ``app/sequences/*.json``.
    * Render templates with the deterministic, explicit variable set:
      ``lead_name``, ``call_time``, ``calendar_link``, ``case_study_link``.
    * Schedule each step's send through the scheduler.
    * Execute the actual send when the scheduler fires (with retries).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound, select_autoescape

from app.config import settings
from app.models import Job, JobStatus, Lead, LeadStatus, OpportunityStatus, Workflow, WorkflowStep
from app.services.email_service import EmailSendError, send_email
from app.services.sms_service import SmsSendError, send_sms
from app import storage


def format_call_time(dt: datetime, tz_name: str | None = None) -> str:
    """Render a datetime as a friendly display string for use in templates.

    Output: ``"Thu May 21, 3:00 PM PDT"`` (uses configured display timezone,
    falls back to UTC if the name is invalid).
    """
    name = tz_name or "UTC"
    try:
        tz = ZoneInfo(name)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(tz)
    abbrev = local.strftime("%Z") or local.strftime("%z")
    hour_12 = local.hour % 12 or 12
    am_pm = "AM" if local.hour < 12 else "PM"
    date_part = local.strftime("%a %b %d")
    return f"{date_part}, {hour_12}:{local.minute:02d} {am_pm} {abbrev}".strip()


NURTURE_WORKFLOW = "Lead Nurture Sequence"
BOOKED_CALL_WORKFLOW = "Booked Call Sequence"

# Lead-lifecycle status → workflow to trigger (None = cancel only).
# NURTURE IS DISABLED FOR AUTO-TRIGGER FOR NOW. Re-enable by setting the desired
# statuses' values to NURTURE_WORKFLOW once you've decided which transitions
# should kick off a nurture sequence.
LEAD_STATUS_WORKFLOWS: dict[str, str | None] = {
    "potential": None,
    "nurture": None,
    "qualified": None,
    "client": None,
    "unqualified": None,
    "lost": None,
    "bad_fit": None,
    "cancelled": None,
}

# Opportunity status → workflow to trigger (None = cancel only).
OPPORTUNITY_STATUS_WORKFLOWS: dict[str, str | None] = {
    "call_booked": BOOKED_CALL_WORKFLOW,
    "call_rescheduled": BOOKED_CALL_WORKFLOW,   # cancel + re-trigger w/ new call_at
    "call_cancelled": None,
    "call_completed": None,
    "contact_sent": None,
    "won": None,
    "lost": None,
    "follow_up": None,
}

# Terminal statuses → cancel ALL workflows (lead is closed-out one way or another).
# Non-terminal transitions cancel only their own workflow's jobs so the other
# axis (lead vs opportunity) is never accidentally wiped by ordering.
TERMINAL_LEAD_STATUSES = frozenset({"client", "unqualified", "lost", "bad_fit", "cancelled"})
TERMINAL_OPPORTUNITY_STATUSES = frozenset({"won", "lost"})


ALLOWED_TEMPLATE_VARS = (
    "lead_name",
    "call_time",
    "calendar_link",
    "case_study_link",
    "meeting_type",
    "mrr",
    "business_type",
    "timezone",
)


def sample_lead() -> Lead:
    """A fully-populated Lead used by the template editor preview/validation.

    Pulls the org-wide defaults from settings for calendar_link and
    case_study_link so the preview reflects what real emails will look like
    when the per-lead Close fields are empty (the common case).
    """
    from app.config import settings as _settings
    return Lead(
        name="Jane Doe",
        email="jane.doe@example.com",
        phone="+15555550100",
        status="qualified",
        opportunity_status="call_booked",
        call_time="Thu May 21, 3:00 PM PDT",
        calendar_link=_settings.app_default_calendar_link or "https://cal.example.com/sample/intro",
        case_study_link=_settings.app_default_case_study_link or "https://example.com/case-study",
        meeting_type="Demo Call",
        mrr=4500.0,
        business_type="Agency",
        timezone="America/Los_Angeles",
    )


def render_template_string(body: str, lead: Lead) -> str:
    """Render an ad-hoc template body against a Lead (used by the UI editor preview)."""
    template = _jinja_env.from_string(body)
    return template.render(**_build_context(lead))


def validate_template_string(body: str, lead: Lead) -> str | None:
    """Return ``None`` if the body renders cleanly, else a human-readable error.

    Combines syntax and undefined-variable checks because StrictUndefined only
    fires at render time, not parse time.
    """
    try:
        render_template_string(body, lead)
    except Exception as exc:  # noqa: BLE001 - want to surface any rendering issue
        return f"{type(exc).__name__}: {exc}"
    return None


# ---------------------------------------------------------------------------
# Workflow loading
# ---------------------------------------------------------------------------


class WorkflowNotFoundError(LookupError):
    """Raised when a workflow JSON file does not exist."""


def _sequence_path(name: str) -> Path:
    # Map workflow names to filenames: "Booked Call Sequence" -> booked_call.json
    # Also accept exact filename stems ("booked_call").
    stem = name.strip().lower().replace(" ", "_")
    if stem.endswith("_sequence"):
        stem = stem[: -len("_sequence")]
    return settings.sequences_dir / f"{stem}.json"


def load_workflow(name: str) -> Workflow:
    path = _sequence_path(name)
    if not path.exists():
        # Fallback: scan and match by ``name`` field inside each JSON file.
        for candidate in settings.sequences_dir.glob("*.json"):
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, dict) and data.get("name") == name:
                return Workflow.model_validate(data)
        raise WorkflowNotFoundError(f"workflow not found: {name}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return Workflow.model_validate(data)


def list_workflows() -> list[Workflow]:
    flows: list[Workflow] = []
    for path in sorted(settings.sequences_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            flows.append(Workflow.model_validate(data))
        except Exception:
            continue
    return flows


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


_jinja_env = Environment(
    loader=FileSystemLoader(str(settings.templates_dir)),
    autoescape=select_autoescape(["html", "htm"]),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _build_context(lead: Lead) -> dict[str, Any]:
    """Build the Jinja context for template rendering.

    Per-lead values win when present; otherwise org-wide defaults from settings
    fill in so links don't render as blank in emails to leads that don't have a
    Calendly opportunity yet (e.g. fresh nurture leads).
    """
    from app.config import settings as _settings
    return {
        "lead_name": lead.name,
        "call_time": lead.call_time,
        "calendar_link": lead.calendar_link or _settings.app_default_calendar_link,
        "case_study_link": lead.case_study_link or _settings.app_default_case_study_link,
        "meeting_type": lead.meeting_type,
        "mrr": lead.mrr,
        "business_type": lead.business_type,
        "timezone": lead.timezone,
    }


def _template_filename(step: WorkflowStep) -> str:
    suffix = ".html" if step.type == "email" else ".txt"
    base = step.template
    if base.endswith((".html", ".txt", ".jinja", ".j2")):
        return base
    return f"{base}{suffix}"


_BLOCK_END_RE = re.compile(r"</\s*(p|div|li|h[1-6]|tr|table|section|article)\s*>", re.IGNORECASE)
_BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_MAP = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
}
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")


def _html_to_text(html: str) -> str:
    """Convert authored HTML email templates into a plain-text fallback.

    Deterministic and small — sufficient for the hand-written templates in
    this project (paragraphs, links, lists). Not a general HTML renderer.
    """
    text = _BR_RE.sub("\n", html)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    for entity, replacement in _ENTITY_MAP.items():
        text = text.replace(entity, replacement)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def render_step(step: WorkflowStep, lead: Lead) -> tuple[str, str]:
    """Return ``(subject, body)`` for a step rendered against a lead."""
    context = _build_context(lead)
    try:
        template = _jinja_env.get_template(_template_filename(step))
    except TemplateNotFound as exc:
        raise WorkflowNotFoundError(f"template not found: {step.template}") from exc
    body = template.render(**context)
    subject = step.subject
    if subject:
        subject = _jinja_env.from_string(subject).render(**context)
    return subject, body


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _compute_run_at(
    step: WorkflowStep, *, base: datetime, call_at: datetime | None
) -> datetime | None:
    """Return the run-at timestamp for ``step``, or ``None`` if it should be skipped.

    Skip reasons:
        * The step is anchored to call_at but the lead has no call_at.
        * The computed run_at is already in the past relative to ``base``.
    """
    if step.before_call_minutes is not None:
        if call_at is None:
            return None
        run_at = call_at - timedelta(minutes=max(step.before_call_minutes, 0))
        if run_at <= base:
            return None
        return run_at

    # delay_minutes anchor (default 0 when neither is set).
    delay = step.delay_minutes if step.delay_minutes is not None else 0
    return base + timedelta(minutes=max(delay, 0))


def schedule_workflow_for_lead(workflow: Workflow, lead: Lead) -> list[Job]:
    """Create persistent Job records for every step of ``workflow``.

    Steps anchored to ``call_at`` (via ``before_call_minutes``) are skipped
    deterministically when the lead has no call time or when the computed
    run-at is already past. Skipped steps are logged so the operator can see
    why a sequence has fewer jobs than expected.

    Actual APScheduler registration is performed by the caller (scheduler
    module) so this function stays pure and easy to unit-test.
    """
    base = _now()
    jobs: list[Job] = []
    skipped = 0
    for index, step in enumerate(workflow.steps):
        run_at = _compute_run_at(step, base=base, call_at=lead.call_at)
        if run_at is None:
            skipped += 1
            if step.before_call_minutes is not None and lead.call_at is None:
                reason = "lead has no call_at"
            else:
                reason = "computed run_at is in the past"
            storage.log(
                "step.skipped",
                level="WARNING",
                lead_id=lead.id,
                message=(
                    f"workflow '{workflow.name}' step {index} ({step.template}) skipped: {reason}"
                ),
                context={
                    "workflow": workflow.name,
                    "step_index": index,
                    "template": step.template,
                    "reason": reason,
                },
            )
            continue
        job = Job(
            lead_id=lead.id,
            workflow_name=workflow.name,
            step_index=index,
            step_type=step.type,
            template=step.template,
            subject=step.subject,
            run_at=run_at,
        )
        storage.upsert_job(job)
        jobs.append(job)
    storage.log(
        "workflow.scheduled",
        lead_id=lead.id,
        message=(
            f"scheduled {len(jobs)} steps for workflow '{workflow.name}'"
            + (f" ({skipped} skipped)" if skipped else "")
        ),
        context={"workflow": workflow.name, "steps": len(jobs), "skipped": skipped},
    )
    return jobs


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class JobExecutionError(RuntimeError):
    """Raised when a job cannot be executed (lead/workflow missing, etc.)."""


def execute_job(job_id: str) -> Job:
    """Execute a single scheduled job.

    Updates job status, attempt count, and writes log entries deterministically.
    Raises on unrecoverable failure after attempt budget is exhausted.
    """
    job = storage.get_job(job_id)
    if job is None:
        raise JobExecutionError(f"job not found: {job_id}")

    if job.status in (JobStatus.CANCELLED, JobStatus.SUCCEEDED):
        return job

    lead = storage.get_lead(job.lead_id)
    if lead is None:
        storage.update_job_status(
            job.id, JobStatus.FAILED, last_error="lead not found"
        )
        storage.log(
            "job.failed",
            level="ERROR",
            lead_id=job.lead_id,
            job_id=job.id,
            message="lead missing at execution time",
        )
        raise JobExecutionError(f"lead missing for job {job_id}")

    try:
        workflow = load_workflow(job.workflow_name)
    except WorkflowNotFoundError as exc:
        storage.update_job_status(job.id, JobStatus.FAILED, last_error=str(exc))
        storage.log(
            "job.failed",
            level="ERROR",
            lead_id=lead.id,
            job_id=job.id,
            message=str(exc),
        )
        raise JobExecutionError(str(exc)) from exc

    if job.step_index >= len(workflow.steps):
        storage.update_job_status(job.id, JobStatus.FAILED, last_error="step index out of range")
        raise JobExecutionError(f"step index {job.step_index} out of range")

    step = workflow.steps[job.step_index]

    storage.update_job_status(job.id, JobStatus.RUNNING, attempts=job.attempts + 1)

    try:
        subject, body = render_step(step, lead)
        if step.type == "email":
            template_file = _template_filename(step)
            if template_file.endswith((".html", ".htm")):
                plain = _html_to_text(body)
                result = send_email(
                    to=lead.email,
                    subject=subject or "(no subject)",
                    body=plain,
                    body_html=body,
                )
            else:
                result = send_email(to=lead.email, subject=subject or "(no subject)", body=body)
        elif step.type == "sms":
            if not lead.phone:
                raise JobExecutionError("lead has no phone number for SMS step")
            result = send_sms(to=lead.phone, body=body)
        else:
            raise JobExecutionError(f"unsupported step type: {step.type}")
    except (EmailSendError, SmsSendError, JobExecutionError) as exc:
        attempts = job.attempts + 1
        if attempts >= settings.max_send_attempts:
            storage.update_job_status(
                job.id, JobStatus.FAILED, attempts=attempts, last_error=str(exc)
            )
            storage.log(
                "job.failed",
                level="ERROR",
                lead_id=lead.id,
                job_id=job.id,
                message=str(exc),
                context={"attempts": attempts},
            )
        else:
            storage.update_job_status(
                job.id, JobStatus.SCHEDULED, attempts=attempts, last_error=str(exc)
            )
            storage.log(
                "job.retry",
                level="WARNING",
                lead_id=lead.id,
                job_id=job.id,
                message=str(exc),
                context={"attempts": attempts},
            )
        raise

    storage.update_job_status(job.id, JobStatus.SUCCEEDED, last_error="")
    storage.log(
        "job.sent",
        lead_id=lead.id,
        job_id=job.id,
        message=f"{step.type} sent via {result.get('provider')}",
        context={
            "step_index": job.step_index,
            "template": step.template,
            "result": result,
            "preview": body[:500],
            "subject": subject,
        },
    )
    refreshed = storage.get_job(job.id)
    return refreshed or job


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


def _apply_personalization(
    lead: Lead,
    *,
    call_at: datetime | None,
    call_time: str | None,
    calendar_link: str | None,
    timezone_name: str | None,
    meeting_type: str | None,
    mrr: float | None,
    business_type: str | None,
) -> None:
    """Update personalization fields on ``lead`` in-place.

    Order matters: timezone is applied first so ``format_call_time`` reads the
    new value when auto-deriving the call_time display string.
    """
    if timezone_name is not None:
        lead.timezone = timezone_name
    if meeting_type is not None:
        lead.meeting_type = meeting_type
    if mrr is not None:
        lead.mrr = mrr
    if business_type is not None:
        lead.business_type = business_type
    if call_at is not None:
        lead.call_at = call_at
    if call_time is not None:
        lead.call_time = call_time
    elif call_at is not None:
        from app.config import settings as _settings
        tz_name = lead.timezone or _settings.app_display_timezone
        lead.call_time = format_call_time(call_at, tz_name)
    if calendar_link is not None:
        lead.calendar_link = calendar_link


def _run_transition(
    lead: Lead,
    *,
    log_event: str,
    log_message: str,
    workflow_name: str | None,
    cancel_workflow: str | None,
    transition_context: dict[str, object],
) -> dict[str, int | str]:
    """Shared cancel + trigger + log routine used by both transition functions.

    ``cancel_workflow``:
        * ``None`` → cancel ALL pending jobs for this lead (terminal transitions).
        * A workflow name → cancel only that workflow's jobs (scoped, prevents
          one axis from wiping the other when both fire near-simultaneously).
    """
    from app import scheduler  # circular guard

    cancelled = scheduler.cancel_jobs_for_lead(lead.id, workflow_name=cancel_workflow)
    storage.upsert_lead(lead)

    triggered_workflow = ""
    scheduled_jobs = 0
    if workflow_name:
        try:
            workflow = load_workflow(workflow_name)
        except WorkflowNotFoundError as exc:
            storage.log(
                f"{log_event}.workflow_missing",
                level="WARNING",
                lead_id=lead.id,
                message=str(exc),
                context={"expected_workflow": workflow_name, **transition_context},
            )
        else:
            jobs = schedule_workflow_for_lead(workflow, lead)
            for job in jobs:
                scheduler.schedule_job(job)
            scheduled_jobs = len(jobs)
            triggered_workflow = workflow_name

    storage.log(
        log_event,
        lead_id=lead.id,
        message=log_message,
        context={
            **transition_context,
            "cancelled": cancelled,
            "scheduled": scheduled_jobs,
            "workflow": triggered_workflow,
        },
    )

    return {
        **{k: v for k, v in transition_context.items() if isinstance(v, (str, int))},
        "cancelled": cancelled,
        "scheduled": scheduled_jobs,
        "workflow": triggered_workflow,
    }


def apply_lead_status_transition(
    lead: Lead,
    new_status: LeadStatus,
    *,
    call_at: datetime | None = None,
    call_time: str | None = None,
    calendar_link: str | None = None,
    timezone_name: str | None = None,
    meeting_type: str | None = None,
    mrr: float | None = None,
    business_type: str | None = None,
) -> dict[str, int | str]:
    """React to a Close LEAD status change (lifecycle).

    Cancels pending jobs (the lifecycle moved), updates the lead's lifecycle
    status, applies personalization, and triggers the workflow keyed in
    LEAD_STATUS_WORKFLOWS (if any).
    """
    prev = lead.status
    lead.status = new_status
    _apply_personalization(
        lead,
        call_at=call_at,
        call_time=call_time,
        calendar_link=calendar_link,
        timezone_name=timezone_name,
        meeting_type=meeting_type,
        mrr=mrr,
        business_type=business_type,
    )
    # Terminal lead statuses (client, lost, bad_fit, etc.) wipe everything.
    # Non-terminal ones only touch nurture jobs — they must NOT cancel a freshly
    # scheduled Booked Call Sequence that an opportunity event just created.
    cancel_workflow = None if new_status in TERMINAL_LEAD_STATUSES else NURTURE_WORKFLOW
    return _run_transition(
        lead,
        log_event="lead_status.transition",
        log_message=f"lead {prev} -> {new_status}",
        workflow_name=LEAD_STATUS_WORKFLOWS.get(new_status),
        cancel_workflow=cancel_workflow,
        transition_context={"from": prev, "to": new_status, "kind": "lead"},
    )


def apply_opportunity_status_transition(
    lead: Lead,
    new_status: OpportunityStatus,
    *,
    call_at: datetime | None = None,
    call_time: str | None = None,
    calendar_link: str | None = None,
    timezone_name: str | None = None,
    meeting_type: str | None = None,
    mrr: float | None = None,
    business_type: str | None = None,
) -> dict[str, int | str]:
    """React to a Close OPPORTUNITY status change (per-call state)."""
    prev = lead.opportunity_status or ""
    lead.opportunity_status = new_status
    _apply_personalization(
        lead,
        call_at=call_at,
        call_time=call_time,
        calendar_link=calendar_link,
        timezone_name=timezone_name,
        meeting_type=meeting_type,
        mrr=mrr,
        business_type=business_type,
    )
    # Cancel scope rules:
    #   • terminal opp (won/lost)          → cancel everything (lead is done).
    #   • call_booked (new opportunity)    → cancel everything; both old nurture
    #     and any stale prior booked-call jobs should clear before re-scheduling.
    #   • everything else                  → only cancel Booked Call jobs.
    if new_status in TERMINAL_OPPORTUNITY_STATUSES:
        cancel_workflow = None
    elif new_status == "call_booked":
        cancel_workflow = None
    else:
        cancel_workflow = BOOKED_CALL_WORKFLOW
    return _run_transition(
        lead,
        log_event="opportunity_status.transition",
        log_message=f"opportunity {prev or '(none)'} -> {new_status}",
        workflow_name=OPPORTUNITY_STATUS_WORKFLOWS.get(new_status),
        cancel_workflow=cancel_workflow,
        transition_context={"from": prev, "to": new_status, "kind": "opportunity"},
    )
