"""Results pulled back from Moodle: one cursor per Moodle, one ledger row per fact.

``moodle_result_cursors`` is where TrueNorth is in a Moodle's change log (the plugin's
opaque cursor) and who is pulling it now: ``lease_until``/``lease_holder`` keep two API
processes from pulling one Moodle at once, as course publications do.

``moodle_result_records`` is the idempotency ledger: one row per (Moodle, person, fact),
the fact being an activity's completion, a quiz's grade or a course's completion. A row
pulled again with the same content changes nothing; a changed grade updates the one quiz
attempt the ledger row points at instead of adding another.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID


class MoodleResultCursor(Base):
    __tablename__ = "moodle_result_cursors"

    platform_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("external_platforms.id"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    cursor: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rows_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_applied: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_holder: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class MoodleResultRecord(Base):
    __tablename__ = "moodle_result_records"
    __table_args__ = (
        UniqueConstraint("platform_id", "user_id", "ref", name="uq_moodle_result_record"),
        Index("ix_moodle_result_records_course", "platform_id", "course_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    platform_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("external_platforms.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    course_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("courses.id"), nullable=False)
    # "completion:<activity idnumber>" | "quiz_grade:<activity idnumber>" | "course_completion"
    ref: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # Moodle completion state
    grade: Mapped[float | None] = mapped_column(Float, nullable=True)
    grade_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_time: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # Moodle's timemodified
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    quiz_attempt_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("quiz_attempts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
