"""The maintenance window table (ADR 0013 §7, range certification S7).

New tables go in their section, never into the shared ``app.models`` (ADR 0003).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID, TimestampMixin

# What a window covers: every range (of its tenant, or of every tenant when it is
# platform-wide), the ranges listed in ``range_ids``, or the ranges whose range template
# declares one of ``tags`` in its top-level ``tags:`` list.
SCOPES = ("all", "ranges", "tags")


class MaintenanceWindow(TimestampMixin, Base):
    """A period in which provision, reset/restore and normal destroy are refused.

    ``tenant_id`` None: platform-wide (created by a platform administrator; it applies to
    every tenant's ranges). Times are stored in UTC; the window is ``[starts_at,
    ends_at)``. Automated cleanup and emergency teardown are never refused (``service``).
    """

    __tablename__ = "maintenance_windows"
    __table_args__ = (
        Index("ix_maintenance_windows_tenant", "tenant_id"),
        Index("ix_maintenance_windows_ends_at", "ends_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="all")
    range_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
