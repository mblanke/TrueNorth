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
    User,
    UserRole,
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
    # A course with an accepted release delivers that release to this enrollment for good;
    # a later release does not move it (app.course_releases).
    from .course_releases.service import pin_enrollment

    pin_enrollment(db, enrollment)

    logger.info("Enrolled user %s in course %s (%d modules)", user_id, course_id, len(modules))
    return enrollment


def complete_enrollment(db: Session, enrollment: Enrollment) -> Enrollment:
    """Mark an enrollment completed with its final score and letter grade, computed from
    its recorded module progress (never supplied by a caller). Flushes, does not commit.

    One rule for every path that completes a course: ``POST /courses/{id}/complete`` and
    the results pulled back from Moodle (app/moodle_results)."""
    from datetime import UTC, datetime

    progress_rows = db.query(ModuleProgress).filter(ModuleProgress.enrollment_id == enrollment.id).all()
    # A module with max_score 0 was completed with nothing to grade: it counts toward
    # neither total. A course with nothing graded at all has no letter grade.
    total_score = sum(p.score or 0 for p in progress_rows if p.max_score)
    max_score = sum(p.max_score or 0 for p in progress_rows)
    grade = None
    if max_score:
        pct = (total_score / max_score) * 100
        grade = next((letter for floor, letter in ((90, "A"), (80, "B"), (70, "C"), (60, "D")) if pct >= floor), "F")
    enrollment.status = EnrollmentStatus.completed
    enrollment.completed_at = datetime.now(UTC)
    enrollment.final_score = total_score
    enrollment.max_score = max_score
    enrollment.final_grade = grade
    db.flush()
    return enrollment


def courses_for_learning_path(
    db: Session, learning_path_id: uuid.UUID, *, tenant_id: uuid.UUID | str
) -> list[Course]:
    """Resolve the ordered course list for a learning path of ``tenant_id`` (or a shared one).

    ``LearningPath.course_ids`` is a JSON array of course ids rather than an
    association table, so the order in that array is the intended sequence and
    is preserved here. Ids that no longer resolve are skipped rather than
    failing the whole enrollment — a stale id should not block a trainee.

    Both the path and its courses must be the tenant's own or shared (tenant_id NULL).
    Registration passes a path id the applicant chose, and a path's JSON list has no
    foreign key, so neither could otherwise be trusted to stay in the tenant.
    """
    tid = uuid.UUID(str(tenant_id))
    path = (
        db.query(LearningPath)
        .filter(
            LearningPath.id == learning_path_id,
            (LearningPath.tenant_id == tid) | LearningPath.tenant_id.is_(None),
        )
        .first()
    )
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
        course = (
            db.query(Course)
            .filter(Course.id == key, (Course.tenant_id == tid) | Course.tenant_id.is_(None))
            .first()
        )
        if course is None:
            logger.warning("learning_path %s references missing or foreign course %s", learning_path_id, key)
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
    courses = courses_for_learning_path(db, learning_path_id, tenant_id=tenant_id)
    return [
        ensure_enrollment(db, user_id=user_id, course_id=c.id, tenant_id=tenant_id) for c in courses
    ]


# -- Who is in a course (the scheduler's class of Students, ADR 0004) ----------
ACTIVE_STATUSES = (EnrollmentStatus.enrolled, EnrollmentStatus.in_progress)


def active_students(db: Session, course_id: uuid.UUID) -> list[User]:
    """Active Students enrolled in a course and still studying it."""
    return (
        db.query(User)
        .join(Enrollment, Enrollment.user_id == User.id)
        .filter(
            Enrollment.course_id == course_id,
            Enrollment.status.in_(ACTIVE_STATUSES),
            User.role == UserRole.student,
            User.is_active == True,  # noqa: E712
            User.deleted_at.is_(None),
        )
        .all()
    )


def active_course_ids(db: Session, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Courses a user is enrolled in and still studying."""
    return [
        cid
        for (cid,) in db.query(Enrollment.course_id)
        .filter(Enrollment.user_id == user_id, Enrollment.status.in_(ACTIVE_STATUSES))
        .all()
    ]
