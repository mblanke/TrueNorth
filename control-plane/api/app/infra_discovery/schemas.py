"""Response shapes for ``GET /hypervisors/discovery`` (kept out of app/schemas.py, ADR 0003)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, field_validator


class ConnectionDiscoveryOut(BaseModel):
    connection_id: uuid.UUID
    #: Last discovery that succeeded: how old this connection's figures are. None: never.
    last_discovered_at: datetime | None = None
    #: Last discovery started, whatever came of it.
    last_attempt_at: datetime | None = None
    #: The last attempt failed. ``last_error`` says why, for callers who may manage connections.
    failing: bool = False
    last_error: str | None = None
    #: A discovery of this connection is running now.
    running: bool = False

    @field_validator("last_discovered_at", "last_attempt_at")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        # Stored in UTC; SQLite returns them naive. Always send an offset, so a browser
        # does not read them as its local time.
        return v.replace(tzinfo=UTC) if v is not None and v.tzinfo is None else v


class InfraDiscoveryOut(BaseModel):
    #: INFRA_DISCOVERY_SECONDS; 0 when scheduled discovery is off.
    interval_seconds: int
    connections: list[ConnectionDiscoveryOut]
