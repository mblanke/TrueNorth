"""Per-range credentials (ADR 0012 decisions 12 and 13, as amended by ADR 0013 §3).

One row per account a range's machines are given at deploy: the accounts its template
declares (``accounts:``), and the implicit staff logins TrueNorth adds so no machine keeps
its golden template's build password (the built-in Administrator, the Linux build user,
Security Onion's ``soadmin``). The password is sealed at rest with the range-credential key
(``RangeSealedText``, ``tnsec:rc1:``). Every read goes through ``service``, which applies the
role and participation rules; every reveal of a password is audited. The worker reads the
rows (its mirror in ``worker/tables.py``) and unseals them when it builds or rotates.

``RangeCredentialSnapshot`` keeps, per range snapshot, the passwords the accounts had when
it was taken: a restore brings those back inside the guests, and the rotation that follows
logs in with them to set the new ones.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID, TimestampMixin
from .sealed_column import RangeSealedText

DECLARED = "declared"  # the range template's accounts:
IMPLICIT = "implicit"  # a staff login TrueNorth adds (accounts.implicit_staff_logins)


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
    password: Mapped[str] = mapped_column(RangeSealedText, nullable=False)
    # True: TrueNorth made the password up for this range. False: typed into the template,
    # which only a ``scenario: true`` account may be (``scenario``).
    generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    student_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # True once a build or rotation reported it set this account; reset when a build is
    # accepted or the password is rotated. Students see only applied accounts.
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")  # admin | user
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default=DECLARED)  # declared | implicit
    # A typed-in password the template author chose on purpose (the weak password is the
    # lesson): never rotated, recorded as authored by design (ADR 0013 §3, owner 2026-10-21).
    scenario: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # When the password was last replaced after it was first issued (rebuild, reset, admin).
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # While an in-guest rotation of a built range is pending: the password the guest still
    # has, so the worker can log in to change it. Cleared once the worker applied the new one.
    previous_password: Mapped[str | None] = mapped_column(RangeSealedText, nullable=True)


class RangeCredentialSnapshot(Base):
    """The password one account had when one range snapshot was taken (sealed)."""

    __tablename__ = "range_credential_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "credential_id", name="uq_range_credential_snapshots"),
        Index("ix_range_credential_snapshots_snapshot", "snapshot_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("range_snapshots.id", ondelete="CASCADE"), nullable=False)
    credential_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("range_credentials.id", ondelete="CASCADE"), nullable=False)
    password: Mapped[str] = mapped_column(RangeSealedText, nullable=False)
