"""FastAPI application entry point.

Run with:
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app import scheduler, storage
from app.config import settings
from app.routes import api, ui


logging.basicConfig(
    level=settings.app_log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    storage.ensure_data_files()
    scheduler.start_scheduler()
    try:
        yield
    finally:
        scheduler.shutdown_scheduler()


app = FastAPI(
    title="Lead Nurture Engine",
    version="0.1.0",
    description="Deterministic lead nurture automation. No AI, no DB.",
    lifespan=lifespan,
)

# Mount the API router under both /api and /api/v1 so existing webhook
# subscriptions registered with either prefix continue to work.
app.include_router(api.router, prefix="/api")
app.include_router(api.router, prefix="/api/v1")
app.include_router(ui.router)


@app.get("/health")
def root_health() -> dict[str, str]:
    """Root-level liveness check for ngrok/uptime probes."""
    return {"status": "ok"}
