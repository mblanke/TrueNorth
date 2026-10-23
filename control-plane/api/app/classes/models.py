"""The Class record, its staff and its roll call (ADR 0014 §1, §2, §5, §8).

New tables go in their section, never into the shared ``app.models`` (ADR 0003).

- ``classes``: one running of a course for a named group ("C201-26-03"), in one tenant,
  with one lead instructor and start and end dates.
- ``class_staff``: its assistant instructors and observers (the lead is on the class row).
- ``class_roll_call``: the Students on it, by email. Being on it is approval to join
  (``onboarding.admit_from_roll_call``).
- ``class_sessions``: its sessions, each one a scheduler booking (``ScheduledEvent``) that
  needs an approver (ADR 0014 §14; ``scheduler/approval.py``).

Nothing here is ever deleted by the API (ADR 0010): a class is cancelled, a roll-call
entry is removed (``status = removed``), so who was invited and who signed in stays on
record.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID, TimestampMixin


class ClassStatus(str, enum.Enum):
    open = "open"
    cancelled = "cancelled"


class StaffRole(str, enum.Enum):
    assistant = "assistant"
    observer = "observer"


class RollCallStatus(str, enum.Enum):
    invited = "invited"  # on the roll call; no TrueNorth sign-in yet
    signed_in = "signed_in"  # linked to an account that has signed in
    ready = "ready"  # set by later slices (acknowledgement signed, range up)
    # Added after the class's sessions were approved, and they had no room for one more
    # Student (ADR 0014 §17): waits for an approver. Not on the roll call until approved:
    # not linked, enrolled, emailed, invited or admitted.
    pending = "pending"
    removed = "removed"  # taken off by the lead or an admin; kept for the record


class Class(TimestampMixin, Base):
    __tablename__ = "classes"
    __table_args__ = (Index("ix_classes_tenant_start", "tenant_id", "start_date"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    course_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("courses.id"), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    lead_instructor_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ClassStatus.open.value)
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)


class ClassStaff(TimestampMixin, Base):
    __tablename__ = "class_staff"
    __table_args__ = (
        UniqueConstraint("class_id", "user_id", name="uq_class_staff_user"),
        Index("ix_class_staff_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("classes.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)


class RollCallEntry(TimestampMixin, Base):
    """One Student on a class's roll call. ``email`` is stored lower-cased and trimmed:
    matching is case-insensitive. ``tenant_id`` is the class's, kept here so a match across
    every class is one indexed query."""

    __tablename__ = "class_roll_call"
    __table_args__ = (
        UniqueConstraint("class_id", "email", name="uq_class_roll_call_email"),
        Index("ix_class_roll_call_email", "email"),
        Index("ix_class_roll_call_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("classes.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RollCallStatus.invited.value)
    added_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    onboarding_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signed_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Why an addition waits for an approver (status pending): what did not fit.
    pending_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class RollCallSettings(TimestampMixin, Base):
    """A tenant's roll-call settings. ``email_domains``: the domains a roll call may add
    (lower-case, no @). No row: the domains of the tenant's active accounts' emails
    (``service.allowed_domains``)."""

    __tablename__ = "class_roll_call_settings"

    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), primary_key=True)
    email_domains: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)


class ClassSession(TimestampMixin, Base):
    """One session of a class: the class's link to its scheduler booking (ADR 0014 §8, §14).

    A link table rather than a ``class_id`` on ``scheduled_events``: the scheduler stays
    ignorant of classes (ADR 0004 §3: it refers to other sections by id and reads them
    through their services), a booking is at most one class's session (``event_id`` is
    unique), and what belongs to the session as a class matter (individual or collective
    mode and teams, #282; attendance) gets a row to hang on. The booking's approval
    itself is the scheduler's (``scheduler_booking_approvals``)."""

    __tablename__ = "class_sessions"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_class_sessions_event"),
        Index("ix_class_sessions_class", "class_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("classes.id"), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("scheduled_events.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
