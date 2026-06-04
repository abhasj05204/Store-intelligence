from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import aiosqlite

from app.models import EventType, StoreEvent

MAX_INGEST_BATCH = 500


def _default_db_path() -> str:
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
            if database_url.startswith(prefix):
                return database_url[len(prefix) :]
    return os.environ.get("STORE_INTEL_DB", "store_intelligence.db")

_BILLING_EVENT_TYPES = (
    EventType.BILLING_QUEUE_JOIN.value,
    EventType.BILLING_QUEUE_ABANDON.value,
)

_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    visitor_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    zone_id TEXT,
    dwell_ms INTEGER,
    is_staff INTEGER NOT NULL,
    confidence REAL NOT NULL,
    metadata_json TEXT NOT NULL
);
"""

_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    visitor_id TEXT NOT NULL,
    store_id TEXT NOT NULL,
    original_visitor_id TEXT,
    entry_time TEXT NOT NULL,
    exit_time TEXT,
    converted INTEGER NOT NULL DEFAULT 0
);
"""

_CREATE_POS = """
CREATE TABLE IF NOT EXISTS pos_transactions (
    transaction_id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    basket_value_inr REAL NOT NULL
);
"""

_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_events_store_visitor_ts
    ON events (store_id, visitor_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_sessions_store_visitor_open
    ON sessions (store_id, visitor_id, exit_time);
CREATE INDEX IF NOT EXISTS idx_pos_store_ts
    ON pos_transactions (store_id, timestamp);
"""


def _utc_iso(dt: datetime) -> str:
    """UTC timestamp string compatible with SQLite datetime()."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _event_row(event: StoreEvent) -> tuple[Any, ...]:
    return (
        str(event.event_id),
        event.store_id,
        event.camera_id,
        event.visitor_id,
        event.event_type.value,
        _utc_iso(event.timestamp),
        event.zone_id,
        event.dwell_ms,
        int(event.is_staff),
        event.confidence,
        json.dumps(event.metadata.model_dump()),
    )


def _is_billing_presence_sql() -> str:
    return """(
        event_type IN (?, ?)
        OR UPPER(COALESCE(zone_id, '')) LIKE '%BILLING%'
    )"""


async def init_db(db_path: str | None = None) -> None:
    path = db_path or _default_db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.executescript(
            _CREATE_EVENTS + _CREATE_SESSIONS + _CREATE_POS + _CREATE_INDEXES
        )
        await db.commit()


@asynccontextmanager
async def get_db(db_path: str | None = None) -> AsyncIterator[aiosqlite.Connection]:
    """Yield an aiosqlite connection; caller commits or rolls back."""
    path = db_path or _default_db_path()
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    try:
        await db.execute("PRAGMA foreign_keys = ON;")
        yield db
    finally:
        await db.close()


async def _existing_event_ids(
    db: aiosqlite.Connection, event_ids: list[str]
) -> set[str]:
    if not event_ids:
        return set()
    placeholders = ",".join("?" * len(event_ids))
    cursor = await db.execute(
        f"SELECT event_id FROM events WHERE event_id IN ({placeholders})",
        event_ids,
    )
    rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def _insert_event(db: aiosqlite.Connection, event: StoreEvent) -> None:
    await db.execute(
        """
        INSERT INTO events (
            event_id, store_id, camera_id, visitor_id, event_type,
            timestamp, zone_id, dwell_ms, is_staff, confidence, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        _event_row(event),
    )


async def _open_session(
    db: aiosqlite.Connection,
    *,
    visitor_id: str,
    store_id: str,
    entry_time: datetime,
    original_visitor_id: str | None = None,
) -> None:
    await db.execute(
        """
        INSERT INTO sessions (
            visitor_id, store_id, original_visitor_id, entry_time, exit_time, converted
        ) VALUES (?, ?, ?, ?, NULL, 0)
        """,
        (visitor_id, store_id, original_visitor_id, _utc_iso(entry_time)),
    )


async def _close_open_session(
    db: aiosqlite.Connection,
    *,
    visitor_id: str,
    store_id: str,
    exit_time: datetime,
) -> None:
    await db.execute(
        """
        UPDATE sessions
        SET exit_time = ?
        WHERE session_id = (
            SELECT session_id FROM sessions
            WHERE visitor_id = ? AND store_id = ? AND exit_time IS NULL
            ORDER BY entry_time DESC
            LIMIT 1
        )
        """,
        (_utc_iso(exit_time), visitor_id, store_id),
    )


async def _apply_session_side_effects(db: aiosqlite.Connection, event: StoreEvent) -> None:
    if event.is_staff:
        return

    if event.event_type == EventType.ENTRY:
        await _open_session(
            db,
            visitor_id=event.visitor_id,
            store_id=event.store_id,
            entry_time=event.timestamp,
        )
    elif event.event_type == EventType.REENTRY:
        await _open_session(
            db,
            visitor_id=event.visitor_id,
            store_id=event.store_id,
            entry_time=event.timestamp,
            original_visitor_id=event.visitor_id,
        )
    elif event.event_type == EventType.EXIT:
        await _close_open_session(
            db,
            visitor_id=event.visitor_id,
            store_id=event.store_id,
            exit_time=event.timestamp,
        )


async def _mark_converted_from_pos(
    db: aiosqlite.Connection, *, store_id: str, visitor_id: str
) -> None:
    billing_filter = _is_billing_presence_sql()
    await db.execute(
        f"""
        UPDATE sessions
        SET converted = 1
        WHERE store_id = ? AND visitor_id = ? AND converted = 0
          AND EXISTS (
            SELECT 1
            FROM pos_transactions pt
            WHERE pt.store_id = sessions.store_id
              AND EXISTS (
                SELECT 1 FROM events e
                WHERE e.store_id = pt.store_id
                  AND e.visitor_id = ?
                  AND e.timestamp > datetime(pt.timestamp, '-5 minutes')
                  AND e.timestamp <= pt.timestamp
                  AND {billing_filter}
              )
          )
        """,
        (
            store_id,
            visitor_id,
            visitor_id,
            _BILLING_EVENT_TYPES[0],
            _BILLING_EVENT_TYPES[1],
        ),
    )


async def _pos_correlation_for_pairs(
    db: aiosqlite.Connection, pairs: set[tuple[str, str]]
) -> None:
    for store_id, visitor_id in pairs:
        await _mark_converted_from_pos(db, store_id=store_id, visitor_id=visitor_id)


async def ingest_events(
    events: list[StoreEvent],
    *,
    db_path: str | None = None,
) -> dict[str, Any]:
    """
    Ingest up to 500 store events. Duplicate event_id values are skipped without error.
    """
    if len(events) > MAX_INGEST_BATCH:
        return {
            "accepted": 0,
            "duplicates": 0,
            "errors": [
                {
                    "error": f"batch size {len(events)} exceeds maximum of {MAX_INGEST_BATCH}",
                }
            ],
        }

    accepted = 0
    duplicates = 0
    errors: list[dict[str, Any]] = []
    correlation_pairs: set[tuple[str, str]] = set()

    async with get_db(db_path) as db:
        event_ids = [str(e.event_id) for e in events]
        existing = await _existing_event_ids(db, event_ids)

        for event in events:
            eid = str(event.event_id)
            if eid in existing:
                duplicates += 1
                continue

            try:
                await _insert_event(db, event)
                await _apply_session_side_effects(db, event)
                await db.commit()
                existing.add(eid)
                accepted += 1
                correlation_pairs.add((event.store_id, event.visitor_id))
            except Exception as exc:
                await db.rollback()
                errors.append({"event_id": eid, "error": str(exc)})

        if correlation_pairs:
            try:
                await _pos_correlation_for_pairs(db, correlation_pairs)
                await db.commit()
            except Exception as exc:
                await db.rollback()
                errors.append({"error": f"pos correlation failed: {exc}"})

    return {"accepted": accepted, "duplicates": duplicates, "errors": errors}
