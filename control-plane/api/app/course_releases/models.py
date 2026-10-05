"""Course releases: the immutable, accepted versions of a course that the platform delivers.

Kept out of ``app/models.py`` (ADR 0003: per-section modules). A release is created as a
candidate from an ARC² release tarball, accepted once by a person with ``course:release``,
and superseded (never edited) by the next accepted release of the same course. The upload
is stored once, by sha256, and every digest the ARC² tooling computed is recomputed on
upload, so ``release_digest`` names exactly the stored content.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint, event, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID

CANDIDATE = "candidate"
ACCEPTED = "accepted"
SUPERSEDED = "superseded"
RELEASE_STATES = (CANDIDATE, ACCEPTED, SUPERSEDED)


class CourseReleaseBlob(Base):
    """An uploaded release tarball, content-addressed. Two releases with the same bytes
    share one blob; nothing ever rewrites one."""

    __tablename__ = "course_release_blobs"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CourseRelease(Base):
    __tablename__ = "course_releases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "release_digest", name="uq_course_release_digest"),
        UniqueConstraint("course_id", "version", name="uq_course_release_version"),
        Index("ix_course_releases_course_state", "course_id", "state"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    course_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("courses.id"), nullable=False)
    catalogue_code: Mapped[str] = mapped_column(String(32), nullable=False)
    arc2_code: Mapped[str] = mapped_column(String(32), nullable=False)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    release_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    learner_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    platform_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    instructor_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    blob_sha256: Mapped[str] = mapped_column(String(64), ForeignKey("course_release_blobs.sha256"), nullable=False)
    meta: Mapped[str] = mapped_column(Text, nullable=False)  # release.json as uploaded
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=CANDIDATE)
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_actions: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")


class EnrollmentReleasePin(Base):
    """The release an enrollment is taking. Set when the enrollment is created (or first
    meets an accepted release) and never moved by a later release, so a new version of a
    course does not change the course under a student's in-progress attempt."""

    __tablename__ = "enrollment_release_pins"
    enrollment_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("enrollments.id"), primary_key=True)
    release_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("course_releases.id"), nullable=False)
    pinned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# What a release is: fixed at upload. Only the acceptance fields and the state move.
IMMUTABLE_COLUMNS = (
    "tenant_id",
    "course_id",
    "catalogue_code",
    "arc2_code",
    "run_id",
    "slug",
    "title",
    "version",
    "release_digest",
    "learner_digest",
    "platform_digest",
    "instructor_digest",
    "blob_sha256",
    "meta",
    "created_by",
)
# Allowed state moves; anything else is a bug, not a request.
TRANSITIONS = {CANDIDATE: {ACCEPTED}, ACCEPTED: {SUPERSEDED}, SUPERSEDED: set()}


class ImmutableReleaseError(RuntimeError):
    pass


@event.listens_for(CourseRelease, "before_update")
def _refuse_rewrites(mapper, connection, target: CourseRelease) -> None:  # noqa: ARG001
    from sqlalchemy import inspect

    state = inspect(target)
    for col in IMMUTABLE_COLUMNS:
        hist = state.attrs[col].history
        if hist.has_changes() and hist.deleted:
            raise ImmutableReleaseError(f"course release {target.id}: {col} is fixed at upload")
    hist = state.attrs["state"].history
    if hist.has_changes() and hist.deleted:
        before, after = hist.deleted[0], target.state
        if after not in TRANSITIONS.get(before, set()):
            raise ImmutableReleaseError(f"course release {target.id}: {before} → {after} is not a release transition")
    accepted = state.attrs["accepted_at"].history
    if accepted.has_changes() and accepted.deleted and accepted.deleted[0] is not None:
        raise ImmutableReleaseError(f"course release {target.id}: acceptance is recorded once")
