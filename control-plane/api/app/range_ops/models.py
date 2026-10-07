"""The range operation table (CR1-06 in docs/review/codereview1.md)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID, TimestampMixin

# pending     accepted and committed; not yet handed to the broker (the outbox)
# dispatched  the broker has the task
# succeeded / failed   the range reached the outcome's state (reconciled from it)
# superseded  a destroy replaced it while it was still in flight
OP_STATUSES = ("pending", "dispatched", "succeeded", "failed", "superseded")
IN_FLIGHT = ("pending", "dispatched")


class RangeOperation(TimestampMixin, Base):
    """What was asked of a range, written in the same transaction as the range's state
    change. The range's ``state`` stays what the worker observed; an operation's outcome
    is reconciled from it, never assumed. The row is its own outbox: ``pending`` until a
    send succeeds (app/range_ops/service.py)."""

    __tablename__ = "range_operations"
    __table_args__ = (
        UniqueConstraint("range_id", "generation", name="uq_range_operations_generation"),
        UniqueConstraint("range_id", "idempotency_key", name="uq_range_operations_idempotency"),
        Index("ix_range_operations_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    range_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("ranges.id", ondelete="CASCADE"), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    # 1, 2, 3 ... per range, in acceptance order.
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # {"code": "...", "message": "..."}; never credentials
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
