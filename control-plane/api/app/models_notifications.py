"""TrueNorth Range - In-app notifications ORM model.

Maps the ``notifications`` table that migration b2c3d4e5f6a1 created long before
anything used it; the columns here are exactly that table's. Rows are written by
``app.notify`` in the same transaction as the change they report, and read by
``routers/notifications.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import GUID


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_read_created", "user_id", "read", text("created_at DESC")),
        Index("ix_notifications_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False, server_default="info", default="info")
    channel: Mapped[str] = mapped_column(String(20), nullable=False, server_default="in_app", default="in_app")
    read: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), default=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # JSON: {"link": "/support/<id>", "kind": "ticket_assigned", ...}
    data_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set by the ORM to the microsecond, so several notifications from one request still
    # sort in the order they happened; the server default is for rows written elsewhere.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("(CURRENT_TIMESTAMP)"),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
