"""cmi5 registrations and sessions: TrueNorth acting as the cmi5 LMS for its own releases.

Kept out of ``app/models.py`` (ADR 0003: per-section modules). docs/cmi5.md has the model.

* A **registration** is a Student's enrolment in a course, as cmi5 means it (learner x
  course). Its id IS the enrolment id (``enrollments.id``): the same value TrueNorth's own
  xAPI statements carry as ``context.registration`` (app/xapi_context.py), so a Student's
  course record is one registration whichever path produced it. It is pinned to the release
  the enrolment is pinned to. ``progress`` holds what moveOn needs: per AU completed /
  passed / waived, and which AUs, blocks and the course are satisfied.
* A **session** is one AU launch, ``launched`` to ``terminated`` or ``abandoned``. The
  one-time fetch secret and the session's auth token are stored as SHA-256 hashes only.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID

LAUNCHED = "launched"
INITIALIZED = "initialized"
TERMINATED = "terminated"
ABANDONED = "abandoned"
OPEN_STATES = (LAUNCHED, INITIALIZED)


class Cmi5Registration(Base):
    __tablename__ = "cmi5_registrations"
    __table_args__ = (Index("ix_cmi5_reg_user", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True)  # == enrollment_id
    enrollment_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("enrollments.id"), unique=True, nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    release_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("course_releases.id"), nullable=False)
    progress: Mapped[str] = mapped_column(Text, nullable=False, default="{}")  # JSON, see module docstring
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Cmi5Grade(Base):
    """One server-marked quiz attempt (``POST .../grade``). The marking is TrueNorth's, so a
    TrueNorth session's ``passed``/``failed`` must report the score recorded here; the rows
    also count attempts against ``CMI5_GRADE_ATTEMPTS``."""

    __tablename__ = "cmi5_grades"
    __table_args__ = (Index("ix_cmi5_grade_user_au", "user_id", "release_id", "au_index", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    release_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("course_releases.id"), nullable=False)
    au_index: Mapped[int] = mapped_column(Integer, nullable=False)
    correct: Mapped[int] = mapped_column(Integer, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    scaled: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Cmi5Session(Base):
    __tablename__ = "cmi5_sessions"
    __table_args__ = (Index("ix_cmi5_session_reg_state", "registration_id", "state"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True)  # the cmi5 session id
    registration_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("cmi5_registrations.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    au_index: Mapped[int] = mapped_column(Integer, nullable=False)
    launch_mode: Mapped[str] = mapped_column(String(8), nullable=False)  # Normal | Browse | Review
    move_on: Mapped[str] = mapped_column(String(24), nullable=False)
    mastery_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    context_template: Mapped[str] = mapped_column(Text, nullable=False)  # JSON, as written to LMS.LaunchData
    fetch_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=LAUNCHED)
    sent: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # cmi5-defined verbs this session
    launch_data_fetched: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prefs_fetched: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    launched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    initialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
