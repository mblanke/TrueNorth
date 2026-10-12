"""Results pulled back from Moodle: one cursor per Moodle, one ledger row per fact.

``moodle_result_cursors`` is where TrueNorth is in a Moodle's change log (the plugin's
opaque cursor) and who is pulling it now: ``lease_until``/``lease_holder`` keep two API
processes from pulling one Moodle at once, as course publications do.

``moodle_result_records`` is the idempotency ledger: one row per (Moodle, person, fact),
the fact being an activity's completion, a quiz's grade or a course's completion. A row
pulled again with the same content changes nothing; a changed grade updates the one quiz
attempt the ledger row points at instead of adding another.

A fact is matched on immutable ids only, never on an email: the platform, the TrueNorth
user id (the locked idnumber of the ``tn-<id>`` account TrueNorth's sign-in made), the
TrueNorth course id and the activity's ``tn:`` idnumber (a quiz's carries the hash of its
questions, so another release's quiz is another fact). ``release_id`` (the release whose
publication made the activity) and ``source_attempt_id`` (Moodle's ``quiz_attempts.id`` of
the latest finished attempt behind a grade) say where it came from.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
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
        # Activity idnumbers (tn:mod_NNN:...) repeat across courses: the course is part of the fact.
        UniqueConstraint("platform_id", "user_id", "course_id", "ref", name="uq_moodle_result_record"),
        Index("ix_moodle_result_records_course", "platform_id", "course_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    platform_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("external_platforms.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    course_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("courses.id"), nullable=False)
    # "completion:<activity idnumber>" | "quiz_grade:<activity idnumber>" (within course_id)
    ref: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # Moodle completion state
    grade: Mapped[float | None] = mapped_column(Float, nullable=True)
    grade_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_time: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # Moodle's timemodified
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    quiz_attempt_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("quiz_attempts.id"), nullable=True)
    release_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("course_releases.id", name="moodle_result_records_release_id_fkey"), nullable=True
    )
    source_attempt_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # Moodle quiz_attempts.id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
