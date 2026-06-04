# Store Intelligence API

Retail store analytics from raw CCTV footage — detection pipeline to live API.

## Setup

```bash
git clone <your-repo-url>
cd store-intelligence
cp .env.example .env
docker compose up --build
curl http://localhost:8000/health
```

## Running the Detection Pipeline

```bash
cd pipeline
pip install ultralytics opencv-python
python -m pipeline.detect --input clips/store_blr_entry.mp4 --store STORE_BLR_002 --camera CAM_ENTRY_01 --output events.jsonl
```

To process all clips at once:
```bash
bash pipeline/run.sh clips/ STORE_BLR_002
```

## Feeding Events into the API

```bash
python3 -c "
import json
events = [json.loads(l) for l in open('events.jsonl') if l.strip()]
print(json.dumps(events[:500]))
" | curl -X POST http://localhost:8000/events/ingest \
     -H 'Content-Type: application/json' \
     -d @-
```

## API Endpoints

```bash
# Health check
curl http://localhost:8000/health

# Store metrics
curl http://localhost:8000/stores/STORE_BLR_002/metrics

# Conversion funnel
curl http://localhost:8000/stores/STORE_BLR_002/funnel

# Zone heatmap
curl http://localhost:8000/stores/STORE_BLR_002/heatmap

# Active anomalies
curl http://localhost:8000/stores/STORE_BLR_002/anomalies

# Ingest events
curl -X POST http://localhost:8000/events/ingest \
     -H 'Content-Type: application/json' \
     -d '[{"event_id":"...","store_id":"STORE_BLR_002",...}]'
```

## Running Tests

```bash
docker compose exec api pytest tests/ -v
```

Or locally:
```bash
pip install -r requirements.txt
pytest tests/ -v
```

## Architecture

See [docs/DESIGN.md](docs/DESIGN.md) for full architecture overview and AI-assisted decisions.
See [docs/CHOICES.md](docs/CHOICES.md) for model selection, schema design, and storage decisions.

# Store Intelligence

End-to-end retail analytics system: CCTV footage → live store metrics API.

## Quick Start (5 commands)

```bash
git clone <your-repo-url> store-intelligence && cd store-intelligence
cp .env.example .env
docker compose up --build -d
bash run.sh ./data http://localhost:8000
curl http://localhost:8000/stores/STORE_BLR_002/metrics
```

The API is now live at **http://localhost:8000**. Interactive docs at **http://localhost:8000/docs**.

---

## Running the Detection Pipeline

### With real CCTV clips

Install detection dependencies (outside Docker — runs on your machine):

```bash
pip install ultralytics opencv-python-headless boxmot
```

Process a single clip:

```bash
python pipeline/detect.py \
  --input  data/store_blr_entry.mp4 \
  --store  STORE_BLR_002 \
  --camera CAM_ENTRY_01 \
  --output events.jsonl
```

Process all clips and feed into the API automatically:

```bash
bash run.sh ./data http://localhost:8000
```

`run.sh` will:
1. Detect people in every `.mp4` / `.avi` file in `./data`
2. Emit structured events to `events.jsonl`
3. POST events to `/events/ingest` in batches of 500
4. Run a smoke-test against `/stores/STORE_BLR_002/metrics`

### Without clips (stub / smoke-test mode)

If `ultralytics` is not installed, `run.sh` automatically falls back to generating 20 synthetic events so you can verify the API end-to-end:

```bash
bash run.sh
```

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/events/ingest` | Ingest up to 500 events (idempotent by `event_id`) |
| `GET`  | `/stores/{id}/metrics` | Unique visitors, conversion rate, dwell, queue depth |
| `GET`  | `/stores/{id}/funnel` | Entry → Zone → Billing → Purchase funnel |
| `GET`  | `/stores/{id}/heatmap` | Zone visit frequency normalised 0–100 |
| `GET`  | `/stores/{id}/anomalies` | Active anomalies with severity + suggested action |
| `GET`  | `/health` | Service status + stale feed detection |

### Example — ingest events

```bash
curl -X POST http://localhost:8000/events/ingest \
  -H "Content-Type: application/json" \
  -d @events.jsonl   # wrap in [] first if sending as array
```

### Example — metrics

```bash
curl http://localhost:8000/stores/STORE_BLR_002/metrics
```

```json
{
  "unique_visitors": 42,
  "conversion_rate": 0.31,
  "avg_dwell_per_zone": {"SKINCARE": 45200.0, "BILLING": 12400.0},
  "queue_depth": 3,
  "abandonment_rate": 0.12
}
```

---

## Running Tests

```bash
pip install pytest pytest-asyncio httpx
pytest tests/ -v --tb=short
```

Test coverage target: >70% statement coverage.

Each test file includes a `# PROMPT:` block at the top documenting the AI prompt used to generate it and the changes made afterwards.

---

## Live Dashboard (Part E)

A terminal dashboard updates in real time as events flow in:

```bash
pip install rich
python dashboard.py --store STORE_BLR_002 --api http://localhost:8000
```

---

## Repository Structure

```
store-intelligence/
├── pipeline/
│   ├── detect.py       # YOLOv8 + ByteTrack detection
│   ├── tracker.py      # Re-ID, zone assignment, dwell tracking
│   ├── emit.py         # Schema validation + JSONL emission
│   └── run.sh          # One command: clips → events → API
├── app/
│   ├── main.py         # FastAPI entrypoint
│   ├── models.py       # Pydantic event schema
│   ├── ingestion.py    # Ingest, dedup, session tracking
│   ├── metrics.py      # Real-time metric computation
│   ├── funnel.py       # Funnel + session deduplication
│   ├── anomalies.py    # Anomaly detection
│   └── health.py       # Health + stale feed monitoring
├── tests/
│   ├── test_pipeline.py
│   ├── test_metrics.py
│   └── test_anomalies.py
├── docs/
│   ├── DESIGN.md
│   └── CHOICES.md
├── data/               # Drop CCTV clips here (gitignored)
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `STORE_INTEL_DB` | `store_intelligence.db` | SQLite database path |
| `DATABASE_URL` | — | Override with `sqlite:///path` or `postgresql://...` |
| `PORT` | `8000` | API listen port |

---

## Architecture

See [docs/DESIGN.md](docs/DESIGN.md) for full architecture and AI-assisted decision log.  
See [docs/CHOICES.md](docs/CHOICES.md) for model selection, schema design, and storage rationale.