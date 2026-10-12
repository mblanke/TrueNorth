"""``audit_exports``: one row per signed export of a UTC day of audit_logs (ADR 0011).

Written only by the worker (worker/audit_tasks.py) after the files were written to the
archive and verified. Never updated or deleted (database triggers, migration
b7c8d9e0f1a2), and its existence for a day is what lets the retention job delete that
day's audit rows. ``prev_manifest_sha256`` chains each export to the one written before.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID


class AuditExport(Base):
    __tablename__ = "audit_exports"
    __table_args__ = (Index("ix_audit_exports_created_at", "created_at"),)

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    # 0 for the day's export; 1..n for supplements (rows that committed after it).
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, default=0)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    last_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    prev_manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    key_id: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
