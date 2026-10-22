"""The latest inventory checks of each range (ADR 0013 §5, range certification S5, #243).

One row per (range, kind), overwritten by the worker each time it checks:

* ``teardown``: the report of the range's last teardown (worker/range_teardown.py): what was
  deleted by recorded id, which recorded ids remain (VMs, disk files, port groups, the
  folder), what carries the range's mark but was not deleted (``unidentified``, which an
  administrator may confirm by managed-object id), and what could not be checked.
* ``drift``: the periodic comparison of the live range with its recorded inventory
  (worker/range_drift.py): VM ids, CPU/RAM, NICs, port group VLANs and snapshots.

This is the current state, for the range page and the confirm flow. The history lives in
the Range Certification Record (``teardown`` and ``drift`` sections, append-only). The
worker writes these rows through its table mirror (worker/tables.py); the API only reads.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID

KINDS = ("teardown", "drift")
# pass / fail / incomplete as the certification sections grade them; error: the check
# itself could not run (vCenter unreachable), which says nothing about the range.
STATUSES = ("pass", "fail", "incomplete", "error")


def _now() -> datetime:
    return datetime.now(UTC)


class RangeInventoryCheck(Base):
    __tablename__ = "range_inventory_checks"
    __table_args__ = (
        UniqueConstraint("range_id", "kind", name="uq_range_inventory_checks_kind"),
        Index("ix_range_inventory_checks_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    range_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("ranges.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    report: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # When the worker last checked, and when what it found last changed.
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now())
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now())
