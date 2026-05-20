# CLAUDE.md

## Project Overview
Lead Nurture Engine

Deterministic Python automation system replacing Make.com lead nurture workflows.
- No AI.
- No LLMs.
- No database.

# Goal
Increase booked-call show-up rates through automated reminders, value reinforcement, and deterministic follow-up sequences.

# Technical Constraints
Required:
- Python
- FastAPI
- APScheduler
- Jinja2
- HTMX
- JSON persistence


Forbidden:
- OpenAI
- Claude APIs
- LLMs
- AI generation
- Databases
- Redis
- Celery
- Kubernetes

# Architecture
Single-service deterministic workflow engine.

Responsibilities:
1. Accept booking webhooks
2. Store leads in JSON (deletes 6 months older data)
3. Schedule jobs
4. Send deterministic messages
5. Provide manual testing UI
6. Persist jobs across restarts



Project Structure
lead-nurture-agent/
    app/
        main.py 
        config.py 
        scheduler.py
        workflow.py
        storage.py
        models.py

        routes/
            api.py
            ui.py

        services/
            email_service.py
            sms_service.py

        templates/
            index.html

        sequences/
            booked_call.json  
            
        data/
            leads.json      
            jobs.json      
            logs.json

# Lead Model
{  
    "id": "uuid",
    "name": "John Smith",
    "email": "john@example.com",
    "phone": "+15555555555",
    "status": "booked"
}

# Workflow Engine
Workflow definitions live in:
app/sequences/

Example:
{  
"name": "Booked Call Sequence",  
    "steps": [    
        {      
            "type": "email",
            "delay_minutes": 0,
            "template": "confirmation_email"
        }  
    ]
}

# Scheduler Requirements
Use APScheduler.

Core responsibilities:
1. Schedule jobs
2. Restore jobs after restart
3. Retry failed sends
4. Persist execution state


# Email System
Use Jinja2 templates.

Allowed variables:
1. lead_name
2. call_time
3. calendar_link
4. case_study_link

No AI-generated copy allowed.

# SMS System
Deterministic plain-text templates only.
Example:
Hi {{lead_name}} — you're confirmed for {{call_time}}.

# Manual Testing UI

Required pages:
1. Dashboard
2. Leads
3. Jobs
4. Logs

Required actions:
1. Create test lead
2. Trigger workflows
3. Cancel jobs
4. Retry sends

# Persistence Rules
No database.

Use only:
1. data/leads.json
2. data/jobs.json
3. data/logs.json

# Run Commands
Install:
pip install -r requirements.txt

Run:
uvicorn app.main:app --reload

Open:
http://localhost:8000

# Final Rule
This system is NOT an AI agent platform.

Every decision must come from:
1. rules
2. schedules
3. templates
4. explicit conditions

Never introduce probabilistic behavior.