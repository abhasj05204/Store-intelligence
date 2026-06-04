"""
Re-ID and tracking logic for the Store Intelligence pipeline.

Maintains track state across frames, assigns stable visitor_id tokens,
detects re-entries, and classifies zone from bounding-box position.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional


# ── Zone map: camera-relative bounding boxes (x1, y1, x2, y2 as fractions) ──
# These are illustrative defaults; real values come from store_layout.json
DEFAULT_ZONES: dict[str, tuple[float, float, float, float]] = {
    "ENTRY":     (0.0, 0.45, 1.0, 0.65),
    "SKINCARE":  (0.0, 0.0,  0.5, 0.45),
    "HAIRCARE":  (0.5, 0.0,  1.0, 0.45),
    "BILLING":   (0.0, 0.65, 1.0, 1.0),
}


@dataclass
class TrackState:
    visitor_id: str
    last_cx: float          # centroid x (fraction of frame width)
    last_cy: float          # centroid y (fraction of frame height)
    entered: bool = False
    zone: Optional[str] = None
    zone_enter_ms: float = 0.0
    last_dwell_ms: float = 0.0
    session_seq: int = 1
    is_staff: bool = False
    exited: bool = False


class Tracker:
    """
    Lightweight tracker wrapper that maintains visitor state across frames.

    Re-ID strategy
    ──────────────
    When a new track_id appears we compute an appearance key from:
      - aspect ratio of the bounding box (rounded to 1 dp)
      - coarse x-sector (frame divided into 10 columns)
    If this key was seen within the re-entry window (default 120 s),
    we issue a REENTRY event rather than a new ENTRY.

    This is intentionally simple — it degrades gracefully under occlusion
    (low confidence is flagged, not dropped) and avoids false re-ID merges
    across long time gaps.
    """

    def __init__(
        self,
        zones: dict[str, tuple[float, float, float, float]] | None = None,
        entry_line_y: float = 0.55,
        reentry_window_ms: float = 120_000,
    ):
        self.zones = zones or DEFAULT_ZONES
        self.entry_line_y = entry_line_y
        self.reentry_window_ms = reentry_window_ms

        self._tracks: dict[int, TrackState] = {}
        # appearance_key → (visitor_id, last_seen_ms)
        self._appearance_cache: dict[str, tuple[str, float]] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def update(
        self,
        track_id: int,
        bbox: tuple[float, float, float, float],  # x1 y1 x2 y2 pixels
        conf: float,
        frame_w: int,
        frame_h: int,
        ts_offset_ms: float,
        is_staff: bool,
    ) -> list[dict]:
        """
        Process one tracker output row.  Returns a list of event dicts
        (may be empty, or contain ENTRY/REENTRY/EXIT/ZONE_ENTER/ZONE_EXIT/
        ZONE_DWELL events).
        """
        x1, y1, x2, y2 = bbox
        cx_frac = ((x1 + x2) / 2) / frame_w
        cy_frac = ((y1 + y2) / 2) / frame_h
        aspect   = round((x2 - x1) / max(y2 - y1, 1), 1)
        x_sector = int(cx_frac * 10)
        appearance_key = f"{aspect}_{x_sector}"

        events: list[dict] = []

        if track_id not in self._tracks:
            events += self._handle_new_track(
                track_id, appearance_key, cx_frac, cy_frac,
                conf, ts_offset_ms, is_staff,
            )
        else:
            events += self._handle_existing_track(
                track_id, cx_frac, cy_frac, conf,
                ts_offset_ms, is_staff,
            )

        return events

    def flush_exits(self, ts_offset_ms: float) -> list[dict]:
        """
        Call at end-of-clip to emit EXIT for any visitor that never crossed
        the exit line (e.g. clip ended while they were still inside).
        """
        events = []
        for track_id, state in self._tracks.items():
            if state.entered and not state.exited:
                events.append(self._make_exit(state, ts_offset_ms, conf=0.5))
                state.exited = True
        return events

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _handle_new_track(
        self,
        track_id: int,
        appearance_key: str,
        cx_frac: float,
        cy_frac: float,
        conf: float,
        ts_offset_ms: float,
        is_staff: bool,
    ) -> list[dict]:
        events = []
        cached = self._appearance_cache.get(appearance_key)
        is_reentry = (
            cached is not None
            and (ts_offset_ms - cached[1]) < self.reentry_window_ms
        )

        if is_reentry:
            visitor_id = cached[0]
            event_type = "REENTRY"
        else:
            visitor_id = f"VIS_{uuid.uuid4().hex[:6]}"
            event_type = "ENTRY" if cy_frac > self.entry_line_y else None

        self._appearance_cache[appearance_key] = (visitor_id, ts_offset_ms)

        state = TrackState(
            visitor_id=visitor_id,
            last_cx=cx_frac,
            last_cy=cy_frac,
            entered=event_type in ("ENTRY", "REENTRY"),
            zone_enter_ms=ts_offset_ms,
            last_dwell_ms=ts_offset_ms,
            session_seq=1,
            is_staff=is_staff,
        )
        self._tracks[track_id] = state

        if event_type:
            events.append(
                self._make_event(
                    state, event_type, ts_offset_ms, conf,
                    zone_id=None, dwell_ms=0,
                )
            )
            state.session_seq += 1

        return events

    def _handle_existing_track(
        self,
        track_id: int,
        cx_frac: float,
        cy_frac: float,
        conf: float,
        ts_offset_ms: float,
        is_staff: bool,
    ) -> list[dict]:
        events = []
        state = self._tracks[track_id]
        prev_cy = state.last_cy

        # Exit detection: crossed entry line outbound (top → bottom direction is entry)
        if prev_cy <= self.entry_line_y < cy_frac and state.entered and not state.exited:
            events.append(self._make_exit(state, ts_offset_ms, conf))
            state.entered = False
            state.exited = True

        # Zone assignment
        current_zone = self._zone_for(cx_frac, cy_frac)
        if current_zone != state.zone:
            if state.zone is not None:
                events.append(
                    self._make_event(
                        state, "ZONE_EXIT", ts_offset_ms, conf,
                        zone_id=state.zone,
                        dwell_ms=int(ts_offset_ms - state.zone_enter_ms),
                    )
                )
                state.session_seq += 1
            if current_zone is not None:
                events.append(
                    self._make_event(
                        state, "ZONE_ENTER", ts_offset_ms, conf,
                        zone_id=current_zone, dwell_ms=0,
                    )
                )
                state.session_seq += 1
            state.zone = current_zone
            state.zone_enter_ms = ts_offset_ms

        # Dwell heartbeat every 30 s
        if (
            state.zone is not None
            and (ts_offset_ms - state.last_dwell_ms) >= 30_000
        ):
            events.append(
                self._make_event(
                    state, "ZONE_DWELL", ts_offset_ms, conf,
                    zone_id=state.zone,
                    dwell_ms=int(ts_offset_ms - state.zone_enter_ms),
                )
            )
            state.last_dwell_ms = ts_offset_ms
            state.session_seq += 1

        state.last_cx = cx_frac
        state.last_cy = cy_frac
        state.is_staff = is_staff
        return events

    def _zone_for(self, cx: float, cy: float) -> str | None:
        """Return the first zone whose bounding box contains (cx, cy)."""
        for zone_name, (zx1, zy1, zx2, zy2) in self.zones.items():
            if zx1 <= cx <= zx2 and zy1 <= cy <= zy2:
                return zone_name
        return None

    def _make_event(
        self,
        state: TrackState,
        event_type: str,
        ts_offset_ms: float,
        conf: float,
        *,
        zone_id: str | None,
        dwell_ms: int,
        queue_depth: int | None = None,
    ) -> dict:
        return {
            "_ts_offset_ms": ts_offset_ms,   # pipeline uses this; emit.py strips it
            "event_type": event_type,
            "visitor_id": state.visitor_id,
            "zone_id": zone_id,
            "dwell_ms": dwell_ms,
            "is_staff": state.is_staff,
            "confidence": round(conf, 4),
            "metadata": {
                "queue_depth": queue_depth,
                "sku_zone": zone_id,
                "session_seq": state.session_seq,
            },
        }

    def _make_exit(self, state: TrackState, ts_offset_ms: float, conf: float) -> dict:
        return self._make_event(
            state, "EXIT", ts_offset_ms, conf,
            zone_id=None, dwell_ms=0,
        )