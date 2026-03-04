"""Pydantic models for the telemetry ingestion pipeline."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TelemetryEvent(BaseModel):
    """Single telemetry event ingested from a range."""

    event_type: str = Field(..., description="Category of event, e.g. dns_query, http_request, process_exec")
    timestamp: datetime = Field(..., description="Original event timestamp")
    source_ip: str | None = Field(None, description="Source IP address")
    dest_ip: str | None = Field(None, description="Destination IP address")
    range_id: str = Field(..., description="Range that generated this event")
    tenant_id: str = Field(..., description="Owning tenant")
    raw_data: dict[str, Any] = Field(default_factory=dict, description="Original payload")
    tags: list[str] = Field(default_factory=list, description="Enrichment tags, e.g. MITRE technique IDs")

    model_config = {"json_schema_extra": {"examples": [{"event_type": "dns_query", "timestamp": "2026-02-26T12:00:00Z", "source_ip": "10.0.1.5", "dest_ip": "10.0.0.2", "range_id": "rng-001", "tenant_id": "tenant-abc", "raw_data": {"query": "evil.example.com", "qtype": "A"}, "tags": ["T1071.004"]}]}}


class IngestBatch(BaseModel):
    """A batch of telemetry events ready for bulk insertion."""

    events: list[TelemetryEvent] = Field(default_factory=list)
    batch_id: str = Field(..., description="Unique batch identifier")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def size(self) -> int:
        return len(self.events)


class PipelineStats(BaseModel):
    """Runtime statistics for the ingestion pipeline."""

    events_received: int = 0
    events_indexed: int = 0
    events_failed: int = 0
    batches_flushed: int = 0
    last_flush_at: datetime | None = None
    uptime_seconds: float = 0.0
    backpressure_pauses: int = 0