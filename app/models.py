from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pydantic import BaseModel, ConfigDict, Field, UUID4


class EventType(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class AnomalySeverity(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class EventMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queue_depth: int | None = None
    sku_zone: str | None = None
    session_seq: int | None = None


class StoreEvent(BaseModel):
    """Core retail analytics event emitted by the vision pipeline."""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID4
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: EventType
    timestamp: datetime
    zone_id: str | None = None
    dwell_ms: int | None = Field(default=None, ge=0)
    is_staff: bool
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: EventMetadata


class MetricsResponse(BaseModel):
    unique_visitors: int = Field(ge=0)
    conversion_rate: float = Field(ge=0.0, le=1.0)
    avg_dwell_per_zone: dict[str, float]
    queue_depth: int = Field(ge=0)
    abandonment_rate: float = Field(ge=0.0, le=1.0)


class FunnelResponse(BaseModel):
    entry_count: int = Field(ge=0)
    zone_visit_count: int = Field(ge=0)
    billing_queue_count: int = Field(ge=0)
    purchase_count: int = Field(ge=0)
    dropoff_percentages: dict[str, float]


class ZoneHeatmapEntry(BaseModel):
    zone_id: str
    visit_frequency: int = Field(ge=0)
    avg_dwell_ms: float = Field(ge=0.0)
    normalized_score: float = Field(ge=0.0, le=100.0)
    data_confidence: bool


class HeatmapResponse(BaseModel):
    zones: list[ZoneHeatmapEntry]


class AnomalyResponse(BaseModel):
    anomaly_id: str
    type: str
    severity: AnomalySeverity
    description: str
    suggested_action: str
    detected_at: datetime


class HealthResponse(BaseModel):
    status: str
    last_event_per_store: dict[str, datetime]
    stale_feeds: list[str]