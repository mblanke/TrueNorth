"""Enrollment creation, shared by every path that enrolls someone.

Extracted from ``routers/courses.py`` because three callers need it — the course
enroll endpoint, registration approval, and onboarding path selection — and the
ModuleProgress fan-out would otherwise be copy-pasted three times and drift.

Idempotent by design: ``enrollments`` carries ``UniqueConstraint(user_id,
course_id)``, so a repeated call returns the existing row rather than raising.
That matters because approving a cohort and then completing onboarding both
enroll the same person on the same courses.
"""

from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy.orm import Session

from .models import (
    Course,
    CourseModule,
    Enrollment,
    EnrollmentStatus,
    LearningPath,
    ModuleProgress,
)

logger = logging.getLogger(__name__)


def ensure_enrollment(
    db: Session,
    *,
    user_id: uuid.UUID,
    course_id: uuid.UUID,
    tenant_id: uuid.UUID | str,
) -> Enrollment:
    """Enroll ``user_id`` on ``course_id``, creating ModuleProgress rows.

    Returns the existing enrollment unchanged if one is already present. Flushes
    but does not commit — the caller owns the transaction, which is what lets
    approval create the user, the enrollment and the progress rows atomically.
    """
    existing = (
        db.query(Enrollment)
        .filter(Enrollment.user_id == user_id, Enrollment.course_id == course_id)
        .first()
    )
    if existing:
        return existing

    enrollment = Enrollment(
        user_id=user_id,
        course_id=course_id,
        tenant_id=tenant_id,
        status=EnrollmentStatus.enrolled,
    )
    db.add(enrollment)
    db.flush()

    modules = (
        db.query(CourseModule)
        .filter(CourseModule.course_id == course_id)
        .order_by(CourseModule.ordinal)
        .all()
    )
    for mod in modules:
        db.add(ModuleProgress(enrollment_id=enrollment.id, module_id=mod.id))
    db.flush()

    logger.info("Enrolled user %s in course %s (%d modules)", user_id, course_id, len(modules))
    return enrollment


def courses_for_learning_path(db: Session, learning_path_id: uuid.UUID) -> list[Course]:
    """Resolve the ordered course list for a learning path.

    ``LearningPath.course_ids`` is a JSON array of course ids rather than an
    association table, so the order in that array is the intended sequence and
    is preserved here. Ids that no longer resolve are skipped rather than
    failing the whole enrollment — a stale id should not block a trainee.
    """
    path = db.query(LearningPath).filter(LearningPath.id == learning_path_id).first()
    if path is None:
        return []

    # course_ids is a Text column holding a JSON array, not a JSON/ARRAY type,
    # so it always arrives as a string.
    try:
        raw = json.loads(path.course_ids or "[]")
    except ValueError:
        logger.warning("learning_path %s has unparseable course_ids", learning_path_id)
        return []
    if not isinstance(raw, list):
        logger.warning("learning_path %s course_ids is not a list", learning_path_id)
        return []

    ordered: list[Course] = []
    for cid in raw:
        try:
            key = uuid.UUID(str(cid))
        except (ValueError, AttributeError, TypeError):
            logger.warning("learning_path %s references malformed course id %r", learning_path_id, cid)
            continue
        course = db.query(Course).filter(Course.id == key).first()
        if course is None:
            logger.warning("learning_path %s references missing course %s", learning_path_id, key)
            continue
        ordered.append(course)
    return ordered


def ensure_path_enrollment(
    db: Session,
    *,
    user_id: uuid.UUID,
    learning_path_id: uuid.UUID,
    tenant_id: uuid.UUID | str,
) -> list[Enrollment]:
    """Enroll ``user_id`` on every course in a learning path. Idempotent."""
    courses = courses_for_learning_path(db, learning_path_id)
    return [
        ensure_enrollment(db, user_id=user_id, course_id=c.id, tenant_id=tenant_id) for c in courses
    ]
