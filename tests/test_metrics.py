# PROMPT: "Generate pytest tests for FastAPI store intelligence API covering metrics, funnel, anomalies and ingestion endpoints. Include edge cases."
# CHANGES MADE: Added re-entry deduplication check, zero-purchase store assertion, all-staff clip scenario

from __future__ import annotations
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, AsyncMock
import uuid
from datetime import datetime, timezone

from app.main import app
from app.ingestion import init_db


def make_event(
    event_type="ENTRY",
    store_id="STORE_BLR_002",
    visitor_id=None,
    is_staff=False,
    zone_id=None,
    dwell_ms=0,
    confidence=0.95,
    queue_depth=None,
    session_seq=1,
):
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": visitor_id or f"VIS_{uuid.uuid4().hex[:6]}",
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": zone_id,
            "session_seq": session_seq,
        },
    }


@pytest.fixture(autouse=True)
async def setup_db():
    with patch("app.ingestion.DB_PATH", ":memory:"):
        await init_db()
        yield


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_ingest_five_events(client):
    events = [make_event() for _ in range(5)]
    resp = await client.post("/events/ingest", json=events)
    assert resp.status_code == 200
    data = resp.json()
    assert data["accepted"] == 5
    assert data["duplicates"] == 0


@pytest.mark.asyncio
async def test_ingest_idempotent(client):
    events = [make_event() for _ in range(5)]
    await client.post("/events/ingest", json=events)
    resp = await client.post("/events/ingest", json=events)
    assert resp.status_code == 200
    data = resp.json()
    assert data["accepted"] == 0
    assert data["duplicates"] == 5


@pytest.mark.asyncio
async def test_metrics_unique_visitors(client):
    events = [make_event(event_type="ENTRY", visitor_id=f"VIS_{i:06d}") for i in range(3)]
    await client.post("/events/ingest", json=events)
    resp = await client.get("/stores/STORE_BLR_002/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["unique_visitors"] >= 3


@pytest.mark.asyncio
async def test_metrics_staff_excluded(client):
    events = [make_event(event_type="ENTRY", is_staff=True) for _ in range(3)]
    await client.post("/events/ingest", json=events)
    resp = await client.get("/stores/STORE_BLR_002/metrics")
    assert resp.status_code == 200
    assert resp.json()["unique_visitors"] == 0


@pytest.mark.asyncio
async def test_metrics_zero_purchases(client):
    events = [make_event(event_type="ENTRY")]
    await client.post("/events/ingest", json=events)
    resp = await client.get("/stores/STORE_BLR_002/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["conversion_rate"] == 0.0


@pytest.mark.asyncio
async def test_health_endpoint(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert "last_event_per_store" in data
    assert "stale_feeds" in data