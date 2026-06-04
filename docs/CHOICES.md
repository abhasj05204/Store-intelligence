# Architecture Choices

## Decision 1: Detection Model Selection

**Options considered:** YOLOv8n, YOLOv8m, RT-DETR-L, MediaPipe Pose

**What AI suggested:** RT-DETR-L — AI argued it has higher mAP on COCO benchmarks and better handles occlusion cases, which are explicitly called out in the challenge dataset (partial occlusion at billing counter, group entry).

**What I chose:** YOLOv8m

**Why:** For a people-counting use case in a retail store, throughput and integration simplicity matter more than squeezing out the last 2% mAP. Three concrete reasons:

1. ByteTrack integration with ultralytics is one line — `tracker="bytetrack"` in the YOLO predict call. With RT-DETR I would have needed to wire the tracker manually.
2. YOLOv8m runs at ~45fps on a mid-range GPU and ~8fps on CPU. RT-DETR-L requires a GPU to hit real-time speeds.
3. The ultralytics ecosystem has extensive documentation for exactly this use case (retail foot traffic), making edge case handling (occlusion, group entry) easier to debug.

**Where I might change this:** If the client required precise individual tracking in dense crowds (festival retail, Black Friday), I would revisit RT-DETR or switch to a specialised crowd-counting model.

---

## Decision 2: Event Schema Design

**Options considered:**
- Fully flat schema — all fields at top level
- Nested metadata — core fields flat, event-type-specific data in metadata object

**What AI suggested:** Flat schema. AI argued it simplifies SQL queries and removes one level of JSON parsing.

**What I chose:** Nested metadata block

**Why:** The challenge has 8 event types with very different data requirements. BILLING_QUEUE_JOIN needs queue_depth. ZONE_DWELL needs dwell_ms. ENTRY and EXIT need neither. A flat schema forces nullable columns for every event-type-specific field at the top level, which makes schema validation harder and the event contract less clear.

The nested approach means:
- Core fields (event_id, store_id, visitor_id, timestamp, event_type) are always present and always the same type
- metadata is validated per event type
- The API can index on core fields and deserialise metadata only when needed

The one trade-off is that SQLite queries on metadata fields require JSON extraction functions. For the query patterns in this API (aggregate by store_id, filter by event_type, group by zone_id) this is not a bottleneck.

---

## Decision 3: API Storage Layer

**Options considered:** PostgreSQL 16, SQLite + aiosqlite, Redis Streams + SQLite

**What AI suggested:** PostgreSQL — AI cited ACID compliance, concurrent write performance, and production credibility.

**What I chose:** SQLite + aiosqlite

**Why:** Three reasons:

1. **Zero infrastructure.** SQLite runs inside the API container. No separate database container, no connection string management, no migration tooling required for this submission. `docker compose up` starts one container, not three.

2. **Sufficient performance.** The ingest endpoint accepts batches of 500 events. At 40 stores each sending a batch every 30 seconds, peak write rate is roughly 670 events/second. SQLite with WAL mode handles this comfortably on a single machine.

3. **Easy to replace.** DB_PATH is read from the DATABASE_URL environment variable. Replacing SQLite with PostgreSQL requires changing the env var and swapping aiosqlite for asyncpg in ingestion.py — no application logic changes.

**Where I would change this:** At genuine production scale with multiple API replicas behind a load balancer, SQLite breaks immediately because it is a file on one machine. That is the correct trigger to migrate to PostgreSQL.