"""Course publications: an accepted release delivered to one Moodle.

One row per (release, platform): asking again returns the same row, and a retry resumes
it. ``state`` moves requested → staging → verifying → activating → published, or to
failed (the reason is in ``error``; retry resumes from the start, every step being
idempotent by idnumber). ``lease_until`` is held by whichever process is running the job,
so two workers never drive the same publication at once, and a job whose process died is
picked up again once its lease lapses.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID

REQUESTED = "requested"
STAGING = "staging"
VERIFYING = "verifying"
ACTIVATING = "activating"
PUBLISHED = "published"
FAILED = "failed"
SUPERSEDED = "superseded"
RUNNING = (REQUESTED, STAGING, VERIFYING, ACTIVATING)


class CoursePublication(Base):
    __tablename__ = "course_publications"
    __table_args__ = (
        UniqueConstraint("release_id", "platform_id", name="uq_course_publication"),
        Index("ix_course_publications_state", "state"),
        Index("ix_course_publications_course", "course_id", "platform_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    release_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("course_releases.id"), nullable=False)
    course_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("courses.id"), nullable=False)
    platform_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("external_platforms.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=REQUESTED)
    stage_idnumber: Mapped[str] = mapped_column(String(100), nullable=False)
    live_idnumber: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    remote: Mapped[str] = mapped_column(Text, nullable=False, default="{}")  # last results per step (JSON)
    receipt: Mapped[str] = mapped_column(Text, nullable=False, default="{}")  # set once published (JSON)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_holder: Mapped[str | None] = mapped_column(String(32), nullable=True)  # the run that holds it
    requested_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
