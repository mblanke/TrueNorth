"""Per-range credentials (ADR 0012, decisions 12 and 13).

One row per account a range's template declares (``accounts:``), with the password the
range's VMs are given at deploy. The password is sealed at rest (``SealedText``, the
same box as hypervisor passwords); every read goes through ``service.visible_accounts``,
which applies the role and participation rules. The worker reads the rows (its mirror in
``worker/tables.py``) and unseals them when it builds the range.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID, TimestampMixin
from ..sealed_column import SealedText


class RangeCredential(TimestampMixin, Base):
    """An account on one machine of one range."""

    __tablename__ = "range_credentials"
    __table_args__ = (
        UniqueConstraint("range_id", "machine", "username", name="uq_range_credentials_account"),
        Index("ix_range_credentials_range", "range_id"),
        Index("ix_range_credentials_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    range_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("ranges.id", ondelete="CASCADE"), nullable=False)
    # The template node's id (``nodes[].id``) or asset role the account belongs to.
    machine: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    password: Mapped[str] = mapped_column(SealedText, nullable=False)
    # True: TrueNorth made the password up for this range (``generate: true``).
    generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    student_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # True once a build reported it set this account (worker/range_accounts.py); reset when a
    # build is accepted. Students see only applied accounts; staff see the rest flagged.
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")  # admin | user
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
