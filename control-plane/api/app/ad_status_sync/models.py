"""AD status sync tables (ADR 0014 decision 6; issue #290).

New tables go in their section, never into the shared ``app.models`` (ADR 0003).

- ``ad_status_sync_runs``: one row per run, nightly or "run now". ``slot`` is unique: the
  nightly run claims ``nightly:<UTC date>`` by inserting it, so of any number of API
  replicas exactly one runs it. A manual run's slot is ``manual:<uuid>``. ``tenant_id`` is
  None for the nightly run (every tenant) and the caller's tenant for a manual one.
- ``ad_status_sync_results``: one row per account the run deactivated (or, in a dry run,
  would have). ``applied`` is false in a dry run and for an account another replica or
  run deactivated first.
- ``user_last_seen``: when TrueNorth last saw each account authenticate (written at most
  once an hour per account and process, ``last_seen.note``). The 90-day rule for local
  accounts reads it.

Nothing here is deleted by the API (ADR 0010).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID


class AdStatusSyncRun(Base):
    __tablename__ = "ad_status_sync_runs"
    __table_args__ = (Index("ix_ad_status_sync_runs_started", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    slot: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)  # nightly | manual
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # running | completed | held (too many to deactivate at once) | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    counts: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class AdStatusSyncResult(Base):
    __tablename__ = "ad_status_sync_results"
    __table_args__ = (Index("ix_ad_status_sync_results_run_tenant", "run_id", "tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("ad_status_sync_runs.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    # The account's address and name when the run saw it: the record does not change later.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # ad_disabled | ad_removed | ad_no_tn_group | local_unused
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    seats_freed: Mapped[int] = mapped_column(default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserLastSeen(Base):
    __tablename__ = "user_last_seen"

    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
