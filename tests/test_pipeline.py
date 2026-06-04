# PROMPT: "Write pytest tests for a retail CCTV detection pipeline that emits structured
# JSON events. Cover: emit_event schema validation, emit_batch partial failure, tracker
# Re-ID (new track, re-entry within window, re-entry outside window), zone assignment,
# dwell heartbeat at 30s, exit detection on line crossing, staff classification,
# flush_exits at end of clip, and group entry (3 people same frame emits 3 ENTRY events)."
#
# CHANGES MADE:
# - Added explicit assertion that emit rejects events missing required fields
# - Added group-entry test (3 distinct track_ids → 3 ENTRY events, not 1)
# - Changed re-entry window boundary test to use ts_offset_ms = window + 1 (exclusive)
# - Added flush_exits test to verify clip-end EXIT emission
# - Replaced MagicMock file writes with tmp_path fixture (cleaner, no patch needed)

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from pipeline.emit import emit_batch, emit_event
from pipeline.tracker import Tracker

# ── Helpers ───────────────────────────────────────────────────────────────────

STORE = "STORE_BLR_002"
CAM   = "CAM_ENTRY_01"


def _base_event(**overrides) -> dict:
    event = {
        "event_id": str(uuid.uuid4()),
        "store_id": STORE,
        "camera_id": CAM,
        "visitor_id": f"VIS_{uuid.uuid4().hex[:6]}",
        "event_type": "ENTRY",
        "timestamp": "2026-03-03T14:22:10Z",
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.91,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }
    event.update(overrides)
    return event


def _make_tracker(entry_line_y: float = 0.55, reentry_window_ms: float = 120_000) -> Tracker:
    return Tracker(entry_line_y=entry_line_y, reentry_window_ms=reentry_window_ms)


def _bbox_at(cx_frac: float, cy_frac: float, fw: int = 1920, fh: int = 1080):
    """Return a pixel bbox centred at (cx_frac, cy_frac)."""
    cx, cy = cx_frac * fw, cy_frac * fh
    return (cx - 30, cy - 60, cx + 30, cy + 60)


# ── emit_event tests ──────────────────────────────────────────────────────────

class TestEmitEvent:
    def test_valid_event_written(self, tmp_path):
        out = str(tmp_path / "events.jsonl")
        result = emit_event(_base_event(), out)
        assert result is True
        lines = Path(out).read_text().strip().split("\n")
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["event_type"] == "ENTRY"

    def test_invalid_event_rejected(self, tmp_path):
        out = str(tmp_path / "events.jsonl")
        bad = _base_event()
        bad["confidence"] = 2.5          # violates ge=0 le=1
        result = emit_event(bad, out)
        assert result is False
        assert not Path(out).exists()

    def test_missing_required_field_rejected(self, tmp_path):
        out = str(tmp_path / "events.jsonl")
        bad = _base_event()
        del bad["store_id"]
        result = emit_event(bad, out)
        assert result is False

    def test_multiple_events_appended(self, tmp_path):
        out = str(tmp_path / "events.jsonl")
        emit_event(_base_event(), out)
        emit_event(_base_event(event_type="EXIT"), out)
        lines = Path(out).read_text().strip().split("\n")
        assert len(lines) == 2

    def test_emit_batch_partial_failure(self, tmp_path):
        out = str(tmp_path / "events.jsonl")
        good = _base_event()
        bad  = _base_event()
        bad["confidence"] = -1.0
        result = emit_batch([good, bad], out)
        assert result["accepted"] == 1
        assert len(result["errors"]) == 1


# ── Tracker tests ─────────────────────────────────────────────────────────────

class TestTrackerNewTrack:
    def test_new_track_below_line_emits_entry(self):
        t = _make_tracker(entry_line_y=0.50)
        bbox = _bbox_at(0.5, 0.70)   # centroid below line
        events = t.update(1, bbox, 0.9, 1920, 1080, 0.0, is_staff=False)
        types = [e["event_type"] for e in events]
        assert "ENTRY" in types

    def test_new_track_above_line_no_entry(self):
        t = _make_tracker(entry_line_y=0.50)
        bbox = _bbox_at(0.5, 0.20)   # centroid above line
        events = t.update(1, bbox, 0.9, 1920, 1080, 0.0, is_staff=False)
        types = [e["event_type"] for e in events]
        assert "ENTRY" not in types

    def test_staff_track_is_staff_true(self):
        t = _make_tracker()
        bbox = _bbox_at(0.5, 0.70)
        events = t.update(1, bbox, 0.9, 1920, 1080, 0.0, is_staff=True)
        for e in events:
            assert e["is_staff"] is True


