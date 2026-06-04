from __future__ import annotations

import aiosqlite

from app.models import EventType, HeatmapResponse, MetricsResponse, ZoneHeatmapEntry

_BILLING_JOIN = EventType.BILLING_QUEUE_JOIN.value
_BILLING_ABANDON = EventType.BILLING_QUEUE_ABANDON.value
_ZONE_DWELL = EventType.ZONE_DWELL.value


def _safe_rate(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


async def get_store_metrics(store_id: str, db: aiosqlite.Connection) -> MetricsResponse:
    cur = await db.execute(
        """
        SELECT COUNT(DISTINCT visitor_id)
        FROM sessions
        WHERE store_id = ? AND date(entry_time) = date('now')
        """,
        (store_id,),
    )
    unique_visitors = (await cur.fetchone())[0]

    cur = await db.execute(
        """
        SELECT
            COALESCE(SUM(converted), 0),
            COUNT(*)
        FROM sessions
        WHERE store_id = ? AND date(entry_time) = date('now')
        """,
        (store_id,),
    )
    converted, total_sessions = await cur.fetchone()
    conversion_rate = _safe_rate(int(converted), int(total_sessions))

    cur = await db.execute(
        """
        SELECT zone_id, AVG(dwell_ms)
        FROM events
        WHERE store_id = ?
          AND event_type = ?
          AND zone_id IS NOT NULL
          AND dwell_ms IS NOT NULL
          AND date(timestamp) = date('now')
          AND is_staff = 0
        GROUP BY zone_id
        """,
        (store_id, _ZONE_DWELL),
    )
    avg_dwell_per_zone = {
        row[0]: float(row[1]) for row in await cur.fetchall()
    }

    cur = await db.execute(
        """
        WITH latest_billing AS (
            SELECT
                visitor_id,
                event_type,
                ROW_NUMBER() OVER (
                    PARTITION BY visitor_id ORDER BY timestamp DESC
                ) AS rn
            FROM events
            WHERE store_id = ?
              AND is_staff = 0
              AND event_type IN (?, ?)
        )
        SELECT COUNT(*)
        FROM latest_billing
        WHERE rn = 1 AND event_type = ?
        """,
        (store_id, _BILLING_JOIN, _BILLING_ABANDON, _BILLING_JOIN),
    )
    queue_depth = (await cur.fetchone())[0]

    cur = await db.execute(
        """
        SELECT
            SUM(CASE WHEN event_type = ? THEN 1 ELSE 0 END),
            SUM(CASE WHEN event_type = ? THEN 1 ELSE 0 END)
        FROM events
        WHERE store_id = ?
          AND date(timestamp) = date('now')
          AND is_staff = 0
        """,
        (_BILLING_ABANDON, _BILLING_JOIN, store_id),
    )
    abandons, joins = await cur.fetchone()
    abandonment_rate = _safe_rate(int(abandons or 0), int(joins or 0))

    return MetricsResponse(
        unique_visitors=int(unique_visitors),
        conversion_rate=conversion_rate,
        avg_dwell_per_zone=avg_dwell_per_zone,
        queue_depth=int(queue_depth),
        abandonment_rate=abandonment_rate,
    )


async def get_store_heatmap(store_id: str, db: aiosqlite.Connection) -> HeatmapResponse:
    cur = await db.execute(
        """
        SELECT
            zone_id,
            COUNT(*) AS visit_frequency,
            AVG(dwell_ms) AS avg_dwell,
            AVG(confidence) AS avg_confidence
        FROM events
        WHERE store_id = ?
          AND date(timestamp) = date('now')
          AND is_staff = 0
          AND zone_id IS NOT NULL
          AND event_type IN (?, ?)
        GROUP BY zone_id
        """,
        (store_id, EventType.ZONE_ENTER.value, _ZONE_DWELL),
    )
    rows = await cur.fetchall()
    if not rows:
        return HeatmapResponse(zones=[])

    max_freq = max(int(r[1]) for r in rows)

    zones: list[ZoneHeatmapEntry] = []
    for zone_id, visit_frequency, avg_dwell, avg_confidence in rows:
        freq = int(visit_frequency)
        normalized = 100.0 if max_freq == 0 else (freq / max_freq) * 100.0
        zones.append(
            ZoneHeatmapEntry(
                zone_id=zone_id,
                visit_frequency=freq,
                avg_dwell_ms=float(avg_dwell or 0.0),
                normalized_score=round(normalized, 2),
                data_confidence=freq >= 5 and float(avg_confidence or 0) >= 0.5,
            )
        )

    return HeatmapResponse(zones=zones)
