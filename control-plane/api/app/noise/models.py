"""Noise engine tables.

Kept out of ``app/models.py`` so the noise section can change without touching the
shared god-file. Registered on the same ``Base``; ``app/noise/__init__.py`` imports this
module, and so does ``alembic/env.py``.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID, TimestampMixin


class NoiseProfile(TimestampMixin, Base):
    """The noise settings for one range. At most one per range."""

    __tablename__ = "noise_profiles"
    __table_args__ = (
        UniqueConstraint("range_id", name="uq_noise_profiles_range"),
        Index("ix_noise_profiles_tenant", "tenant_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    # CASCADE: deleting a range takes its noise with it (ranges.py knows nothing of us).
    range_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("ranges.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # A pause is distinct from level 0: it is the white cell's "stop everything now"
    # and leaves the configured level intact for when it is lifted.
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    level: Mapped[int] = mapped_column(Integer, default=40)
    seed: Mapped[int] = mapped_column(Integer, default=1)
    # The range's notional local time, as hours from UTC: drives the work-day curve.
    utc_offset: Mapped[int] = mapped_column(Integer, default=0)
    # {"zone:<vlan or zone>": level, "node:<hostname>": level}
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    pack: Mapped[str] = mapped_column(String(120), default="builtin")
    # What there is to talk to: pool -> hosts, e.g. {"web": ["intranet.corp.local"]}.
    # Pools are named in planner.TARGET_POOL. An empty pool means that activity is skipped.
    targets: Mapped[dict] = mapped_column(JSON, default=dict)
    # Signs every action handed to an agent. Never leaves the controller, so a stolen
    # agent token cannot mint ground truth for activity that was never planned.
    plan_key: Mapped[str] = mapped_column(String(64), default=lambda: secrets.token_hex(32))


class NoisePersona(TimestampMixin, Base):
    """A synthetic user. Lives on one node (their workstation or admin box)."""

    __tablename__ = "noise_personas"
    __table_args__ = (
        UniqueConstraint("profile_id", "handle", name="uq_noise_personas_handle"),
        Index("ix_noise_personas_tenant", "tenant_id"),
        Index("ix_noise_personas_profile_node", "profile_id", "node"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("noise_profiles.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    handle: Mapped[str] = mapped_column(String(64), nullable=False)  # account name
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="")
    department: Mapped[str] = mapped_column(String(120), default="")
    node: Mapped[str] = mapped_column(String(255), default="")
    work_start: Mapped[int] = mapped_column(Integer, default=8)  # local hour, inclusive
    work_end: Mapped[int] = mapped_column(Integer, default=17)  # local hour, exclusive
    # Activity kind -> relative weight, multiplied into the dial's protocol mix.
    habits: Mapped[dict] = mapped_column(JSON, default=dict)
    # Whether this persona may perform attack lookalikes (an IT admin running a scan).
    lookalikes: Mapped[bool] = mapped_column(Boolean, default=False)
    # Free-form: rank, appointment, bio, photo, contacts, sites. Filled by org packs.
    attrs: Mapped[dict] = mapped_column(JSON, default=dict)


class NoiseAgent(TimestampMixin, Base):
    """One in-VM agent. Authenticates with a token whose hash is all we keep."""

    __tablename__ = "noise_agents"
    __table_args__ = (
        UniqueConstraint("profile_id", "node", name="uq_noise_agents_node"),
        UniqueConstraint("token_hash", name="uq_noise_agents_token"),
        Index("ix_noise_agents_tenant", "tenant_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("noise_profiles.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    node: Mapped[str] = mapped_column(String(255), nullable=False)
    zone: Mapped[str] = mapped_column(String(120), default="")
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(40), default="")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Revoked rather than deleted, so the agent's ground truth survives for the AAR.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NoiseActivity(Base):
    """Ground truth: one thing an agent actually did. White cell only."""

    __tablename__ = "noise_activities"
    __table_args__ = (
        Index("ix_noise_activities_profile_at", "profile_id", "at"),
        # One planned action is recorded once, however often it is reported.
        UniqueConstraint("agent_id", "at", "persona", "kind", name="uq_noise_activities_action"),
        Index("ix_noise_activities_tenant", "tenant_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("noise_profiles.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("noise_agents.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # as planned (signed)
    persona: Mapped[str] = mapped_column(String(64), default="")
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(512), default="")
    lookalike: Mapped[bool] = mapped_column(Boolean, default=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
