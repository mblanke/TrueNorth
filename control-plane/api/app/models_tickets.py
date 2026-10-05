"""TrueNorth Range - Trouble ticket ORM models.

Kept out of ``models.py`` so the tickets section can change without touching the
shared model file; ``models.py`` re-exports these at its foot.

A ticket is numbered per tenant (shown as ``TN-<number>``). Its ``status`` doubles as
the kanban column and ``board_order`` orders cards within a column. Every change to a
tracked field writes a ``TicketActivity`` row, which is the ticket's history.

User ids (reporter, assignee, author, actor) are not foreign keys: with AUTH_DISABLED
the dev identity has no ``users`` row, and a ticket must still be fileable.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import GUID, SoftDeleteMixin, TimestampMixin

TICKET_TYPES = ("incident", "bug", "task", "request")
TICKET_STATUSES = ("open", "in_progress", "waiting", "resolved", "closed")
TICKET_PRIORITIES = ("low", "medium", "high", "critical")


class SupportQueue(TimestampMixin, SoftDeleteMixin, Base):
    """A triage bucket, e.g. "Range support" or "Platform"."""

    __tablename__ = "support_queues"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_support_queues_tenant_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


class Ticket(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "number", name="uq_tickets_tenant_number"),
        Index("ix_tickets_tenant_status", "tenant_id", "status"),
        Index("ix_tickets_tenant_reporter", "tenant_id", "reporter_id"),
        Index("ix_tickets_tenant_assignee", "tenant_id", "assignee_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(20), default="incident")
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="open")
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    category: Mapped[str] = mapped_column(String(50), default="other")
    labels: Mapped[str] = mapped_column(String(500), default="")  # comma-separated
    queue_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("support_queues.id"), nullable=True)
    reporter_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    range_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("ranges.id"), nullable=True)
    exercise_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("exercises.id"), nullable=True)
    board_order: Mapped[float] = mapped_column(Float, default=0.0)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TicketComment(TimestampMixin, Base):
    __tablename__ = "ticket_comments"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    ticket_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tickets.id"), nullable=False, index=True)
    author_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Staff-only note: never returned to a caller without TICKET_WORK.
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False)


class TicketAttachment(TimestampMixin, Base):
    __tablename__ = "ticket_attachments"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    ticket_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tickets.id"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    object_key: Mapped[str] = mapped_column(String(600), default="")
    uploaded_by: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)


class TicketActivity(TimestampMixin, Base):
    """One field change on a ticket: who changed what, from what, to what."""

    __tablename__ = "ticket_activity"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    ticket_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tickets.id"), nullable=False, index=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    field: Mapped[str] = mapped_column(String(50), nullable=False)
    old_value: Mapped[str] = mapped_column(String(500), default="")
    new_value: Mapped[str] = mapped_column(String(500), default="")
