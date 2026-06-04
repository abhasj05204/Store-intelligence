from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite

from app.models import HealthResponse


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


async def get_health(db: aiosqlite.Connection) -> HealthResponse:
    cur = await db.execute(
        """
        SELECT store_id, MAX(timestamp) AS last_ts
        FROM events
        GROUP BY store_id
        """
    )
    rows = await cur.fetchall()

    last_event_per_store: dict[str, datetime] = {
        row[0]: _parse_ts(row[1]) for row in rows
    }

    stale_feeds: list[str] = []
    cur = await db.execute(
        """
        SELECT store_id
        FROM events
        GROUP BY store_id
        HAVING MAX(timestamp) < datetime('now', '-10 minutes')
        """
    )
    stale_rows = await cur.fetchall()
    stale_feeds = [row[0] for row in stale_rows]

    status = "degraded" if stale_feeds else "ok"

    return HealthResponse(
        status=status,
        last_event_per_store=last_event_per_store,
        stale_feeds=stale_feeds,
    )
