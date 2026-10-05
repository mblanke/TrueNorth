"""Lab sessions: one student's (or team's) own small range for one range activity.

A session is keyed by (tenant, user, release, activity, attempt): launching twice, a
browser refresh or a retried request returns the same session and never a second range.
Its VMs are an ordinary ``Range`` built by the existing worker tasks; the session owns the
lease, the isolated networks, readiness and teardown. Evidence a student or validator
submits is kept on the session, so it survives the range being destroyed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID

QUEUED = "queued"  # waiting for capacity (quota or networks)
PROVISIONING = "provisioning"  # VMs being built
BASELINING = "baselining"  # built and healthy; taking the snapshot reset returns to
READY = "ready"  # usable, not yet opened
ACTIVE = "active"  # the student has opened it
RESETTING = "resetting"
COMPLETED = "completed"  # ended by the student or an instructor
EXPIRED = "expired"  # idle or maximum lifetime reached
CLEANING = "cleaning"  # VMs being destroyed
DESTROYED = "destroyed"
FAILED = "failed"
RECONCILE = "reconcile_required"  # teardown did not finish cleanly; leftovers being removed

LIVE = (QUEUED, PROVISIONING, BASELINING, READY, ACTIVE, RESETTING)
ENDING = (COMPLETED, EXPIRED, CLEANING, RECONCILE)
TERMINAL = (DESTROYED, FAILED)
HOLDS_RESOURCES = LIVE + ENDING


class LabSession(Base):
    __tablename__ = "lab_sessions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "release_id", "activity_id", "attempt", name="uq_lab_session_key"),
        Index("ix_lab_sessions_state", "state"),
        Index("ix_lab_sessions_user", "user_id", "state"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    release_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("course_releases.id"), nullable=False)
    activity_id: Mapped[str] = mapped_column(String(16), nullable=False)  # the release module, e.g. mod_003
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default=QUEUED)
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    backend: Mapped[str] = mapped_column(String(50), nullable=False)
    range_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("ranges.id"), nullable=True)
    baseline_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    vcpu: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ram_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    probes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # last readiness probe results (JSON)
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # submitted evidence (JSON list)
    readiness_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    provisioning_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idle_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # runner lease


class LabNetworkLease(Base):
    """A pre-created, isolated hypervisor network (port group) from the pool in
    ``LAB_PORT_GROUPS``. A session leases one per network in its lab profile and returns
    them when its VMs are gone, so two sessions never share a network."""

    __tablename__ = "lab_network_leases"
    port_group: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("lab_sessions.id"), nullable=True, index=True
    )
    network_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
