"""Deterministic JSON persistence.

Responsibilities:
    * Atomic read/write of leads/jobs/logs JSON files.
    * In-process locking to serialize concurrent writes.
    * Graceful recovery from a missing or corrupt file (logged + replaced
      with an empty list, never a silent crash).
    * Periodic retention sweep that purges leads older than the configured
      retention window.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

from app.config import settings
from app.models import Job, JobStatus, Lead, LogEntry


_file_locks: dict[Path, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    with _locks_guard:
        lock = _file_locks.get(path)
        if lock is None:
            lock = threading.RLock()
            _file_locks[path] = lock
        return lock


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        # Corruption: quarantine the file and start fresh.
        if path.exists():
            quarantine = path.with_suffix(path.suffix + ".corrupt")
            try:
                path.replace(quarantine)
            except OSError:
                pass
        return []
    if not isinstance(data, list):
        return []
    return data


def _atomic_write_json(path: Path, payload: Any) -> None:
    _ensure_parent(path)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=_json_default)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"Unserializable type: {type(value).__name__}")


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _load(path: Path, model: type[T]) -> list[T]:
    with _lock_for(path):
        raw = _read_json_list(path)
    items: list[T] = []
    for entry in raw:
        try:
            items.append(model.model_validate(entry))
        except Exception:
            # Skip malformed individual records rather than failing the load.
            continue
    return items


def _save(path: Path, items: Iterable[T]) -> None:
    payload = [item.model_dump(mode="json") for item in items]
    with _lock_for(path):
        _atomic_write_json(path, payload)


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


def load_leads() -> list[Lead]:
    return _load(settings.leads_path, Lead)


def save_leads(leads: Iterable[Lead]) -> None:
    _save(settings.leads_path, leads)


def get_lead(lead_id: str) -> Lead | None:
    return next((lead for lead in load_leads() if lead.id == lead_id), None)


def get_lead_by_external_id(external_id: str) -> Lead | None:
    """Find a lead by its upstream CRM identifier (e.g. Close's lead_xxx)."""
    if not external_id:
        return None
    return next(
        (lead for lead in load_leads() if lead.external_id == external_id),
        None,
    )


def upsert_lead(lead: Lead) -> Lead:
    with _lock_for(settings.leads_path):
        leads = load_leads()
        lead.updated_at = datetime.now(timezone.utc)
        for idx, existing in enumerate(leads):
            if existing.id == lead.id:
                leads[idx] = lead
                break
        else:
            leads.append(lead)
        save_leads(leads)
    return lead


def delete_lead(lead_id: str) -> bool:
    """Delete a lead. If the lead has an external_id, tombstone it so future
    webhook events from the upstream CRM don't silently re-create the lead.
    """
    with _lock_for(settings.leads_path):
        leads = load_leads()
        target = next((lead for lead in leads if lead.id == lead_id), None)
        if target is None:
            return False
        new_leads = [lead for lead in leads if lead.id != lead_id]
        save_leads(new_leads)
    if target.external_id:
        add_tombstone(target.external_id)
    return True


# ---------------------------------------------------------------------------
# Tombstones
# ---------------------------------------------------------------------------
#
# When a lead is deleted locally we record its upstream CRM identifier in a
# tombstone file. The webhook handler checks this list and silently ignores
# events for tombstoned external_ids so a CRM-side edit doesn't re-create a
# lead we explicitly removed.


def _read_tombstones() -> dict[str, str]:
    """Return {external_id: deleted_at_iso} from disk (graceful on missing/corrupt)."""
    path = settings.tombstones_path
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _write_tombstones(entries: dict[str, str]) -> None:
    with _lock_for(settings.tombstones_path):
        _atomic_write_json(settings.tombstones_path, entries)  # type: ignore[arg-type]


def add_tombstone(external_id: str) -> None:
    if not external_id:
        return
    with _lock_for(settings.tombstones_path):
        entries = _read_tombstones()
        entries[external_id] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(settings.tombstones_path, entries)  # type: ignore[arg-type]


def remove_tombstone(external_id: str) -> bool:
    with _lock_for(settings.tombstones_path):
        entries = _read_tombstones()
        if external_id not in entries:
            return False
        del entries[external_id]
        _atomic_write_json(settings.tombstones_path, entries)  # type: ignore[arg-type]
    return True


def is_tombstoned(external_id: str) -> bool:
    if not external_id:
        return False
    return external_id in _read_tombstones()


def load_tombstones() -> dict[str, str]:
    return _read_tombstones()


def purge_expired_leads(retention_days: int | None = None) -> int:
    """Delete leads whose created_at is older than the retention window.

    Also prunes tombstones older than the same window so they don't grow
    forever — a CRM lead that's been deleted longer than the retention period
    can safely be allowed to re-appear if it ever comes back.
    """
    days = retention_days if retention_days is not None else settings.lead_retention_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with _lock_for(settings.leads_path):
        leads = load_leads()
        kept = [lead for lead in leads if lead.created_at >= cutoff]
        removed = len(leads) - len(kept)
        if removed:
            save_leads(kept)
    # Prune expired tombstones.
    with _lock_for(settings.tombstones_path):
        entries = _read_tombstones()
        survivors: dict[str, str] = {}
        for eid, ts in entries.items():
            try:
                t = datetime.fromisoformat(ts)
            except ValueError:
                continue  # drop malformed entries
            if t >= cutoff:
                survivors[eid] = ts
        if len(survivors) != len(entries):
            _atomic_write_json(settings.tombstones_path, survivors)
    return removed


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def load_jobs() -> list[Job]:
    return _load(settings.jobs_path, Job)


def save_jobs(jobs: Iterable[Job]) -> None:
    """Persist the jobs list, capping terminal job retention.

    Active jobs (scheduled/running) are always kept — they represent pending
    work the scheduler will pick up on next boot. Only terminal jobs
    (succeeded/failed/cancelled) are trimmed when the cap is exceeded, oldest
    by ``updated_at`` first.
    """
    all_jobs = list(jobs)
    active = [j for j in all_jobs if j.status in (JobStatus.SCHEDULED, JobStatus.RUNNING)]
    terminal = [
        j for j in all_jobs
        if j.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)
    ]
    cap = settings.max_job_entries
    if len(terminal) > cap:
        terminal.sort(key=lambda j: j.updated_at)
        terminal = terminal[-cap:]
    _save(settings.jobs_path, active + terminal)


