"""Range operations: each requested hypervisor action on a range, durably recorded.

A provision or destroy request used to be a state flip plus a best-effort Celery send:
if the broker was down the range sat in ``provisioning`` with nothing behind it, and two
concurrent requests could both dispatch. An operation row is written in the same
transaction as the range's state change, so an accepted request is never lost; it is
its own outbox (``status = pending`` until a dispatch succeeds), retried by
``range_ops.redispatch_pending``. The range's ``state`` remains the observed state;
the operation is what was asked for, and its outcome is reconciled from that state.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import GUID, TimestampMixin

# pending     accepted and committed; not yet handed to the broker (the outbox)
# dispatched  the broker has the task
# succeeded / failed   the range reached the outcome's state (reconciled from it)
# superseded  a newer request replaced it before it was ever dispatched
OP_STATUSES = ("pending", "dispatched", "succeeded", "failed", "superseded")
IN_FLIGHT = ("pending", "dispatched")


class RangeOperation(TimestampMixin, Base):
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
    # 1, 2, 3 ... per range, in acceptance order. A later generation supersedes an earlier one.
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


class RangeLease(Base):
    """Which worker execution is acting on a range right now (worker/fencing.py).

    A task claims the range's in-progress state *and* this lease. A second copy arriving
    while the first holds it is re-queued (``fencing.defer``) and comes back to find the
    range finished (it then skips) or the lease expired (it then takes over). The holder
    is one execution (a random token, not the Celery task id, which a redelivered copy
    shares). Released when the task ends, however it ends; ``expires_at`` bounds how long a
    worker that died can hold it.
    """

    __tablename__ = "range_leases"

    range_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("ranges.id", ondelete="CASCADE"), primary_key=True)
    holder: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
