"""The content clock's tables (ADR 0014 decisions 22-24, issue #286).

New tables go in their section, never into the shared ``app.models`` (ADR 0003).

* ``content_review_items``: the review state of one piece of content (a course, module,
  lab, scenario, range template or golden template). The content itself stays where it is;
  ``item_type`` + ``item_id`` point at it. ``tenant_id`` is the content's: null is shared
  catalogue content, readable by every tenant and changed only by a platform administrator.
* ``content_review_edges``: ``item`` depends on ``depends_on`` (a range template on a golden
  template, a course on its module). Staleness flows from ``depends_on`` to ``item``.
* ``content_review_themes``: theme tags with a colour (Blue team, DFIR, ...), per tenant or
  platform-wide (null).
* ``content_review_cycles``: the review cycle per content type, 90 to 120 days, per tenant
  or platform-wide (null). Without a row the cycle is 90.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, false, text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID, TimestampMixin
from .staleness import ITEM_TYPES

__all__ = ["ITEM_TYPES", "ContentReviewCycle", "ContentReviewEdge", "ContentReviewItem", "ContentReviewTheme"]


class ContentReviewItem(TimestampMixin, Base):
    """One piece of content's review state. ``stale_reasons`` holds the reasons
    (``staleness`` module docstring); ``stale`` is whether there are any. ``due_at`` is
    stored so a date window is one indexed query. ``revision`` rises on every write, so the
    worker's sync never overwrites a review made while it was computing."""

    __tablename__ = "content_review_items"
    __table_args__ = (
        UniqueConstraint("item_type", "item_id", name="uq_content_review_item"),
        Index("ix_content_review_items_tenant_due", "tenant_id", "due_at"),
        Index("ix_content_review_items_stale", "stale"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    owner_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    theme_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Null: the content type's cycle (content_review_cycles, else 90).
    cycle_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reviewed_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    stale_reasons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    retired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    # The content is gone (deleted); the row is kept for its review history and hidden.
    missing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class ContentReviewEdge(Base):
    """``item_id`` depends on ``depends_on_id``. ``derived``: the periodic sync found it in
    the content and replaces it each run."""

    __tablename__ = "content_review_edges"
    __table_args__ = (
        UniqueConstraint("item_id", "depends_on_id", name="uq_content_review_edge"),
        Index("ix_content_review_edges_depends_on", "depends_on_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("content_review_items.id", ondelete="CASCADE"), nullable=False)
    depends_on_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("content_review_items.id", ondelete="CASCADE"), nullable=False)
    derived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ContentReviewTheme(TimestampMixin, Base):
    __tablename__ = "content_review_themes"
    __table_args__ = (Index("ix_content_review_themes_tenant", "tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    colour: Mapped[str] = mapped_column(String(7), nullable=False)  # #RRGGBB, the dot
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ContentReviewCycle(TimestampMixin, Base):
    __tablename__ = "content_review_cycles"
    __table_args__ = (
        Index("ix_content_review_cycles_tenant_type", "tenant_id", "item_type", unique=True),
        # NULLs are distinct in a unique index: one platform-wide row per type needs its own.
        Index("uq_content_review_cycles_platform", "item_type", unique=True,
              postgresql_where=text("tenant_id IS NULL"), sqlite_where=text("tenant_id IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)
    days: Mapped[int] = mapped_column(Integer, nullable=False)
