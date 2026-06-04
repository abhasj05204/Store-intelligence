from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import aiosqlite
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.anomalies import get_anomalies
from app.funnel import get_store_funnel
from app.ingestion import get_db, ingest_events, init_db
from app.metrics import get_store_heatmap, get_store_metrics
from app.health import get_health
from app.models import (
    AnomalyResponse,
    FunnelResponse,
    HeatmapResponse,
    HealthResponse,
    MetricsResponse,
    StoreEvent,
)

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),
    logger_factory=structlog.PrintLoggerFactory(),
)
logger = structlog.get_logger()


def _store_id_from_path(path: str) -> str | None:
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "stores":
        return parts[1]
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Store Intelligence API", lifespan=lifespan)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    trace_id = str(uuid4())
    store_id = _store_id_from_path(request.url.path)
    start = time.perf_counter()

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(trace_id=trace_id)

    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.info(
            "request_completed",
            trace_id=trace_id,
            store_id=store_id,
            endpoint=request.url.path,
            latency_ms=latency_ms,
            status_code=status_code,
        )


@app.exception_handler(aiosqlite.Error)
async def sqlite_error_handler(_request: Request, exc: aiosqlite.Error):
    logger.error("database_error", error=str(exc))
    return JSONResponse(
        status_code=503,
        content={
            "error": "service_unavailable",
            "message": "Database operation failed",
        },
    )


@app.post("/events/ingest")
async def ingest(events: list[StoreEvent]) -> dict[str, Any]:
    return await ingest_events(events)


@app.get("/stores/{store_id}/metrics", response_model=MetricsResponse)
async def store_metrics(store_id: str) -> MetricsResponse:
    async with get_db() as db:
        return await get_store_metrics(store_id, db)


@app.get("/stores/{store_id}/funnel", response_model=FunnelResponse)
async def store_funnel(store_id: str) -> FunnelResponse:
    async with get_db() as db:
        return await get_store_funnel(store_id, db)


@app.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse)
async def store_heatmap(store_id: str) -> HeatmapResponse:
    async with get_db() as db:
        return await get_store_heatmap(store_id, db)


@app.get("/stores/{store_id}/anomalies", response_model=list[AnomalyResponse])
async def store_anomalies(store_id: str) -> list[AnomalyResponse]:
    async with get_db() as db:
        return await get_anomalies(store_id, db)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    async with get_db() as db:
        return await get_health(db)
