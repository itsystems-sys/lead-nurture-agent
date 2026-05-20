"""APScheduler wrapper.

Why a custom persistence layer:
    APScheduler's built-in persistent jobstores (SQLAlchemy / MongoDB / Redis)
    all require a database, which is explicitly forbidden by the project
    constraints. Instead we use a MemoryJobStore and re-register pending jobs
    from ``data/jobs.json`` on startup. Job state is the source of truth and
    survives restarts deterministically.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from app import storage
from app.config import settings
from app.models import Job, JobStatus
from app.workflow import execute_job

logger = logging.getLogger(__name__)


_scheduler: Optional[BackgroundScheduler] = None


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="UTC")
    return _scheduler


def _run_job(job_id: str) -> None:
    """APScheduler entry point. Swallows exceptions; the workflow layer logs them."""
    try:
        execute_job(job_id)
    except Exception as exc:  # noqa: BLE001 - boundary
        logger.warning("job %s raised: %s", job_id, exc)


def _job_id_for(job: Job) -> str:
    return f"job:{job.id}"


def schedule_job(job: Job) -> None:
    """Register a Job with APScheduler at its ``run_at`` time."""
    scheduler = get_scheduler()
    run_at = job.run_at
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=timezone.utc)
    # Past-due jobs run immediately (with a tiny offset so APScheduler accepts them).
    if run_at <= datetime.now(timezone.utc):
        run_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    scheduler.add_job(
        _run_job,
        trigger=DateTrigger(run_date=run_at),
        args=[job.id],
        id=_job_id_for(job),
        replace_existing=True,
        misfire_grace_time=None,
    )


def schedule_retry(job: Job, *, delay_seconds: int | None = None) -> None:
    """Register a retry for a job that just failed but has attempts remaining."""
    delay = delay_seconds if delay_seconds is not None else settings.retry_delay_seconds
    run_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    scheduler = get_scheduler()
    scheduler.add_job(
        _run_job,
        trigger=DateTrigger(run_date=run_at),
        args=[job.id],
        id=_job_id_for(job),
        replace_existing=True,
        misfire_grace_time=None,
    )


def cancel_job(job_id: str) -> bool:
    """Cancel a scheduled job. Marks the persisted job as cancelled."""
    scheduler = get_scheduler()
    try:
        scheduler.remove_job(f"job:{job_id}")
    except Exception:
        # Already fired, never scheduled, etc. -- treat as no-op for APScheduler side.
        pass
    job = storage.get_job(job_id)
    if job is None:
        return False
    if job.status in (JobStatus.SUCCEEDED, JobStatus.CANCELLED):
        return False
    storage.update_job_status(job_id, JobStatus.CANCELLED)
    storage.log("job.cancelled", lead_id=job.lead_id, job_id=job.id)
    return True


def cancel_jobs_for_lead(lead_id: str, *, workflow_name: str | None = None) -> int:
    """Cancel pending jobs for a lead. Returns count cancelled.

    If ``workflow_name`` is given, only jobs from that workflow are cancelled —
    used by status transitions that should leave other workflows untouched.
    """
    cancelled = 0
    for job in storage.pending_jobs_for_lead(lead_id):
        if workflow_name is not None and job.workflow_name != workflow_name:
            continue
        if cancel_job(job.id):
            cancelled += 1
    return cancelled


def retry_job(job_id: str) -> bool:
    """Reset a failed job and re-schedule it for immediate execution."""
    job = storage.get_job(job_id)
    if job is None:
        return False
    if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
        return False
    job.status = JobStatus.SCHEDULED
    job.attempts = 0
    job.last_error = ""
    job.run_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    storage.upsert_job(job)
    schedule_job(job)
    storage.log("job.retry_manual", lead_id=job.lead_id, job_id=job.id)
    return True


def _purge_expired_leads_job() -> None:
    try:
        removed = storage.purge_expired_leads()
        if removed:
            storage.log(
                "retention.purge",
                message=f"removed {removed} expired leads",
                context={"removed": removed},
            )
    except Exception as exc:  # noqa: BLE001 - boundary
        logger.warning("retention purge failed: %s", exc)


def start_scheduler() -> BackgroundScheduler:
    """Start scheduler, restore pending jobs, register daily retention sweep."""
    scheduler = get_scheduler()
    if scheduler.running:
        return scheduler

    scheduler.start()

    # Restore scheduled jobs from persisted state.
    for job in storage.pending_jobs():
        schedule_job(job)

    # Daily retention sweep at 03:00 UTC.
    scheduler.add_job(
        _purge_expired_leads_job,
        trigger="cron",
        hour=3,
        minute=0,
        id="retention:purge_leads",
        replace_existing=True,
    )
    # Also run once on boot to catch up if the service was down for a while.
    _purge_expired_leads_job()

    storage.log(
        "scheduler.started",
        message=f"scheduler started; restored {len(storage.pending_jobs())} jobs",
    )
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
