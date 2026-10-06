"""Course release lifecycle: upload a candidate, accept it once, pin enrollments to it.

Accepting a release is the point where authored content reaches the course: the release's
course file is imported (keyed on the catalogue code, so no second course is created), the
previous accepted release is superseded, and new enrollments pin to the new one. Existing
pins never move.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import yaml
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..course_content_ingest import find_catalogue_course, import_course_content
from ..models import Course, Enrollment
from . import bundle as bundle_mod
from .models import ACCEPTED, CANDIDATE, SUPERSEDED, CourseRelease, CourseReleaseBlob, EnrollmentReleasePin

logger = logging.getLogger(__name__)


class ReleaseRefusedError(ValueError):
    """A request the release lifecycle refuses; ``status`` is the HTTP code to answer with."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def create_candidate(
    db: Session, data: bytes, *, tenant_id: uuid.UUID | None, user_id: uuid.UUID | None
) -> tuple[CourseRelease, bool]:
    """Verify and store an upload. Returns (release, created). The same bytes uploaded twice
    to one tenant return the first release rather than a duplicate."""
    try:
        parsed = bundle_mod.parse(data)
    except bundle_mod.BundleError as exc:
        raise ReleaseRefusedError(str(exc), 422) from exc
    meta = parsed.meta
    existing = (
        db.query(CourseRelease)
        .filter(CourseRelease.tenant_id == tenant_id, CourseRelease.release_digest == meta["release_digest"])
        .one_or_none()
    )
    if existing is not None:
        return existing, False
    course = find_catalogue_course(db, meta["catalogue_code"], tenant_id)
    if course is None:
        raise ReleaseRefusedError(
            f"no catalogue course {meta['catalogue_code']!r} in this tenant; import the programme catalogue first", 422
        )
    if db.get(CourseReleaseBlob, parsed.sha256) is None:
        db.add(CourseReleaseBlob(sha256=parsed.sha256, size=len(data), data=data))
    # Serialise uploads for one course (the course row lock) so two at once cannot both
    # take the next version; the unique (course, version) constraint backs it up.
    db.query(Course).filter(Course.id == course.id).with_for_update().one()
    version = (db.query(func.max(CourseRelease.version)).filter(CourseRelease.course_id == course.id).scalar() or 0) + 1
    release = CourseRelease(
        tenant_id=tenant_id,
        course_id=course.id,
        catalogue_code=meta["catalogue_code"],
        arc2_code=meta["arc2_code"],
        run_id=meta["run_id"],
        slug=meta["slug"],
        title=meta["title"],
        version=version,
        release_digest=meta["release_digest"],
        learner_digest=meta["parts"]["learner"]["digest"],
        platform_digest=meta["parts"]["platform"]["digest"],
        instructor_digest=meta["parts"]["instructor"]["digest"],
        blob_sha256=parsed.sha256,
        meta=json.dumps(meta, sort_keys=True),
        state=CANDIDATE,
        created_by=user_id,
    )
    db.add(release)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ReleaseRefusedError("another upload for this course was stored at the same moment; upload again") from exc
    logger.info("course release %s v%d candidate for %s", release.id, version, release.catalogue_code)
    return release, True


def load_bundle(db: Session, release: CourseRelease) -> bundle_mod.Bundle:
    blob = db.get(CourseReleaseBlob, release.blob_sha256)
    if blob is None:  # the FK makes this unreachable short of manual deletion
        raise ReleaseRefusedError(f"release {release.id} has lost its stored content", 500)
    return bundle_mod.parse(blob.data)


def open_actions(release: CourseRelease) -> list[dict[str, str]]:
    return list(json.loads(release.meta).get("open_human_actions") or [])


def accept(
    db: Session,
    release: CourseRelease,
    *,
    user_id: uuid.UUID | None,
    acknowledge: list[str],
    notes: str = "",
) -> CourseRelease:
    """Accept a candidate: import its course file, supersede the previous accepted release.
    Every open human action ARC² recorded must be acknowledged by id; acceptance records who
    acknowledged what, rather than letting a candidate with open actions through silently."""
    if release.state != CANDIDATE:
        raise ReleaseRefusedError(f"release is {release.state}; only a candidate can be accepted")
    # One acceptance per course at a time: lock the course row, then re-read what is live.
    db.query(Course).filter(Course.id == release.course_id).with_for_update().one()
    db.refresh(release)
    if release.state != CANDIDATE:
        raise ReleaseRefusedError(f"release is {release.state}; only a candidate can be accepted")
    current = active_release(db, release.course_id)
    if current is not None and current.version > release.version:
        raise ReleaseRefusedError(
            f"v{current.version} is already accepted; v{release.version} is older and would replace newer content"
        )
    pending = [a["id"] for a in open_actions(release) if a["id"] not in set(acknowledge)]
    if pending:
        raise ReleaseRefusedError("acknowledge the open actions first: " + ", ".join(pending))
    parsed = load_bundle(db, release)
    import_course_content(db, _as_catalogue_course(parsed), release.tenant_id, commit=False, release_mode=True)

    previous = (
        db.query(CourseRelease)
        .filter(CourseRelease.course_id == release.course_id, CourseRelease.state == ACCEPTED)
        .all()
    )
    for old in previous:
        old.state = SUPERSEDED
    db.flush()  # the old release leaves `accepted` before the new one enters it (one per course)
    release.state = ACCEPTED
    release.accepted_by = user_id
    release.accepted_at = datetime.now(UTC)
    release.acknowledged_actions = json.dumps(sorted(set(acknowledge)))
    release.notes = notes
    db.flush()
    logger.info("course release %s v%d accepted for %s", release.id, release.version, release.catalogue_code)
    return release


def _as_catalogue_course(parsed: bundle_mod.Bundle) -> str:
    """The release's course file with its catalogue identity: ARC² runs carry an ARC2-…
    code, the course they deliver is the catalogue one."""
    doc = yaml.safe_load(parsed.files["platform"][parsed.meta["course_yaml"]].decode("utf-8"))
    doc["course_code"] = parsed.meta["catalogue_code"]
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def active_release(db: Session, course_id: uuid.UUID) -> CourseRelease | None:
    return (
        db.query(CourseRelease)
        .filter(CourseRelease.course_id == course_id, CourseRelease.state == ACCEPTED)
        .one_or_none()
    )


def pin_enrollment(db: Session, enrollment: Enrollment) -> EnrollmentReleasePin | None:
    """Pin an enrollment to its course's accepted release, once. A course with no accepted
    release (legacy content) leaves the enrollment unpinned."""
    pin = db.get(EnrollmentReleasePin, enrollment.id)
    if pin is not None:
        return pin
    release = active_release(db, enrollment.course_id)
    if release is None:
        return None
    pin = EnrollmentReleasePin(enrollment_id=enrollment.id, release_id=release.id)
    db.add(pin)
    db.flush()
    return pin


def pinned_release(db: Session, enrollment_id: uuid.UUID) -> CourseRelease | None:
    pin = db.get(EnrollmentReleasePin, enrollment_id)
    return db.get(CourseRelease, pin.release_id) if pin else None


def course_status(db: Session, course: Course) -> dict[str, Any]:
    active = active_release(db, course.id)
    return {
        "course_id": course.id,
        "legacy": active is None,  # content that predates releases, labelled as such
        "active_release_id": active.id if active else None,
        "active_version": active.version if active else None,
        "candidates": db.query(CourseRelease)
        .filter(CourseRelease.course_id == course.id, CourseRelease.state == CANDIDATE)
        .count(),
    }
