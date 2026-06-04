from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite

from app.metrics import _safe_rate, get_store_metrics
from app.models import AnomalyResponse, AnomalySeverity, EventType

_ZONE_ENTER = EventType.ZONE_ENTER.value


async def _conversion_rate_for_day(
    db: aiosqlite.Connection, store_id: str, day_offset: int
) -> float:
    cur = await db.execute(
        """
        SELECT COALESCE(SUM(converted), 0), COUNT(*)
        FROM sessions
        WHERE store_id = ?
          AND date(entry_time) = date('now', ?)
        """,
        (store_id, f"-{day_offset} days"),
    )
    converted, total = await cur.fetchone()
    return _safe_rate(int(converted), int(total))


async def get_anomalies(
    store_id: str, db: aiosqlite.Connection
) -> list[AnomalyResponse]:
    now = datetime.now(timezone.utc)
    anomalies: list[AnomalyResponse] = []

    metrics = await get_store_metrics(store_id, db)

    if metrics.queue_depth > 10:
        anomalies.append(
            _anomaly(
                store_id=store_id,
                anomaly_type="BILLING_QUEUE_SPIKE",
                severity=AnomalySeverity.CRITICAL,
                description=(
                    f"Billing queue depth is {metrics.queue_depth} (critical threshold: 10)"
                ),
                suggested_action="Deploy additional staff to billing counter",
                detected_at=now,
            )
        )
    elif metrics.queue_depth > 5:
        anomalies.append(
            _anomaly(
                store_id=store_id,
                anomaly_type="BILLING_QUEUE_SPIKE",
                severity=AnomalySeverity.WARN,
                description=(
                    f"Billing queue depth is {metrics.queue_depth} (warning threshold: 5)"
                ),
                suggested_action="Deploy additional staff to billing counter",
                detected_at=now,
            )
        )

    today_rate = metrics.conversion_rate
    historical_rates = [
        await _conversion_rate_for_day(db, store_id, offset)
        for offset in range(1, 8)
    ]
    avg_7d = sum(historical_rates) / len(historical_rates)
    if avg_7d > 0 and today_rate < 0.5 * avg_7d:
        anomalies.append(
            _anomaly(
                store_id=store_id,
                anomaly_type="CONVERSION_DROP",
                severity=AnomalySeverity.WARN,
                description=(
                    f"Today's conversion rate ({today_rate:.2%}) is below 50% of the "
                    f"7-day average ({avg_7d:.2%})"
                ),
                suggested_action="Review pricing and promotions",
                detected_at=now,
            )
        )

    cur = await db.execute(
        """
        SELECT COUNT(*)
        FROM events
        WHERE store_id = ?
          AND event_type = ?
          AND timestamp >= datetime('now', '-30 minutes')
        """,
        (store_id, _ZONE_ENTER),
    )
    recent_zone_enters = (await cur.fetchone())[0]
    if recent_zone_enters == 0:
        anomalies.append(
            _anomaly(
                store_id=store_id,
                anomaly_type="DEAD_ZONE",
                severity=AnomalySeverity.INFO,
                description="No ZONE_ENTER events for any zone in the last 30 minutes",
                suggested_action="Check camera feed and zone signage",
                detected_at=now,
            )
        )

    return anomalies


def _anomaly(
    *,
    store_id: str,
    anomaly_type: str,
    severity: AnomalySeverity,
    description: str,
    suggested_action: str,
    detected_at: datetime,
) -> AnomalyResponse:
    return AnomalyResponse(
        anomaly_id=f"{store_id}_{anomaly_type}_{uuid4().hex[:8]}",
        type=anomaly_type,
        severity=severity,
        description=description,
        suggested_action=suggested_action,
        detected_at=detected_at,
    )
