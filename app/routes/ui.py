"""HTMX-powered manual testing UI.

All views return server-rendered HTML fragments (HTMX-friendly) or the full
shell page at ``/``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import scheduler, storage
from app.config import settings
from app.models import Lead, LeadCreate
from app.workflow import (
    ALLOWED_TEMPLATE_VARS,
    WorkflowNotFoundError,
    list_workflows,
    load_workflow,
    render_template_string,
    sample_lead,
    schedule_workflow_for_lead,
    validate_template_string,
)


router = APIRouter(tags=["ui"])
templates = Jinja2Templates(directory=str(settings.templates_dir))


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    return {"request": request, **extra}


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        _ctx(
            request,
            active_tab="dashboard",
            workflows=list_workflows(),
        ),
    )


# ---------------------------------------------------------------------------
# Dashboard fragment
# ---------------------------------------------------------------------------


@router.get("/ui/dashboard", response_class=HTMLResponse)
def dashboard_fragment(request: Request) -> HTMLResponse:
    leads = storage.load_leads()
    jobs = storage.load_jobs()
    counts = {
        "leads": len(leads),
        "scheduled": sum(1 for j in jobs if j.status == "scheduled"),
        "succeeded": sum(1 for j in jobs if j.status == "succeeded"),
        "failed": sum(1 for j in jobs if j.status == "failed"),
        "cancelled": sum(1 for j in jobs if j.status == "cancelled"),
    }
    recent_logs = storage.load_logs(limit=10)
    return templates.TemplateResponse(
        "_dashboard.html",
        _ctx(request, counts=counts, recent_logs=recent_logs),
    )


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


@router.get("/ui/leads", response_class=HTMLResponse)
def leads_fragment(
    request: Request, *, success: str = "", error: str = ""
) -> HTMLResponse:
    leads = storage.load_leads()
    leads.sort(key=lambda lead: lead.created_at, reverse=True)
    return templates.TemplateResponse(
        "_leads.html",
        _ctx(
            request,
            leads=leads,
            workflows=list_workflows(),
            success=success,
            error=error,
        ),
    )


@router.post("/ui/leads", response_class=HTMLResponse)
def create_lead_form(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    call_time: str = Form(""),
    call_at: str = Form(""),
    calendar_link: str = Form(""),
    case_study_link: str = Form(""),
) -> HTMLResponse:
    payload = LeadCreate(
        name=name,
        email=email,  # type: ignore[arg-type]
        phone=phone,
        call_time=call_time,
        call_at=call_at or None,  # pydantic parses ISO 8601 string into datetime
        calendar_link=calendar_link,
        case_study_link=case_study_link,
    )
    lead = Lead(**payload.model_dump())
    storage.upsert_lead(lead)
    storage.log("lead.created", lead_id=lead.id, message=f"lead created via UI: {lead.email}")
    return leads_fragment(request, success=f"Created lead {lead.name} <{lead.email}>.")


@router.post("/ui/leads/{lead_id}/trigger", response_class=HTMLResponse)
def trigger_workflow_form(
    request: Request,
    lead_id: str,
    workflow: str = Form("Booked Call Sequence"),
) -> HTMLResponse:
    lead = storage.get_lead(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    try:
        wf = load_workflow(workflow)
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    jobs = schedule_workflow_for_lead(wf, lead)
    for job in jobs:
        scheduler.schedule_job(job)
    skipped = len(wf.steps) - len(jobs)
    msg = (
        f"Triggered \"{workflow}\" for {lead.name} <{lead.email}> "
        f"— {len(jobs)} job(s) scheduled"
    )
    if skipped:
        msg += f", {skipped} step(s) skipped (missing call_at or past-due)"
    return leads_fragment(request, success=msg)


@router.post("/ui/leads/{lead_id}/delete", response_class=HTMLResponse)
def delete_lead_form(request: Request, lead_id: str) -> HTMLResponse:
    lead = storage.get_lead(lead_id)
    if not storage.delete_lead(lead_id):
        raise HTTPException(status_code=404, detail="lead not found")
    storage.log("lead.deleted", lead_id=lead_id)
    name = lead.name if lead else lead_id
    return leads_fragment(request, success=f"Deleted lead {name}.")


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@router.get("/ui/jobs", response_class=HTMLResponse)
def jobs_fragment(request: Request) -> HTMLResponse:
    jobs = storage.load_jobs()
    jobs.sort(key=lambda job: job.run_at, reverse=True)
    leads_by_id = {lead.id: lead for lead in storage.load_leads()}
    return templates.TemplateResponse(
        "_jobs.html",
        _ctx(request, jobs=jobs, leads_by_id=leads_by_id),
    )


@router.post("/ui/jobs/{job_id}/cancel", response_class=HTMLResponse)
def cancel_job_form(request: Request, job_id: str) -> HTMLResponse:
    scheduler.cancel_job(job_id)
    return jobs_fragment(request)


@router.post("/ui/jobs/{job_id}/retry", response_class=HTMLResponse)
def retry_job_form(request: Request, job_id: str) -> HTMLResponse:
    scheduler.retry_job(job_id)
    return jobs_fragment(request)


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


@router.get("/ui/logs", response_class=HTMLResponse)
def logs_fragment(request: Request) -> HTMLResponse:
    logs = storage.load_logs(limit=200)
    return templates.TemplateResponse("_logs.html", _ctx(request, logs=logs))


# ---------------------------------------------------------------------------
# Templates editor
# ---------------------------------------------------------------------------

_FILENAME_RE = re.compile(r"^[a-z][a-z0-9_]*\.(html|txt)$")
_RESERVED_TEMPLATES = {"index.html"}


def _list_editable_templates() -> list[dict[str, Any]]:
    """All editable templates (excludes UI fragments and index.html)."""
    out: list[dict[str, Any]] = []
    for path in sorted(settings.templates_dir.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if name.startswith("_") or name in _RESERVED_TEMPLATES:
            continue
        if path.suffix not in (".html", ".txt"):
            continue
        out.append(
            {
                "name": name,
                "stem": path.stem,
                "kind": "email" if path.suffix == ".html" else "sms",
                "size": path.stat().st_size,
                "mtime": path.stat().st_mtime,
            }
        )
    return out


def _workflows_referencing(template_stem: str) -> list[str]:
    return [w.name for w in list_workflows() if any(s.template == template_stem for s in w.steps)]


def _safe_template_path(name: str) -> Path:
    """Validate ``name`` and return the absolute path inside templates_dir."""
    if not _FILENAME_RE.match(name):
        raise HTTPException(status_code=400, detail="invalid filename")
    if name.startswith("_") or name in _RESERVED_TEMPLATES:
        raise HTTPException(status_code=400, detail="reserved system template")
    root = settings.templates_dir.resolve()
    path = (root / name).resolve()
    # Guard against path traversal.
    if not str(path).startswith(str(root)):
        raise HTTPException(status_code=400, detail="path traversal denied")
    return path


def _render_template_list(
    request: Request, *, error: str = "", success: str = ""
) -> HTMLResponse:
    items = _list_editable_templates()
    usage = {it["stem"]: _workflows_referencing(it["stem"]) for it in items}
    return templates.TemplateResponse(
        "_templates.html",
        _ctx(request, items=items, usage=usage, error=error, success=success),
    )


@router.get("/ui/templates", response_class=HTMLResponse)
def templates_fragment(request: Request) -> HTMLResponse:
    return _render_template_list(request)


@router.get("/ui/templates/{name}", response_class=HTMLResponse)
def template_edit_fragment(
    request: Request, name: str, *, error: str = "", success: str = ""
) -> HTMLResponse:
    path = _safe_template_path(name)
    if not path.exists():
        raise HTTPException(status_code=404, detail="template not found")
    body = path.read_text(encoding="utf-8")
    return templates.TemplateResponse(
        "_template_edit.html",
        _ctx(
            request,
            name=name,
            stem=path.stem,
            kind="email" if path.suffix == ".html" else "sms",
            body=body,
            allowed_vars=ALLOWED_TEMPLATE_VARS,
            sample=sample_lead(),
            preview="",
            error=error,
            success=success,
        ),
    )


@router.post("/ui/templates/{name}", response_class=HTMLResponse)
def save_template(
    request: Request, name: str, body: str = Form(...)
) -> HTMLResponse:
    path = _safe_template_path(name)
    if not path.exists():
        raise HTTPException(status_code=404, detail="template not found")
    err = validate_template_string(body, sample_lead())
    if err:
        return templates.TemplateResponse(
            "_template_edit.html",
            _ctx(
                request,
                name=name,
                stem=path.stem,
                kind="email" if path.suffix == ".html" else "sms",
                body=body,  # show the user's failed input, not the saved version
                allowed_vars=ALLOWED_TEMPLATE_VARS,
                sample=sample_lead(),
                preview="",
                error=err,
                success="",
            ),
        )
    path.write_text(body, encoding="utf-8")
    return template_edit_fragment(request, name, success="Saved.")


@router.post("/ui/templates/{name}/preview", response_class=HTMLResponse)
def preview_template(
    request: Request, name: str, body: str = Form(...)
) -> HTMLResponse:
    path = _safe_template_path(name)
    try:
        rendered = render_template_string(body, sample_lead())
    except Exception as exc:  # noqa: BLE001 - surface any render error
        rendered = ""
        err = f"{type(exc).__name__}: {exc}"
    else:
        err = ""
    return templates.TemplateResponse(
        "_template_preview.html",
        _ctx(
            request,
            rendered=rendered,
            kind="email" if path.suffix == ".html" else "sms",
            error=err,
        ),
    )


@router.post("/ui/templates", response_class=HTMLResponse)
def create_template_form(
    request: Request,
    filename: str = Form(...),
    body: str = Form(""),
) -> HTMLResponse:
    name = filename.strip().lower()
    try:
        path = _safe_template_path(name)
    except HTTPException as exc:
        return _render_template_list(request, error=f"Invalid filename: {exc.detail}")
    if path.exists():
        return _render_template_list(request, error=f"{name} already exists.")
    err = validate_template_string(body, sample_lead())
    if err:
        return _render_template_list(request, error=f"Template body invalid: {err}")
    path.write_text(body, encoding="utf-8")
    return _render_template_list(request, success=f"Created {name}.")


@router.post("/ui/templates/{name}/delete", response_class=HTMLResponse)
def delete_template_form(request: Request, name: str) -> HTMLResponse:
    path = _safe_template_path(name)
    if not path.exists():
        raise HTTPException(status_code=404, detail="template not found")
    using = _workflows_referencing(path.stem)
    if using:
        return _render_template_list(
            request,
            error=f"Cannot delete: still used by workflows: {', '.join(using)}",
        )
    path.unlink()
    return _render_template_list(request, success=f"Deleted {name}.")
