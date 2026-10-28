"""The content port's tables (owner decision 2026-10-27, v1.2.0 GA; docs/content-port.md).

New tables go in their section, never into the shared ``app.models`` (ADR 0003).

"Proven" is **computed**, never stored (``proven.py``): it reads the certification records,
the closed classes, the completed exercises and the content clock as they are now. What is
stored is only what an import needs to remember:

* ``content_port_imports``: one row per bundle this site imported: its id (a bundle is
  imported once; a replay is refused), the key that signed it, the hash of what the
  signature covers, when it was made and by whom it was imported, where it landed.
* ``content_port_items``: one row per local item an import created or updated: which item
  of which origin it came from, the origin's content hash, when the bundle was made (an
  older bundle is a downgrade), the local fingerprint right after the import (a later local
  edit shows as a difference), and the origin's proof summary and golden-image builds, kept
  for display. An imported item is **not proven here** until this site's own evidence says
  so.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID, TimestampMixin

__all__ = ["ContentPortImport", "ContentPortItem"]


class ContentPortImport(Base):
    __tablename__ = "content_port_imports"
    __table_args__ = (UniqueConstraint("bundle_id", name="uq_content_port_imports_bundle"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    bundle_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    key_id: Mapped[str] = mapped_column(String(16), nullable=False)
    signed_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    exported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    imported_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    shared: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # counts per action


class ContentPortItem(TimestampMixin, Base):
    __tablename__ = "content_port_items"
    __table_args__ = (
        UniqueConstraint("item_type", "local_id", name="uq_content_port_items_local"),
        Index("ix_content_port_items_origin", "item_type", "origin_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)  # course | range_template | scenario
    local_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # The local item's tenant (null: a shared course). Matching an origin item is per tenant.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    origin_id: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_key_id: Mapped[str] = mapped_column(String(16), nullable=False)
    origin_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    exported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    import_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("content_port_imports.id"), nullable=False)
    local_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    proof: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    golden_builds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
