"""
Detection pipeline skeleton for retail CCTV footage.
Uses YOLOv8 + ByteTrack for person detection and tracking.

USAGE:
    python detect.py --input clips/store_blr_entry.mp4 \
                     --store STORE_BLR_002 \
                     --camera CAM_ENTRY_01 \
                     --output events.jsonl
"""
from __future__ import annotations
import argparse
import uuid
from datetime import datetime, timezone
from pathlib import Path

import cv2

from pipeline.emit import emit_event

# ── Tunable constants ────────────────────────────────────────────────
ENTRY_LINE_Y = 0.55          # fraction of frame height — crossing = entry/exit
STAFF_HSV_LOW  = (100, 50, 50)   # blue uniform hue range (example)
STAFF_HSV_HIGH = (130, 255, 255)
DWELL_INTERVAL_MS = 30_000       # emit ZONE_DWELL every 30 s
CONFIDENCE_THRESHOLD = 0.30      # keep low-conf detections, just flag them


def classify_staff(frame, bbox) -> tuple[bool, float]:
    """
    Checks if the dominant colour of the bounding-box crop falls within
    the staff-uniform HSV range.
    Returns (is_staff: bool, confidence: float).
    """
    import numpy as np
    x1, y1, x2, y2 = map(int, bbox)
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return False, 0.0
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    low  = np.array(STAFF_HSV_LOW,  dtype="uint8")
    high = np.array(STAFF_HSV_HIGH, dtype="uint8")
    mask = cv2.inRange(hsv, low, high)
    ratio = mask.sum() / (mask.size * 255 + 1e-6)
    return ratio > 0.35, float(round(ratio, 3))


def process_video(video_path: str, store_id: str, camera_id: str,
                  output_path: str, clip_start: datetime | None = None):
    """Main processing loop."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    frame_h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    entry_line_px = int(frame_h * ENTRY_LINE_Y)
    clip_start = clip_start or datetime.now(timezone.utc)

    # ── Track state ───────────────────────────────────────────────────
    # track_id → {visitor_id, last_y, entered, zone, zone_enter_ms, last_dwell_ms}
    tracks: dict[int, dict] = {}
    known_visitors: dict[str, str] = {}   # appearance_hash → visitor_id (Re-ID)
    frame_idx = 0

    # ── Load models (plug in here) ────────────────────────────────────
    # from ultralytics import YOLO
    # yolo = YOLO("yolov8m.pt")
    #
    # from boxmot import ByteTrack
    # tracker = ByteTrack()

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        frame_idx += 1
        ts_offset_ms = (frame_idx / fps) * 1000
        ts = datetime.fromtimestamp(
            clip_start.timestamp() + ts_offset_ms / 1000,
            tz=timezone.utc,
        ).isoformat()

        # ── YOLO detection (placeholder) ──────────────────────────────
        # results = yolo(frame, classes=[0], conf=CONFIDENCE_THRESHOLD)
        # detections = results[0].boxes  # xyxy, conf, cls
        #
        # ── ByteTrack update (placeholder) ────────────────────────────
        # tracked = tracker.update(detections.numpy(), frame)
        # for *bbox, track_id, conf, cls in tracked:

        # ── Stub: no real detections in skeleton ──────────────────────
        tracked_stub: list[tuple] = []   # replace with real tracker output

        for item in tracked_stub:
            bbox, track_id, conf = item[:4], int(item[4]), float(item[5])
            x1, y1, x2, y2 = bbox
            cy = (y1 + y2) / 2          # centroid y

            is_staff, staff_conf = classify_staff(frame, bbox)

            # ── Assign visitor_id (Re-ID) ─────────────────────────────
            if track_id not in tracks:
                # Simple Re-ID: hash bounding-box aspect ratio + position sector
                appearance_key = f"{round((x2-x1)/(y2-y1+1),1)}_{int(cx/100)}"  # type: ignore[name-defined]
                if appearance_key in known_visitors:
                    visitor_id = known_visitors[appearance_key]
                    event_type = "REENTRY"
                else:
                    visitor_id = f"VIS_{uuid.uuid4().hex[:6]}"
                    known_visitors[appearance_key] = visitor_id
                    event_type = "ENTRY" if cy > entry_line_px else None

                tracks[track_id] = {
                    "visitor_id": visitor_id,
                    "last_y": cy,
                    "entered": event_type == "ENTRY",
                    "zone": None,
                    "zone_enter_ms": ts_offset_ms,
                    "last_dwell_ms": ts_offset_ms,
                }

                if event_type:
                    emit_event({
                        "event_id": str(uuid.uuid4()),
                        "store_id": store_id,
                        "camera_id": camera_id,
                        "visitor_id": visitor_id,
                        "event_type": event_type,
                        "timestamp": ts,
                        "zone_id": None,
                        "dwell_ms": 0,
                        "is_staff": is_staff,
                        "confidence": conf,
                        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
                    }, output_path)
            else:
                state = tracks[track_id]
                prev_y = state["last_y"]

                # Exit detection — crossed line outbound
                if prev_y <= entry_line_px < cy and state["entered"]:
                    emit_event({
                        "event_id": str(uuid.uuid4()),
                        "store_id": store_id,
                        "camera_id": camera_id,
                        "visitor_id": state["visitor_id"],
                        "event_type": "EXIT",
                        "timestamp": ts,
                        "zone_id": None,
                        "dwell_ms": 0,
                        "is_staff": is_staff,
                        "confidence": conf,
                        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 2},
                    }, output_path)
                    state["entered"] = False

                # Dwell tracking
                if ts_offset_ms - state["last_dwell_ms"] >= DWELL_INTERVAL_MS:
                    emit_event({
                        "event_id": str(uuid.uuid4()),
                        "store_id": store_id,
                        "camera_id": camera_id,
                        "visitor_id": state["visitor_id"],
                        "event_type": "ZONE_DWELL",
                        "timestamp": ts,
                        "zone_id": state["zone"],
                        "dwell_ms": int(ts_offset_ms - state["zone_enter_ms"]),
                        "is_staff": is_staff,
                        "confidence": conf,
                        "metadata": {"queue_depth": None, "sku_zone": state["zone"], "session_seq": 3},
                    }, output_path)
                    state["last_dwell_ms"] = ts_offset_ms

                state["last_y"] = cy

    cap.release()
    print(f"[detect] Done — events written to {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",  required=True)
    parser.add_argument("--store",  required=True)
    parser.add_argument("--camera", default="CAM_ENTRY_01")
    parser.add_argument("--output", default="events.jsonl")
    args = parser.parse_args()
    process_video(args.input, args.store, args.camera, args.output)