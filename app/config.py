"""Application configuration loaded from environment variables.

All settings are deterministic and explicit. No hidden defaults that change
behavior at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Server ---
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)
    app_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    # IANA timezone used when formatting call_at into a human-readable
    # call_time string (e.g. "Thu May 21, 3:00 PM PDT"). Defaults to UTC.
    app_display_timezone: str = Field(default="UTC")

    # Org-wide fallbacks for template variables. When the lead doesn't have a
    # per-lead value (Close custom field empty, or manual UI lead with no link),
    # these are substituted at render time. Leave empty to keep variables blank.
    app_default_calendar_link: str = Field(default="")
    app_default_case_study_link: str = Field(default="")

    # --- Paths ---
    data_dir: Path = Field(default=APP_ROOT / "data")
    sequences_dir: Path = Field(default=APP_ROOT / "sequences")
    templates_dir: Path = Field(default=APP_ROOT / "templates")

    # --- Retention ---
    lead_retention_days: int = Field(default=180)  # 6 months
    # Cap for terminal jobs in jobs.json (succeeded/failed/cancelled).
    # Active jobs (scheduled/running) are never trimmed regardless of count.
    max_job_entries: int = Field(default=5000)

    # --- Retry policy ---
    max_send_attempts: int = Field(default=3)
    retry_delay_seconds: int = Field(default=300)  # 5 minutes between retries

    # --- Email (SMTP) ---
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_username: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_use_tls: bool = Field(default=True)
    smtp_from: str = Field(default="no-reply@example.com")

    # --- Close CRM ---
    close_api_key: str = Field(default="")
    close_base_url: str = Field(default="https://api.close.com/api/v1")
    close_webhook_secret: str = Field(default="")
    # Custom field IDs (cf_xxx) on Close. Empty = not configured; the engine
    # will then rely on whatever call_at is already on the lead.
    close_cf_booking_date: str = Field(default="")
    close_cf_call_time: str = Field(default="")
    close_cf_calendly_link: str = Field(default="")
    close_cf_lead_timezone: str = Field(default="")
    close_cf_meeting_type: str = Field(default="")
    close_cf_mrr: str = Field(default="")
    close_cf_business_type: str = Field(default="")
    # Opportunity status IDs (stat_xxx). Preferred over label matching.
    close_status_call_booked: str = Field(default="")
    close_status_call_rescheduled: str = Field(default="")
    close_status_call_cancelled: str = Field(default="")
    close_status_call_completed: str = Field(default="")
    close_status_contact_sent: str = Field(default="")
    close_status_won: str = Field(default="")
    close_status_opportunity_lost: str = Field(default="")
    close_status_follow_up: str = Field(default="")
    # Lead-lifecycle status IDs.
    close_lead_status_potential: str = Field(default="")
    close_lead_status_nurture: str = Field(default="")
    close_lead_status_qualified: str = Field(default="")
    close_lead_status_client: str = Field(default="")
    close_lead_status_unqualified: str = Field(default="")
    close_lead_status_lost: str = Field(default="")
    close_lead_status_bad_fit: str = Field(default="")
    close_lead_status_cancelled: str = Field(default="")

    # --- SMS (Twilio-compatible REST; optional) ---
    sms_provider: Literal["none", "twilio", "log"] = Field(default="log")
    twilio_account_sid: str = Field(default="")
    twilio_auth_token: str = Field(default="")
    twilio_from_number: str = Field(default="")

    # --- Email dispatch mode ---
    email_provider: Literal["smtp", "log"] = Field(default="log")

    @property
    def leads_path(self) -> Path:
        return self.data_dir / "leads.json"

    @property
    def jobs_path(self) -> Path:
        return self.data_dir / "jobs.json"

    @property
    def logs_path(self) -> Path:
        return self.data_dir / "logs.json"

    @property
    def tombstones_path(self) -> Path:
        return self.data_dir / "tombstones.json"


settings = Settings()
