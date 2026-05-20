# Lead Nurture Engine

A deterministic Python automation platform that drives lead nurture and
booked-call show-up sequences from CRM webhooks. No AI, no LLMs, no database.

- **Deterministic.** Every send is decided by `rules + schedules + templates`. Same input, same output.
- **No database.** State lives in three JSON files (`data/leads.json`, `data/jobs.json`, `data/logs.json`) with atomic writes and in-process locks.
- **No LLM, no AI generation.** Email and SMS bodies are static Jinja2 templates with a strict allowlist of variables. Authored by humans.
- **CRM-agnostic core.** Close ships in-box via an adapter; HubSpot / Salesforce / Pipedrive plug in the same way (add `app/adapters/<crm>.py`).
- **Built-in UI.** Server-rendered HTMX pages for leads, jobs, logs, and a template editor with live preview.
- **Persistent across restarts.** Scheduled jobs are re-loaded from `jobs.json` on boot — no work is lost when the process restarts.

Built to be small enough to run on a single VPS, predictable enough that
operations don't need a dashboard to understand what it's doing.

---

## Table of contents

- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Project structure](#project-structure)
- [Configuration](#configuration)
- [Sequences and templates](#sequences-and-templates)
- [HTTP endpoints](#http-endpoints)
- [Close CRM integration](#close-crm-integration)
- [Adding a new CRM](#adding-a-new-crm)
- [UI tour](#ui-tour)
- [Operations](#operations)

---

## How it works

```
┌─────────────────────────────────────────────────────────────────┐
│ CRM adapters (CRM-specific)                                     │
│ ─────────────────────────────                                   │
│  adapters/close.py                                              │
│  adapters/hubspot.py     (future)                               │
│  adapters/salesforce.py  (future)                               │
│                                                                 │
│  Each adapter:                                                  │
│   • verifies incoming webhook signature                         │
│   • parses CRM payload into NormalizedWebhookEvent              │
│   • maps CRM statuses to our LeadStatus / OpportunityStatus     │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│ Engine core (CRM-agnostic)                                      │
│ ──────────────────────────                                      │
│  models.py     Lead, LeadStatus, OpportunityStatus, Job          │
│  workflow.py   State machine + workflow rendering                │
│  scheduler.py  APScheduler + JSON-backed job persistence         │
│  storage.py    Atomic JSON I/O + retention sweeps                │
│  routes/api.py Webhook + REST endpoints                          │
│  routes/ui.py  HTMX UI fragments                                 │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│ Side effects                                                    │
│ ───────────                                                     │
│  services/email_service.py   SMTP (Gmail / Postmark / SES / …)  │
│  services/sms_service.py     Twilio (or log-only in dev)        │
│  data/leads.json             Persistent lead store               │
│  data/jobs.json              Persistent job store                │
│  data/logs.json              Ring buffer (last 5000 events)      │
└─────────────────────────────────────────────────────────────────┘
```

Event flow: CRM webhook → adapter normalizes → engine applies status
transition → scheduler queues template-rendered sends.

---

## Quick start

```bash
git clone <repo> lead-nurture-agent
cd lead-nurture-agent
cp .env.example .env       # fill in CLOSE_API_KEY, SMTP_*, etc.
./run.sh
```

`run.sh` creates the venv, installs dependencies, kills any prior uvicorn
process, and starts the server on `http://localhost:8000` with auto-reload.

Open `http://localhost:8000` for the UI. The webhook endpoint for Close is at
`/api/webhooks/close` (or the dedicated `/api/webhooks/close/lead` and
`/api/webhooks/close/opportunity`).

### Sending real emails

`EMAIL_PROVIDER=log` (default) writes rendered bodies to `data/logs.json`
without hitting the network — useful for dev. To actually send via Gmail SMTP:

```env
EMAIL_PROVIDER=smtp
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465                # 587 (STARTTLS) is blocked by some ISPs; 465 (SMTPS) works
SMTP_USERNAME=you@your-domain.com
SMTP_PASSWORD=xxxxxxxxxxxxxxxx   # Gmail App Password, not your account password
SMTP_USE_TLS=true
SMTP_FROM=you@your-domain.com
```

Gmail requires the `From` to match an authenticated address or a verified
"Send mail as" alias, or it silently rewrites it.

### Testing without a CRM

For smoke-testing the engine end-to-end without setting up Close:

```bash
# Create a lead via the UI form (Leads tab) or POST:
curl -X POST localhost:8000/api/leads \
  -H 'Content-Type: application/json' \
  -d '{"name":"Test","email":"you@example.com","status":"potential"}'

# Manually fire a Call Booked transition:
curl -X POST "localhost:8000/api/leads/{lead_id}/opportunity-status" \
  -H 'Content-Type: application/json' \
  -d '{"status":"call_booked","call_at":"2026-05-22T15:00:00Z"}'
```

---

## Project structure

```
lead-nurture-agent/
├─ run.sh
├─ requirements.txt
├─ .env.example
└─ app/
   ├─ main.py            FastAPI app + lifespan hook
   ├─ config.py          pydantic-settings Settings (env vars)
   ├─ models.py          Pydantic models: Lead, Workflow, Job, LogEntry
   ├─ storage.py         Atomic JSON read/write + retention sweep
   ├─ scheduler.py       APScheduler wrapper, job re-load on boot
   ├─ workflow.py        State machine, render, dispatch
   │
   ├─ adapters/          ───── CRM adapters live here ─────
   │   ├─ base.py        CrmAdapter protocol + NormalizedWebhookEvent
   │   └─ close.py       Close CRM adapter
   │
   ├─ routes/
   │   ├─ api.py         JSON API + webhook endpoints
   │   └─ ui.py          HTMX UI fragments
   │
   ├─ services/
   │   ├─ email_service.py  SMTP / log
   │   └─ sms_service.py    Twilio / log / none
   │
   ├─ sequences/         ───── Workflow definitions (JSON) ─────
   │   ├─ booked_call.json     The show-up sequence
   │   ├─ lead_nurture.json    The pre-call nurture sequence
   │   └─ welcome_test.json    Single-step smoke-test sequence
   │
   ├─ templates/         ───── Email & SMS templates ─────
   │   ├─ confirmation_email.html
   │   ├─ value_followup_email.html
   │   ├─ reminder_24h_email.html
   │   ├─ reminder_1h_sms.txt
   │   ├─ nurture_*.html        (Lead Nurture Sequence)
   │   ├─ welcome_test_email.html
   │   ├─ index.html            (UI shell)
   │   └─ _*.html               (HTMX UI fragments — not editable in UI)
   │
   └─ data/
       ├─ leads.json     Persistent lead store
       ├─ jobs.json      Persistent job store
       └─ logs.json      Ring-buffer event log
```

---

## Configuration

All runtime config lives in `.env`. Key sections:

### Application
```env
APP_HOST=0.0.0.0
APP_PORT=8000
APP_LOG_LEVEL=INFO
APP_DISPLAY_TIMEZONE=UTC          # IANA TZ used when formatting call_time
APP_DEFAULT_CALENDAR_LINK=        # Falls back when lead has no per-lead value
APP_DEFAULT_CASE_STUDY_LINK=      # Falls back when lead has no per-lead value
```

### Retention
```env
LEAD_RETENTION_DAYS=180           # 6-month sweep, runs daily at 03:00 UTC + on boot
MAX_JOB_ENTRIES=5000              # Caps terminal jobs; active jobs never trimmed
```

(Logs are capped at 5000 entries in code via `storage.MAX_LOG_ENTRIES`.)

### Retry policy
```env
MAX_SEND_ATTEMPTS=3               # Per-job retry budget
RETRY_DELAY_SECONDS=300           # Delay between attempts
```

### Email
```env
EMAIL_PROVIDER=log                # log | smtp
SMTP_HOST=
SMTP_PORT=465                     # 465=SMTPS (recommended), 587=STARTTLS
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_USE_TLS=true
SMTP_FROM="You <you@your-domain.com>"
```

### SMS
```env
SMS_PROVIDER=log                  # log | twilio | none
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=
```

### Close CRM
```env
CLOSE_API_KEY=
CLOSE_BASE_URL=https://api.close.com/api/v1
CLOSE_WEBHOOK_SECRET=             # If set, all Close webhooks are HMAC-verified

# Custom field IDs (Settings → Custom Fields → URL contains cf_xxx)
CLOSE_CF_BOOKING_DATE=
CLOSE_CF_CALL_TIME=
CLOSE_CF_CALENDLY_LINK=
CLOSE_CF_LEAD_TIMEZONE=
CLOSE_CF_MEETING_TYPE=
CLOSE_CF_MRR=
CLOSE_CF_BUSINESS_TYPE=

# Opportunity status IDs (Settings → Pipelines → status URL contains stat_xxx)
CLOSE_STATUS_CALL_BOOKED=
CLOSE_STATUS_CALL_RESCHEDULED=
CLOSE_STATUS_CALL_CANCELLED=
CLOSE_STATUS_CALL_COMPLETED=
CLOSE_STATUS_CONTACT_SENT=
CLOSE_STATUS_WON=
CLOSE_STATUS_OPPORTUNITY_LOST=
CLOSE_STATUS_FOLLOW_UP=

# Lead lifecycle status IDs
CLOSE_LEAD_STATUS_POTENTIAL=
CLOSE_LEAD_STATUS_NURTURE=
CLOSE_LEAD_STATUS_QUALIFIED=
CLOSE_LEAD_STATUS_CLIENT=
CLOSE_LEAD_STATUS_UNQUALIFIED=
CLOSE_LEAD_STATUS_LOST=
CLOSE_LEAD_STATUS_BAD_FIT=
CLOSE_LEAD_STATUS_CANCELLED=
```

Status IDs are preferred over label matching (stable across renames), but
label matching is a fallback when an ID isn't configured. Setting the IDs is
optional but recommended.

---

## Sequences and templates

A **sequence** is a JSON file in `app/sequences/` that describes a workflow
as an ordered list of timed steps:

```json
{
  "name": "Booked Call Sequence",
  "steps": [
    { "type": "email", "delay_minutes": 0,        "template": "confirmation_email",
      "subject": "You're confirmed for {{ call_time }}" },
    { "type": "email", "delay_minutes": 10,       "template": "value_followup_email",
      "subject": "Quick read before our call" },
    { "type": "email", "before_call_minutes": 1440, "template": "reminder_24h_email",
      "subject": "Reminder: our call is tomorrow at {{ call_time }}" },
    { "type": "sms",   "before_call_minutes": 60,   "template": "reminder_1h_sms" }
  ]
}
```

### Step timing anchors

| Field | Meaning |
|---|---|
| `delay_minutes` | Fire N minutes after the workflow was triggered (booking-anchored). |
| `before_call_minutes` | Fire N minutes before `lead.call_at` (call-anchored). |

Mix both freely. Steps with `before_call_minutes` are silently skipped at
schedule time when:
- The lead has no `call_at` value.
- The computed `run_at` is already in the past.

### Templates

- HTML email templates use `.html` extension; SMS uses `.txt`.
- Variables are injected at render time. Only the allowlisted variables are
  available — anything else fails fast at save time with `UndefinedError`:

  ```
  lead_name, call_time, calendar_link, case_study_link,
  meeting_type, mrr, business_type, timezone
  ```

- HTML emails are sent as `multipart/alternative` (HTML + auto-generated
  plain text fallback). SMS is plain text only.

### Editing

Use the **Templates** tab in the UI. Each template gets:
- Source editor (textarea)
- Live preview rendered against a sample lead populated with your env
  defaults so the preview matches what real recipients will see.
- Validation against the allowlist at save time.
- Delete protection (can't delete a template that's still referenced by a
  sequence).

Or edit the files directly under `app/templates/`. No server restart needed
— templates are auto-reloaded on next render.

---

## HTTP endpoints

All API endpoints are mounted under both `/api/` and `/api/v1/` for
compatibility with subscriptions that hard-code either prefix.

### Health
```
GET  /health
GET  /api/health
```

### CRM webhooks
```
POST /api/webhooks/close              (generic dispatcher — accepts any object_type)
POST /api/webhooks/close/lead         (dedicated lead-event endpoint)
POST /api/webhooks/close/opportunity  (dedicated opportunity-event endpoint)
```

All three accept Close payloads. The generic dispatcher routes by
`event.object_type`; `contact.*` events are silently ignored.

### Leads
```
GET    /api/leads                          List all leads
GET    /api/leads/{lead_id}                Get a lead
POST   /api/leads                          Create a lead manually
DELETE /api/leads/{lead_id}                Delete a lead

POST   /api/leads/{lead_id}/lead-status         Change lifecycle status
POST   /api/leads/{lead_id}/opportunity-status  Change opportunity status

POST   /api/webhooks/booking               Lightweight direct-booking webhook (non-CRM)
```

### Workflows + jobs + logs
```
GET    /api/workflows                      List available sequences
POST   /api/workflows/trigger              Manually schedule a workflow for a lead

GET    /api/jobs                           List all jobs (sorted by run_at)
GET    /api/jobs/{job_id}                  Get a job
POST   /api/jobs/{job_id}/cancel           Cancel a pending job
POST   /api/jobs/{job_id}/retry            Reset and re-schedule a failed job

GET    /api/logs?limit=200                 Recent log entries
```

### UI
```
GET    /                                   The HTMX shell
GET    /ui/dashboard | /ui/leads | /ui/jobs | /ui/logs | /ui/templates
POST   /ui/leads, /ui/leads/{id}/trigger, /ui/leads/{id}/delete
GET/POST  /ui/templates/*
```

---

## Close CRM integration

### One-time setup

1. **Find your Close API key**: Settings → Developer → API Keys → Create new.
2. **Find custom field IDs**: Settings → Custom Fields → click each field; the
   URL contains `cf_xxx`. Add to `.env`.
3. **Find status IDs**: Settings → Pipelines / Lead Statuses. URLs contain
   `stat_xxx`. Add to `.env`.
4. **Start the engine** and **expose it** via ngrok (or your prod URL):
   ```bash
   ngrok http 8000
   ```
5. **Register webhook subscriptions in Close**:

   ```bash
   NGROK_URL="https://YOUR-NGROK-URL.ngrok-free.app"
   CLOSE_API_KEY="$(grep '^CLOSE_API_KEY=' .env | cut -d= -f2-)"

   # One subscription that handles everything:
   curl -X POST https://api.close.com/api/v1/webhook/ \
     -u "${CLOSE_API_KEY}:" \
     -H 'Content-Type: application/json' \
     -d "{
       \"url\": \"${NGROK_URL}/api/webhooks/close\",
       \"events\": [
         {\"object_type\": \"lead\",        \"action\": \"created\"},
         {\"object_type\": \"lead\",        \"action\": \"updated\"},
         {\"object_type\": \"opportunity\", \"action\": \"created\"},
         {\"object_type\": \"opportunity\", \"action\": \"updated\"}
       ]
     }"
   ```

   List subscriptions any time:
   ```bash
   curl -s -u "${CLOSE_API_KEY}:" https://api.close.com/api/v1/webhook/ | python -m json.tool
   ```

### How transitions are handled

The engine has **two independent status axes** that mirror Close's data model:

| Axis | Field | Status enum |
|---|---|---|
| Lead lifecycle | `Lead.status` | `potential`, `nurture`, `qualified`, `client`, `unqualified`, `lost`, `bad_fit`, `cancelled` |
| Per-opportunity | `Lead.opportunity_status` | `call_booked`, `call_rescheduled`, `call_cancelled`, `call_completed`, `contact_sent`, `won`, `lost`, `follow_up` |

| Event | Workflow triggered | Pending jobs cancelled |
|---|---|---|
| Lead → `potential` / `nurture` | (disabled, manual only) | Nurture-sequence jobs only |
| Lead → `qualified` | None | Nurture-sequence jobs only |
| Lead → `client` / `unqualified` / `lost` / `bad_fit` / `cancelled` | None | **All pending jobs** (terminal) |
| Opp → `call_booked` | **Booked Call Sequence** | All pending jobs (fresh start) |
| Opp → `call_rescheduled` | **Booked Call Sequence** | Booked-call jobs (re-schedule with new call_at) |
| Opp → `call_cancelled` / `call_completed` | None | Booked-call jobs only |
| Opp → `won` / `lost` | None | All pending jobs (terminal) |
| Opp → `contact_sent` / `follow_up` | None | Nothing |

**Cancellations are scoped** so a lead-status change can never accidentally
wipe a freshly-scheduled Booked Call Sequence (and vice-versa). This makes
event ordering irrelevant — Close can fire `lead.updated` before or after
`opportunity.updated` and the final state is the same.

---

## Adding a new CRM

The architecture is built around a `CrmAdapter` protocol so each new CRM is
a single-file addition.

1. **Create `app/adapters/<crm>.py`** implementing:
   ```python
   from app.adapters.base import CrmAdapter, NormalizedWebhookEvent

   class MyCrmAdapter:
       name = "mycrm"

       def verify_webhook(self, body, headers): ...
       def parse_event(self, payload): ...        # returns NormalizedWebhookEvent
       def fetch_contact(self, external_id): ...  # returns (name, email, phone)

   adapter = MyCrmAdapter()
   ```

2. **Add env vars** for that CRM's auth and status/field IDs in `app/config.py`.

3. **Add 1–3 lines in `app/routes/api.py`** to register routes:
   ```python
   from app.adapters.mycrm import adapter as mycrm_adapter

   @router.post("/webhooks/mycrm")
   async def mycrm_webhook(request: Request) -> dict[str, Any]:
       return await _handle_crm_webhook(mycrm_adapter, request)
   ```

The state machine, scheduler, workflows, and templates are **provider-agnostic**
— none of them change when you add a new CRM. Each adapter maps the CRM's
proprietary statuses onto the canonical `LeadStatus` and `OpportunityStatus`
enums; everything downstream treats all leads identically.

See `app/adapters/close.py` for a complete reference implementation.

---

## UI tour

The web UI lives at `http://localhost:8000` and has five tabs:

| Tab | What it shows | What you can do |
|---|---|---|
| **Dashboard** | Counts of leads / jobs by status, latest log entries | At-a-glance health |
| **Leads** | All leads with both status axes | Create test leads, trigger workflows manually, delete |
| **Templates** | Every email/SMS template + which workflows use each | Edit, preview live, create, delete (with reference protection) |
| **Jobs** | All jobs sorted by run time | Cancel pending jobs, retry failed jobs |
| **Logs** | Most recent 200 entries | Audit what's happened |

The Trigger button in the Leads tab shows a "Triggering…" indicator while
the request is in flight and a green success banner above the table when the
workflow has been scheduled.

---

## Operations

### Restarts are safe

On startup, the scheduler reads `data/jobs.json` and re-registers every job
whose status is `scheduled` with APScheduler at its original `run_at`. Jobs
that were past-due when the process restarts fire within ~1 second of boot.
**No work is lost across restarts.**

### Retention

- **Leads** are swept at 03:00 UTC daily (and once on boot). Anything older
  than `LEAD_RETENTION_DAYS` (default 180) is deleted.
- **Jobs**: terminal jobs (succeeded / failed / cancelled) are capped at
  `MAX_JOB_ENTRIES` (default 5000). Active jobs (`scheduled` / `running`)
  are *never* trimmed. Oldest terminal jobs are dropped first by `updated_at`.
- **Logs**: capped at 5000 entries; oldest dropped on write when exceeded.

### Retries

Failed sends are re-scheduled `RETRY_DELAY_SECONDS` later, up to
`MAX_SEND_ATTEMPTS` (default 3). After that the job is marked `failed`. You
can manually retry from the Jobs tab — that resets attempts and re-schedules.

### Template edits while jobs are in flight

- **Template edits** (under `app/templates/`) take effect on the *next render*,
  so already-scheduled jobs will use the updated body when they fire.
- **Sequence edits** (under `app/sequences/`) only affect *future triggers*.
  Already-scheduled jobs have their `run_at` baked in; changing
  `delay_minutes` doesn't reach back in time.

### Logging

All meaningful events land in `data/logs.json` as structured entries:
`lead.created`, `status.transition`, `workflow.scheduled`, `step.skipped`,
`job.sent`, `job.retry`, `job.failed`, `job.cancelled`,
`close.webhook.*`, etc. The UI Logs tab shows the last 200 sorted newest-first.

---

## License

Internal — see repository owner.
