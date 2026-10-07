"""Scheduler tables. New scheduler tables go here, never into the shared ``app.models``."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, false, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ..models import GUID, TimestampMixin


class EventState(str, enum.Enum):
    """draft -> scheduled -> provisioning -> active -> completed; cancelled from any
    state before completed. Transitions live in :mod:`.lifecycle`."""

    draft = "draft"
    scheduled = "scheduled"
    provisioning = "provisioning"
    active = "active"
    completed = "completed"
    cancelled = "cancelled"


class ScheduledEvent(TimestampMixin, Base):
    """Resource-reserving event to prevent over-commitment of cluster capacity."""

    __tablename__ = "scheduled_events"
    __table_args__ = (
        Index("ix_event_tenant_start", "tenant_id", "start_time"),
        Index("ix_event_state", "state"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[EventState] = mapped_column(Enum(EventState), default=EventState.draft)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    range_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("ranges.id"), nullable=True)
    template_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("templates.id"), nullable=True)
    # Who teaches it (one session per instructor at a time) and who booked it.
    instructor_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    # The class: its course's active Students attend (feed, invites, reminders).
    course_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("courses.id"), nullable=True)
    # What runs on the range. A linked exercise is used as it is; otherwise, with a
    # scenario, the clock creates a pending exercise when it builds the range
    # (auto_exercise), and cancelling the booking cancels that exercise if unstarted.
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("scenarios.id"), nullable=True)
    exercise_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("exercises.id"), nullable=True)
    auto_exercise: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    # Schedule
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Set by the clock (scheduler/clock.py). auto_provisioned: this booking built its
    # range and still owns the live build; only such a range is ever torn down by the
    # scheduler, never one that was already up. A reminder goes out once.
    auto_provisioned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # iCalendar SEQUENCE: raised whenever a calendar would need to update the event
    # (time, name, cancellation), so clients replace their copy (RFC 5545 §3.8.7.4).
    sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Resource reservation (claimed at schedule time)
    vm_count: Mapped[int] = mapped_column(Integer, default=0)
    vcpu_total: Mapped[int] = mapped_column(Integer, default=0)
    ram_mb_total: Mapped[int] = mapped_column(Integer, default=0)
    disk_gb_total: Mapped[int] = mapped_column(Integer, default=0)

    # Relations
    tenant = relationship("Tenant", lazy="select")


class OvercapacityPolicy(str, enum.Enum):
    """What happens to a booking that does not fit (ADR 0004, "Booking rules")."""

    block = "block"  # refused with 409, for everyone
    warn = "warn"  # created; the response carries the warnings and they are audit-logged


class SchedulerSetting(Base):
    """Platform-wide scheduler settings, one row per key. Platform-wide because every
    tenant books against the same cluster. Changed by admins only, audit-logged."""

    __tablename__ = "scheduler_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)


class FeedToken(Base):
    """A user's calendar-feed token (ADR 0004 §6). Calendar clients cannot log in, so
    the feed URL carries this bearer secret. Only its SHA-256 is stored; one per user;
    regenerating replaces it, revoking deletes it."""

    __tablename__ = "scheduler_feed_tokens"
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
