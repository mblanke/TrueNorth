"""The range Greyspace table: which ranges have a simulated internet attached (ADR 0007)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID, TimestampMixin

# configured              attached; config renders; not deployed (yet, or since the last destroy)
# deployed                the worker brought it up with the range (mock backend: recorded only)
# pending_infrastructure  the range's backend has no Greyspace host yet (vSphere gs-core VM: TODO)
# failed                  the worker could not deploy it; ``detail.error`` says why
STATUSES = ("configured", "deployed", "pending_infrastructure", "failed")


class RangeGreyspace(TimestampMixin, Base):
    """One Greyspace block per range. ``block`` is the validated block parameters
    (app/greyspace/schemas.py ``GreyspaceBlock``); ``detail`` is what the worker last
    recorded (worker/greyspace.py), never credentials."""

    __tablename__ = "range_greyspace"
    __table_args__ = (Index("ix_range_greyspace_tenant", "tenant_id"),)

    range_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("ranges.id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    block: Mapped[dict] = mapped_column(JSON, nullable=False)
    corpus_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="configured")
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attached_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
