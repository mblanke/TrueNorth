"""Per-range credentials (ADR 0012 decisions 12 and 13, as amended by ADR 0013 §3).

One row per account a range's machines are given at deploy: the accounts its template
declares (``accounts:``), and the implicit staff logins TrueNorth adds so no machine keeps
its golden template's build password (the built-in Administrator, the Linux build user,
Security Onion's ``soadmin``). Every read goes through ``service``, which applies the role
and participation rules; every reveal of a password is audited. The worker reads the rows
(its mirror in ``worker/tables.py``) and opens them when it builds or rotates.

Passwords are sealed at rest with the stored-credential key (``seal.seal_row``,
``tnsec:rk1:``), bound to the row's tenant, range and id, so a stored value opens nowhere
else: not in another row, not as a template's typed-in password. Python reads and writes
the plain value through ``password`` / ``previous_password``; the columns hold the seals.
A row's ``id``, ``tenant_id`` and ``range_id`` are set before its password (the binding).

``RangeCredentialSnapshot`` keeps, per range snapshot, the password each account had in the
guests when it was taken: a restore brings those back, and the rotation after it logs in
with them (or sets the account's current password back).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID, TimestampMixin
from . import seal as rc_seal

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
    sealed_password: Mapped[str] = mapped_column("password", Text, nullable=False)
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
    # While a change in the guests is pending (a rotation, or a restore putting the current
    # password back): the password the guests have now. The worker logs in with it, and puts
    # it back as ``password`` when the change fails. Cleared once the change is verified.
    sealed_previous: Mapped[str | None] = mapped_column("previous_password", Text, nullable=True)

    def _aad(self) -> str:
        if self.id is None:
            self.id = uuid.uuid4()
        if self.tenant_id is None or self.range_id is None:
            raise ValueError("a range credential needs its tenant and range before its password")
        return rc_seal.row_aad(self.tenant_id, self.range_id, self.id)

    @property
    def password(self) -> str:
        return rc_seal.unseal_row(self.sealed_password, self._aad()) or ""

    @password.setter
    def password(self, plain: str | None) -> None:
        self.sealed_password = rc_seal.seal_row(plain or "", self._aad())

    @property
    def previous_password(self) -> str | None:
        return rc_seal.unseal_row(self.sealed_previous, self._aad()) if self.sealed_previous else None

    @previous_password.setter
    def previous_password(self, plain: str | None) -> None:
        self.sealed_previous = None if plain is None else rc_seal.seal_row(plain, self._aad())

    @property
    def pending(self) -> bool:
        """A change in the guests is in flight: the guests still have ``previous_password``."""
        return self.sealed_previous is not None


class RangeCredentialSnapshot(Base):
    """The password one account had in the guests when one range snapshot was taken (sealed,
    bound to the tenant, snapshot and account)."""

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
    sealed_password: Mapped[str] = mapped_column("password", Text, nullable=False)

    def _aad(self) -> str:
        if None in (self.tenant_id, self.snapshot_id, self.credential_id):
            raise ValueError("a snapshot credential needs its tenant, snapshot and account before its password")
        return rc_seal.snapshot_aad(self.tenant_id, self.snapshot_id, self.credential_id)

    @property
    def password(self) -> str:
        return rc_seal.unseal_row(self.sealed_password, self._aad()) or ""

    @password.setter
    def password(self, plain: str) -> None:
        self.sealed_password = rc_seal.seal_row(plain, self._aad())
