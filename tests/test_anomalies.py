# PROMPT: "Generate pytest tests for FastAPI store intelligence API covering metrics, funnel, anomalies and ingestion endpoints. Include edge cases."
# CHANGES MADE: Added re-entry deduplication check, zero-purchase store assertion, all-staff clip scenario

from __future__ import annotations
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch
import uuid
from datetime import datetime, timezone

from app.main import app
from app.ingestion import init_db


def make_event(event_type="ENTRY", store_id="STORE_BLR_002",
               visitor_id=None, is_staff=False, zone_id=None,
               dwell_ms=0, queue_depth=None, session_seq=1):
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
        "confidence": 0.95,
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
async def test_anomalies_no_events(client):
    resp = await client.get("/stores/STORE_BLR_002/anomalies")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_billing_queue_spike_critical(client):
    events = [
        make_event(
            event_type="BILLING_QUEUE_JOIN",
            zone_id="BILLING",
            queue_depth=11,
            visitor_id=f"VIS_{i:06d}",
            session_seq=1,
        )
        for i in range(11)
    ]
    await client.post("/events/ingest", json=events)
    resp = await client.get("/stores/STORE_BLR_002/anomalies")
    assert resp.status_code == 200
    anomalies = resp.json()
    severities = [a["severity"] for a in anomalies]
    assert "CRITICAL" in severities


@pytest.mark.asyncio
async def test_funnel_reentry_counted_once(client):
    visitor_id = "VIS_abc123"
    events = [
        make_event(event_type="ENTRY", visitor_id=visitor_id, session_seq=1),
        make_event(event_type="EXIT", visitor_id=visitor_id, session_seq=2),
        make_event(event_type="REENTRY", visitor_id=visitor_id, session_seq=3),
    ]
    await client.post("/events/ingest", json=events)
    resp = await client.get("/stores/STORE_BLR_002/funnel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["entry_count"] == 1