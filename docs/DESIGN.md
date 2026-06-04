# Store Intelligence — Design Document

## Overview

Store Intelligence is an end-to-end retail analytics system that transforms raw CCTV footage into actionable store metrics. It bridges the analytics gap between Apex Retail's mature online channel and its offline stores by providing real-time visitor tracking, zone analytics, conversion measurement, and anomaly detection.

## Architecture

The system has four layers:

1. **Detection Pipeline** — processes CCTV clips using YOLOv8 + ByteTrack, emits structured JSON events
2. **Event Ingestion** — FastAPI endpoint validates, deduplicates, and stores events in SQLite
3. **Intelligence API** — computes real-time metrics, funnel analytics, heatmaps, and anomalies
4. **Health Layer** — monitors feed staleness and service status

## Data Flow

Raw CCTV Clips (.mp4)
↓
pipeline/detect.py (YOLOv8 detection + ByteTrack tracking)
↓
pipeline/emit.py (schema validation → events.jsonl)
↓
POST /events/ingest (dedup by event_id, session tracking, POS correlation)
↓
SQLite (events table + sessions table + pos_transactions table)
↓
GET /stores/{id}/metrics|funnel|heatmap|anomalies
↓
Dashboard / Monitoring

## Component Descriptions

**detect.py** — Opens video with OpenCV, runs person detection, assigns visitor_id tokens using a lightweight Re-ID based on appearance hashing. Classifies staff by HSV colour range of bounding box crop. Emits ENTRY, EXIT, ZONE_DWELL, REENTRY events.

**emit.py** — Validates every event against the StoreEvent Pydantic model before writing to JSONL. Invalid events are logged and skipped, never silently dropped.

**ingestion.py** — Async SQLite layer using aiosqlite. Idempotent by event_id. Tracks sessions, handles POS correlation by matching billing zone presence within a 5-minute window before each transaction timestamp.

**metrics.py** — Computes unique visitors, conversion rate, avg dwell per zone, queue depth, abandonment rate in real time from the events table. Excludes is_staff=true. Safe against zero-purchase stores.

**funnel.py** — Session-level funnel: Entry → Zone Visit → Billing Queue → Purchase. Re-entries are deduplicated by visitor_id so a customer who re-enters is counted once.

**anomalies.py** — Three anomaly detectors: BILLING_QUEUE_SPIKE (queue depth threshold), CONVERSION_DROP (vs 7-day average), DEAD_ZONE (no zone events in 30 minutes). Each anomaly includes a suggested_action string.

**health.py** — Returns last event timestamp per store and flags STALE_FEED if any store has had no events in 10+ minutes.

## Production Decisions

- **Structured logging** via structlog — every request logs trace_id, store_id, endpoint, latency_ms, status_code as JSON
- **Idempotency** — POST /events/ingest is safe to call multiple times with the same payload
- **Graceful degradation** — aiosqlite errors return HTTP 503 with structured body, no raw tracebacks
- **Docker** — single `docker compose up` starts everything, SQLite persisted via volume mount

## AI-Assisted Decisions

### 1. Detection Model Selection
When evaluating detection models, I used Claude to compare YOLOv8m vs RT-DETR on the criteria that matter for this use case: inference speed, ease of ByteTrack integration, and community support. AI suggested RT-DETR for higher accuracy. I overrode this and chose YOLOv8m because inference latency matters more than marginal accuracy gains for a counting use case, and the ultralytics ecosystem made ByteTrack integration a single function call. This saved significant implementation time.

### 2. Event Schema — Nested Metadata
AI initially suggested a fully flat event schema to simplify queries. I disagreed. The nested metadata block keeps core fields clean and queryable while allowing event-type-specific data (queue_depth for BILLING_QUEUE_JOIN, session_seq for ordering) to live where it belongs. Schema validation is cleaner because metadata fields are only required for relevant event types.

### 3. SQLite vs PostgreSQL
AI recommended PostgreSQL for production readiness. I chose SQLite with aiosqlite for this submission because it requires zero infrastructure, runs in a single container, and handles the event volume of 40 stores comfortably. The DB_PATH is an environment variable so swapping to PostgreSQL requires only changing the connection string and swapping aiosqlite for asyncpg.