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