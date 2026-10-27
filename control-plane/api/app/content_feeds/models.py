"""The content feeds' tables (ADR 0014 decision 25, issue #287).

New tables go in their section, never into the shared ``app.models`` (ADR 0003).

* ``content_feed_entries``: what the release and vulnerability feeds say, normalised (one row
  per end-of-life date, point release, KEV entry or NVD CVE), whether the worker's daily pull
  or a signed bundle brought it. Public data: no tenant.
* ``content_feed_flags``: each (content review item, entry) the content clock's sync has
  flagged, so it is flagged once and Mark reviewed clears it for good.
* ``content_feed_sources``: per source (endoflife, cisa_kev, nvd, bundle) its last pull or
  import, when it last worked, its last error and its cursor: the feeds status panel.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID

__all__ = ["ContentFeedEntry", "ContentFeedFlag", "ContentFeedSource"]


class ContentFeedEntry(Base):
    __tablename__ = "content_feed_entries"
    __table_args__ = (Index("ix_content_feed_entries_source", "source"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    # "eol:ubuntu:20.04", "release:ubuntu:24.04:24.04.6", "kev:CVE-2024-1234", "nvd:CVE-2024-1234"
    entry_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # endoflife | cisa_kev | nvd
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # release | vulnerability
    products: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # product keys it names
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # when it took effect
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="online")  # online | bundle
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContentFeedFlag(Base):
    __tablename__ = "content_feed_flags"
    __table_args__ = (UniqueConstraint("item_id", "entry_key", name="uq_content_feed_flag"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("content_review_items.id", ondelete="CASCADE"), nullable=False)
    entry_key: Mapped[str] = mapped_column(String(200), nullable=False)
    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContentFeedSource(Base):
    __tablename__ = "content_feed_sources"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)  # endoflife | cisa_kev | nvd | bundle
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    entries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # entries the last run brought
    cursor: Mapped[str] = mapped_column(String(64), nullable=False, default="")  # NVD: end of the last window
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # bundle: key id, generated at, by
