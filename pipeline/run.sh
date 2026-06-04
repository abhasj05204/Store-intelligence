#!/usr/bin/env bash
# run.sh — process all CCTV clips and feed events into the Store Intelligence API
# Usage:  bash run.sh [CLIPS_DIR] [API_URL]
# Example: bash run.sh ./data http://localhost:8000

set -euo pipefail

CLIPS_DIR="${1:-./data}"
API_URL="${2:-http://localhost:8000}"
EVENTS_FILE="events.jsonl"
STORE_ID="STORE_BLR_002"

echo "=== Store Intelligence Pipeline ==="
echo "Clips directory : $CLIPS_DIR"
echo "API endpoint    : $API_URL"
echo ""

# ── 1. Check dependencies ─────────────────────────────────────────────────────
python -c "import cv2, ultralytics" 2>/dev/null || {
    echo "[warn] ultralytics / opencv not installed — running in stub/simulation mode"
    STUB_MODE=true
}

# ── 2. Process each clip ──────────────────────────────────────────────────────
> "$EVENTS_FILE"   # truncate / create

declare -A CAMERA_MAP=(
    ["entry"]="CAM_ENTRY_01"
    ["floor"]="CAM_FLOOR_01"
    ["billing"]="CAM_BILLING_01"
)

if [[ "${STUB_MODE:-false}" == "true" ]]; then
    echo "[stub] Generating synthetic events for API smoke-test..."
    python - <<'PYEOF'
import json, uuid, random
from datetime import datetime, timezone, timedelta

store_id = "STORE_BLR_002"
events = []
base_ts = datetime.now(timezone.utc)

cameras = ["CAM_ENTRY_01", "CAM_FLOOR_01", "CAM_BILLING_01"]
zones   = ["SKINCARE", "HAIRCARE", "BILLING", "ENTRY"]

for i in range(20):
    visitor_id = f"VIS_{uuid.uuid4().hex[:6]}"
    ts = base_ts + timedelta(seconds=i * 30)

    # ENTRY
    events.append({
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": visitor_id,
        "event_type": "ENTRY",
        "timestamp": ts.isoformat(),
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": round(random.uniform(0.7, 0.98), 2),
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    })

    # ZONE_ENTER
    zone = random.choice(zones[:3])
    ts2 = ts + timedelta(seconds=15)
    events.append({
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_FLOOR_01",
        "visitor_id": visitor_id,
        "event_type": "ZONE_ENTER",
        "timestamp": ts2.isoformat(),
        "zone_id": zone,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": round(random.uniform(0.7, 0.98), 2),
        "metadata": {"queue_depth": None, "sku_zone": zone, "session_seq": 2},
    })

    # BILLING_QUEUE_JOIN for half the visitors
    if i % 2 == 0:
        ts3 = ts2 + timedelta(seconds=60)
        events.append({
            "event_id": str(uuid.uuid4()),
            "store_id": store_id,
            "camera_id": "CAM_BILLING_01",
            "visitor_id": visitor_id,
            "event_type": "BILLING_QUEUE_JOIN",
            "timestamp": ts3.isoformat(),
            "zone_id": "BILLING",
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": round(random.uniform(0.7, 0.98), 2),
            "metadata": {"queue_depth": i % 4 + 1, "sku_zone": "BILLING", "session_seq": 3},
        })

with open("events.jsonl", "w") as f:
    for e in events:
        f.write(json.dumps(e) + "\n")

print(f"[stub] Generated {len(events)} events → events.jsonl")
PYEOF

else
    for clip in "$CLIPS_DIR"/*.mp4 "$CLIPS_DIR"/*.avi 2>/dev/null; do
        [[ -f "$clip" ]] || continue
        fname=$(basename "$clip" | tr '[:upper:]' '[:lower:]')

        camera_id="CAM_ENTRY_01"
        for key in "${!CAMERA_MAP[@]}"; do
            if [[ "$fname" == *"$key"* ]]; then
                camera_id="${CAMERA_MAP[$key]}"
                break
            fi
        done

        echo "[detect] Processing: $clip  →  camera=$camera_id"
        python pipeline/detect.py \
            --input "$clip" \
            --store "$STORE_ID" \
            --camera "$camera_id" \
            --output "$EVENTS_FILE"
    done
fi

EVENT_COUNT=$(wc -l < "$EVENTS_FILE" | tr -d ' ')
echo ""
echo "[pipeline] Total events emitted: $EVENT_COUNT"

# ── 3. Feed events into API in batches of 500 ─────────────────────────────────
echo "[ingest] Sending events to $API_URL/events/ingest ..."

python - <<PYEOF
import json, sys, urllib.request, urllib.error

api_url = "$API_URL"
events_file = "$EVENTS_FILE"
batch_size = 500

with open(events_file) as f:
    lines = [l.strip() for l in f if l.strip()]

events = [json.loads(l) for l in lines]
total_accepted = 0
total_duplicates = 0
total_errors = []

for i in range(0, len(events), batch_size):
    batch = events[i:i + batch_size]
    payload = json.dumps(batch).encode()
    req = urllib.request.Request(
        f"{api_url}/events/ingest",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            total_accepted   += result.get("accepted", 0)
            total_duplicates += result.get("duplicates", 0)
            total_errors     += result.get("errors", [])
            print(f"  batch {i//batch_size + 1}: accepted={result.get('accepted')} "
                  f"duplicates={result.get('duplicates')} errors={len(result.get('errors', []))}")
    except urllib.error.URLError as e:
        print(f"  [error] Could not reach API: {e}")
        sys.exit(1)

print(f"\n[ingest] Done — accepted={total_accepted} duplicates={total_duplicates} errors={len(total_errors)}")
PYEOF

# ── 4. Quick smoke-test ───────────────────────────────────────────────────────
echo ""
echo "[smoke] GET $API_URL/stores/$STORE_ID/metrics"
curl -sf "$API_URL/stores/$STORE_ID/metrics" | python -m json.tool || echo "[warn] metrics endpoint check failed"

echo ""
echo "=== Pipeline complete ==="
echo "Dashboard: open $API_URL/docs in your browser"