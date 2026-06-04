from __future__ import annotations

import aiosqlite

from app.models import EventType, FunnelResponse

_VISITOR_KEY = "COALESCE(original_visitor_id, visitor_id)"
_ZONE_ENTER = EventType.ZONE_ENTER.value
_BILLING_JOIN = EventType.BILLING_QUEUE_JOIN.value


def _safe_dropoff(from_count: int, to_count: int) -> float:
    if from_count == 0:
        return 0.0
    lost = max(from_count - to_count, 0)
    return (lost / from_count) * 100.0


async def _distinct_visitors_today(
    db: aiosqlite.Connection, store_id: str, extra_filter: str = ""
) -> int:
    query = f"""
        SELECT COUNT(DISTINCT {_VISITOR_KEY})
        FROM sessions
        WHERE store_id = ?
          AND date(entry_time) = date('now')
          {extra_filter}
    """
    cur = await db.execute(query, (store_id,))
    return int((await cur.fetchone())[0])


async def get_store_funnel(store_id: str, db: aiosqlite.Connection) -> FunnelResponse:
    entry_count = await _distinct_visitors_today(db, store_id)

    zone_visit_count = await _distinct_visitors_with_event(
        db, store_id, _ZONE_ENTER
    )
    billing_queue_count = await _distinct_visitors_with_event(
        db, store_id, _BILLING_JOIN
    )

    cur = await db.execute(
        f"""
        SELECT COUNT(DISTINCT {_VISITOR_KEY})
        FROM sessions
        WHERE store_id = ?
          AND date(entry_time) = date('now')
          AND converted = 1
        """,
        (store_id,),
    )
    purchase_count = int((await cur.fetchone())[0])

    dropoff_percentages = {
        "entry_to_zone": _safe_dropoff(entry_count, zone_visit_count),
        "zone_to_billing": _safe_dropoff(zone_visit_count, billing_queue_count),
        "billing_to_purchase": _safe_dropoff(billing_queue_count, purchase_count),
    }

    return FunnelResponse(
        entry_count=entry_count,
        zone_visit_count=zone_visit_count,
        billing_queue_count=billing_queue_count,
        purchase_count=purchase_count,
        dropoff_percentages=dropoff_percentages,
    )


async def _distinct_visitors_with_event(
    db: aiosqlite.Connection, store_id: str, event_type: str
) -> int:
    cur = await db.execute(
        f"""
        SELECT COUNT(DISTINCT {_VISITOR_KEY})
        FROM sessions s
        WHERE s.store_id = ?
          AND date(s.entry_time) = date('now')
          AND EXISTS (
            SELECT 1
            FROM events e
            WHERE e.store_id = s.store_id
              AND e.visitor_id = s.visitor_id
              AND e.event_type = ?
              AND date(e.timestamp) = date('now')
              AND e.is_staff = 0
          )
        """,
        (store_id, event_type),
    )
    return int((await cur.fetchone())[0])
