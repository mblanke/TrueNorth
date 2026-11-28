# Author: Marc-Andre Blanke
# Created: 2026-11-28T02:58:11Z
"""The rate card table. New tables go in their section, never into ``app.models`` (ADR 0003)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID, TimestampMixin

RATE = Numeric(14, 6)


class RateCard(TimestampMixin, Base):
    """What one hour of allocated capacity costs, from ``effective_from`` on.

    ``tenant_id`` None: the platform default, used by every tenant with no card of its own
    in force. A price change is a new row with a later ``effective_from``, never an edit of
    an old one, so history keeps the price it was run at. All rates are per hour.
    """

    __tablename__ = "rate_cards"
    __table_args__ = (Index("ix_rate_cards_tenant_from", "tenant_id", "effective_from"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CAD")
    per_vcpu_hour: Mapped[Decimal] = mapped_column(RATE, nullable=False, default=0)
    per_gb_ram_hour: Mapped[Decimal] = mapped_column(RATE, nullable=False, default=0)
    per_gb_disk_hour: Mapped[Decimal] = mapped_column(RATE, nullable=False, default=0)
    per_vm_hour: Mapped[Decimal] = mapped_column(RATE, nullable=False, default=0)
    notes: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)


class RangeStateEvent(Base):
    """One change of a range's state, appended in the same transaction as the change
    (ADR 0021). Never updated or deleted.

    Written by the worker's ``update_range_state`` (``source`` worker), by the API's flush
    hook (``app/usage/ledger.py``, source api) and by the hourly reconciler when the two
    disagree with the range's real state (source reconcile, ``at`` = when it noticed).
    No foreign keys: the history outlives its range and the range's tenant row.
    """

    __tablename__ = "range_state_events"
    __table_args__ = (
        Index("ix_range_state_events_range_at", "range_id", "at"),
        Index("ix_range_state_events_tenant_at", "tenant_id", "at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    range_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_state: Mapped[str] = mapped_column(String(16), nullable=False)
    # The writer's clock to the microsecond, never the database's now() (a transaction's
    # start on PostgreSQL): two changes in one transaction must not tie.
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), server_default=func.now()
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
