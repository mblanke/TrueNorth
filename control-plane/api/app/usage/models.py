# Author: Marc-Andre Blanke
# Created: 2026-11-28T02:58:11Z
"""The rate card table. New tables go in their section, never into ``app.models`` (ADR 0003)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String
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
