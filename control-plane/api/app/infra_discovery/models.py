"""Per-connection discovery state: when it last ran, its last error, and who holds it now."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID


class HypervisorDiscoveryState(Base):
    """One row per hypervisor connection that has ever been discovered.

    ``last_discovered_at`` is the last discovery that succeeded (the age of the figures on
    the Infrastructure page); ``last_attempt_at`` the last one started, whatever its
    outcome, which is what the schedule spaces runs by, so a failing vCenter is retried
    once per interval and not on every tick. ``last_error`` is the last attempt's error,
    cleared by a success.

    The lease (``lease_holder``, ``lease_expires_at``) keeps one discovery per connection
    at a time across API processes, the scheduled loop and the Discover button alike: a
    slow vCenter cannot collect a pile of overlapping runs. It is released when the
    discovery ends and expires on its own if the process holding it dies.
    """

    __tablename__ = "hypervisor_discovery_state"

    connection_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("hypervisor_connections.id", ondelete="CASCADE"), primary_key=True
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_holder: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