def get_job(job_id: str) -> Job | None:
    return next((job for job in load_jobs() if job.id == job_id), None)


def upsert_job(job: Job) -> Job:
    with _lock_for(settings.jobs_path):
        jobs = load_jobs()
        job.updated_at = datetime.now(timezone.utc)
        for idx, existing in enumerate(jobs):
            if existing.id == job.id:
                jobs[idx] = job
                break
        else:
            jobs.append(job)
        save_jobs(jobs)
    return job


def update_job_status(
    job_id: str,
    status: JobStatus,
    *,
    attempts: int | None = None,
    last_error: str | None = None,
) -> Job | None:
    with _lock_for(settings.jobs_path):
        jobs = load_jobs()
        for job in jobs:
            if job.id == job_id:
                job.status = status
                if attempts is not None:
                    job.attempts = attempts
                if last_error is not None:
                    job.last_error = last_error
                job.updated_at = datetime.now(timezone.utc)
                save_jobs(jobs)
                return job
    return None


def pending_jobs() -> list[Job]:
    return [j for j in load_jobs() if j.status == JobStatus.SCHEDULED]


def pending_jobs_for_lead(lead_id: str) -> list[Job]:
    return [
        j for j in load_jobs()
        if j.lead_id == lead_id and j.status in (JobStatus.SCHEDULED, JobStatus.RUNNING)
    ]


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


MAX_LOG_ENTRIES = 5000


def load_logs(limit: int | None = None) -> list[LogEntry]:
    logs = _load(settings.logs_path, LogEntry)
    logs.sort(key=lambda entry: entry.ts, reverse=True)
    if limit is not None:
        return logs[:limit]
    return logs


def append_log(entry: LogEntry) -> None:
    with _lock_for(settings.logs_path):
        logs = _load(settings.logs_path, LogEntry)
        logs.append(entry)
        if len(logs) > MAX_LOG_ENTRIES:
            logs = logs[-MAX_LOG_ENTRIES:]
        _save(settings.logs_path, logs)


def log(
    event: str,
    *,
    level: str = "INFO",
    lead_id: str = "",
    job_id: str = "",
    message: str = "",
    context: dict[str, Any] | None = None,
) -> LogEntry:
    entry = LogEntry(
        event=event,
        level=level,  # type: ignore[arg-type]
        lead_id=lead_id,
        job_id=job_id,
        message=message,
        context=context or {},
    )
    append_log(entry)
    return entry


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def ensure_data_files() -> None:
    """Create empty data files on first boot."""
    for path in (settings.leads_path, settings.jobs_path, settings.logs_path):
        if not path.exists():
            _ensure_parent(path)
            _atomic_write_json(path, [])
    if not settings.tombstones_path.exists():
        _ensure_parent(settings.tombstones_path)
        _atomic_write_json(settings.tombstones_path, {})