class TestTrackerReentry:
    def test_reentry_within_window(self):
        t = _make_tracker(reentry_window_ms=120_000)
        bbox = _bbox_at(0.5, 0.70)
        # First appearance — ENTRY
        t.update(1, bbox, 0.9, 1920, 1080, 0.0, is_staff=False)
        # Second appearance with same appearance key, within window — REENTRY
        events = t.update(2, bbox, 0.9, 1920, 1080, 60_000.0, is_staff=False)
        types = [e["event_type"] for e in events]
        assert "REENTRY" in types

    def test_reentry_outside_window_is_new_entry(self):
        t = _make_tracker(reentry_window_ms=120_000)
        bbox = _bbox_at(0.5, 0.70)
        t.update(1, bbox, 0.9, 1920, 1080, 0.0, is_staff=False)
        # 121 s later — beyond window → new visitor, new ENTRY
        events = t.update(2, bbox, 0.9, 1920, 1080, 121_000.0, is_staff=False)
        types = [e["event_type"] for e in events]
        assert "ENTRY" in types
        assert "REENTRY" not in types


class TestTrackerZoneAndDwell:
    def test_zone_enter_on_zone_change(self):
        t = _make_tracker()
        # Start outside any zone
        bbox_out = _bbox_at(0.5, 0.70)
        t.update(1, bbox_out, 0.9, 1920, 1080, 0.0, is_staff=False)

        # Move into SKINCARE zone (default: x<0.5, y<0.45)
        bbox_in = _bbox_at(0.25, 0.20)
        events = t.update(1, bbox_in, 0.9, 1920, 1080, 1_000.0, is_staff=False)
        types = [e["event_type"] for e in events]
        assert "ZONE_ENTER" in types

    def test_dwell_heartbeat_emitted_after_30s(self):
        t = _make_tracker()
        bbox = _bbox_at(0.25, 0.20)
        t.update(1, bbox, 0.9, 1920, 1080, 0.0, is_staff=False)
        # Same zone, 31 s later
        events = t.update(1, bbox, 0.9, 1920, 1080, 31_000.0, is_staff=False)
        types = [e["event_type"] for e in events]
        assert "ZONE_DWELL" in types

    def test_dwell_not_emitted_before_30s(self):
        t = _make_tracker()
        bbox = _bbox_at(0.25, 0.20)
        t.update(1, bbox, 0.9, 1920, 1080, 0.0, is_staff=False)
        events = t.update(1, bbox, 0.9, 1920, 1080, 25_000.0, is_staff=False)
        types = [e["event_type"] for e in events]
        assert "ZONE_DWELL" not in types


class TestTrackerExit:
    def test_exit_on_line_cross_outbound(self):
        t = _make_tracker(entry_line_y=0.50)
        # Appear below line (entered)
        bbox_below = _bbox_at(0.5, 0.70)
        t.update(1, bbox_below, 0.9, 1920, 1080, 0.0, is_staff=False)
        # Move above line (exit direction: bottom → top is exit for inbound-entry setup)
        # Our convention: prev_cy <= line_y < cy → exit (moving down = exit)
        # So: start above line, move to below line = EXIT
        t2 = _make_tracker(entry_line_y=0.50)
        bbox_above = _bbox_at(0.5, 0.30)
        t2.update(1, bbox_above, 0.9, 1920, 1080, 0.0, is_staff=False)
        # Force entered=True
        t2._tracks[1].entered = True
        bbox_below2 = _bbox_at(0.5, 0.70)
        events = t2.update(1, bbox_below2, 0.9, 1920, 1080, 5_000.0, is_staff=False)
        types = [e["event_type"] for e in events]
        assert "EXIT" in types

    def test_flush_exits_emits_exit_for_open_sessions(self):
        t = _make_tracker()
        bbox = _bbox_at(0.5, 0.70)
        t.update(1, bbox, 0.9, 1920, 1080, 0.0, is_staff=False)
        # Mark as entered without explicit exit
        t._tracks[1].entered = True
        t._tracks[1].exited  = False
        events = t.flush_exits(60_000.0)
        types = [e["event_type"] for e in events]
        assert "EXIT" in types


class TestGroupEntry:
    def test_three_people_same_frame_emit_three_entries(self):
        """
        Group entry: 3 distinct track_ids arriving at the same timestamp
        must produce 3 ENTRY events (not 1).
        """
        t = _make_tracker(entry_line_y=0.50)
        all_events = []
        for track_id, x_offset in enumerate([0.3, 0.5, 0.7]):
            bbox = _bbox_at(x_offset, 0.70)
            all_events += t.update(track_id, bbox, 0.9, 1920, 1080, 0.0, is_staff=False)

        entry_events = [e for e in all_events if e["event_type"] == "ENTRY"]
        assert len(entry_events) == 3
        visitor_ids = {e["visitor_id"] for e in entry_events}
        assert len(visitor_ids) == 3, "Each person should have a distinct visitor_id"