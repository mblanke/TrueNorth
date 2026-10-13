"""Withdraw a fixture course: back to draft in TrueNorth, hidden in every Moodle it reached.

C105's committed release is a CI / CATAPULT cmi5 fixture, not a course for Students
(docs/content-library.md). Since #147 the installer leaves it a draft, but a host
installed earlier may have it published in TrueNorth, its quizzes published, and its
release published to a Moodle. This is the reviewed way to retire it there.

``plan()`` says exactly what would change and what is kept; ``apply()`` makes those
changes. Nothing is deleted: enrolments, module progress, quiz attempts, final grades
and Moodle result records stay as they are, and the Moodle course is hidden
(``set_visible(False)`` through the moodle_backends adapter), never removed. Running it
again changes nothing and says so. Every change is written to the audit log.

Only the codes in ``FIXTURE_CODES`` can be withdrawn: this is fixture cleanup, not a
general "retire any course" switch, which would need its own review.

Only one tenant is touched per call: the courses are found by catalogue code within
that tenant (a course named ``"{code} — {title}"``, programme_ingest.course_name, or the
course of a release with that ``catalogue_code``), and every related row is read through
those courses.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..course_releases.models import CourseRelease
from ..models import AuditLog, Course, CourseModule, Enrollment, ExternalPlatform, ModuleProgress, Quiz, QuizAttempt
from ..moodle_backends import MoodleError
from ..moodle_results.models import MoodleResultRecord
from . import service as publishing
from .models import FAILED, REQUESTED, RUNNING, CoursePublication

logger = logging.getLogger(__name__)

# Courses that exist only as test fixtures and must never be offered to Students. The
# installer's shipped-content publish skips the same set (scripts/load_content_inprocess.py
# KEEP_DRAFT). Adding a code here makes it withdrawable; review that like any other change.
FIXTURE_CODES = frozenset({"C105"})
AUDIT_ACTION = "withdraw_fixture_course"
ENROLMENT_LIMIT = 200
WITHDRAWN_ERROR = "stopped: the course was withdrawn as a fixture by a platform administrator"


class WithdrawRefusedError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


# ── Report shapes (the API's response model) ────────────────────────────────


class WithdrawQuiz(BaseModel):
    id: uuid.UUID
    title: str
    module_id: uuid.UUID | None
    is_published: bool
    attempts: int  # every attempt, kept
    submitted_attempts: int  # graded ones, kept


class WithdrawEnrolment(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    status: str
    final_grade: str | None
    final_score: int
    max_score: int


class WithdrawMoodle(BaseModel):
    platform_id: uuid.UUID
    platform_name: str
    live_idnumber: str  # the Moodle course's idnumber: the TrueNorth course id
    publications: list[uuid.UUID]
    publication_states: list[str]
    # Moodle's answer: "visible", "hidden", "absent" (no live course there) or "unknown"
    # (Moodle could not be asked; ``error`` says why). After apply, the re-checked state.
    visibility: str
    error: str = ""


class WithdrawCourse(BaseModel):
    id: uuid.UUID
    name: str
    is_published: bool
    quizzes: list[WithdrawQuiz]
    enrolments_total: int
    enrolments_graded: int  # with a final grade or score
    enrolments: list[WithdrawEnrolment]  # the first ENROLMENT_LIMIT, oldest first
    module_progress: int
    quiz_attempts: int
    moodle_results: int  # Moodle result ledger rows (completions, quiz grades)
    moodle: list[WithdrawMoodle]
    pending_publications: list[uuid.UUID]  # requested or stalled jobs apply stops
    running_publications: list[uuid.UUID]  # jobs another process is running now: apply refuses


class WithdrawReport(BaseModel):
    code: str
    tenant_id: uuid.UUID
    dry_run: bool
    courses: list[WithdrawCourse]
    changes: list[str]  # dry run: what apply would change; apply: what it changed
    changed: bool
    errors: list[str]
    preserved: dict[str, int]  # records left exactly as they were


# ── Finding the records ─────────────────────────────────────────────────────


def fixture_courses(db: Session, tenant_id: uuid.UUID, code: str) -> list[Course]:
    """The tenant's courses with this catalogue code, oldest first."""
    released = [
        row[0]
        for row in db.query(CourseRelease.course_id)
        .filter(CourseRelease.tenant_id == tenant_id, CourseRelease.catalogue_code == code)
        .distinct()
        .all()
    ]
    clauses = [Course.name == code, Course.name.like(f"{code} — %")]
    if released:
        clauses.append(Course.id.in_(released))
    return (
        db.query(Course)
        .filter(Course.tenant_id == tenant_id, or_(*clauses))
        .order_by(Course.created_at, Course.id)
        .all()
    )


def _quizzes(db: Session, course: Course) -> list[Quiz]:
    module_ids = [
        row[0]
        for row in db.query(CourseModule.id)
        .filter(CourseModule.course_id == course.id)  # tenant-safe: the modules of a course checked in this tenant
        .all()
    ]
    if not module_ids:
        return []
    return (
        db.query(Quiz)
        .filter(Quiz.tenant_id == course.tenant_id, Quiz.module_id.in_(module_ids), Quiz.deleted_at.is_(None))
        .order_by(Quiz.title, Quiz.id)
        .all()
    )


def _publications(db: Session, course: Course) -> list[CoursePublication]:
    return (
        db.query(CoursePublication)
        .filter(CoursePublication.tenant_id == course.tenant_id, CoursePublication.course_id == course.id)
        .order_by(CoursePublication.created_at, CoursePublication.id)
        .all()
    )


def _lease_live(pub: CoursePublication, now: datetime) -> bool:
    until = pub.lease_until
    if until is None:
        return False
    if until.tzinfo is None:  # SQLite hands back naive datetimes
        until = until.replace(tzinfo=UTC)
    return until >= now


def _describe(db: Session, platform: ExternalPlatform, idnumber: str) -> tuple[str, str]:
    """(visibility, error) of the live course on this Moodle."""
    try:
        described = publishing.backend_for(db, platform).describe_course(platform, idnumber)
    except (MoodleError, ValueError) as exc:
        return "unknown", str(exc)
    if not described.get("exists"):
        return "absent", ""
    return ("visible" if described.get("visible") else "hidden"), ""


def _course_report(db: Session, course: Course, *, ask_moodle: bool) -> WithdrawCourse:
    quizzes = _quizzes(db, course)
    quiz_ids = [q.id for q in quizzes]
    attempts: dict[uuid.UUID, tuple[int, int]] = {qid: (0, 0) for qid in quiz_ids}
    if quiz_ids:
        for qid, submitted in (
            db.query(QuizAttempt.quiz_id, QuizAttempt.submitted_at)
            .filter(QuizAttempt.tenant_id == course.tenant_id, QuizAttempt.quiz_id.in_(quiz_ids))
            .all()
        ):
            total, done = attempts[qid]
            attempts[qid] = (total + 1, done + (1 if submitted is not None else 0))

    enrol_q = db.query(Enrollment).filter(Enrollment.tenant_id == course.tenant_id, Enrollment.course_id == course.id)
    enrolments_total = enrol_q.count()
    enrolments_graded = enrol_q.filter(or_(Enrollment.final_grade.isnot(None), Enrollment.final_score > 0)).count()
    enrolments = enrol_q.order_by(Enrollment.enrolled_at, Enrollment.id).limit(ENROLMENT_LIMIT).all()
    enrolment_ids = db.query(Enrollment.id).filter(
        Enrollment.tenant_id == course.tenant_id, Enrollment.course_id == course.id
    )
    progress = db.query(ModuleProgress).filter(ModuleProgress.enrollment_id.in_(enrolment_ids)).count()
    # tenant-safe: the ledger rows of a course already checked to be in this tenant.
    results = db.query(MoodleResultRecord).filter(MoodleResultRecord.course_id == course.id).count()

    now = datetime.now(UTC)
    pubs = _publications(db, course)
    running = [p.id for p in pubs if p.state in (*RUNNING, REQUESTED) and _lease_live(p, now)]
    pending = [p.id for p in pubs if p.state in (*RUNNING, REQUESTED) and not _lease_live(p, now)]
    moodle: list[WithdrawMoodle] = []
    by_platform: dict[uuid.UUID, list[CoursePublication]] = {}
    for p in pubs:
        by_platform.setdefault(p.platform_id, []).append(p)
    for platform_id, group in by_platform.items():
        platform = (
            db.query(ExternalPlatform)
            .filter(ExternalPlatform.id == platform_id, ExternalPlatform.tenant_id == course.tenant_id)
            .one_or_none()
        )
        idnumber = group[0].live_idnumber or str(course.id)
        entry = WithdrawMoodle(
            platform_id=platform_id,
            platform_name=platform.name if platform else "(platform not found in this tenant)",
            live_idnumber=idnumber,
            publications=[p.id for p in group],
            publication_states=[p.state for p in group],
            visibility="unknown",
        )
        if platform is None:
            entry.error = "the platform is not in this tenant"
        elif ask_moodle:
            entry.visibility, entry.error = _describe(db, platform, idnumber)
        moodle.append(entry)

    return WithdrawCourse(
        id=course.id,
        name=course.name,
        is_published=bool(course.is_published),
        quizzes=[
            WithdrawQuiz(
                id=q.id,
                title=q.title,
                module_id=q.module_id,
                is_published=bool(q.is_published),
                attempts=attempts[q.id][0],
                submitted_attempts=attempts[q.id][1],
            )
            for q in quizzes
        ],
        enrolments_total=enrolments_total,
        enrolments_graded=enrolments_graded,
        enrolments=[
            WithdrawEnrolment(
                id=e.id,
                user_id=e.user_id,
                status=getattr(e.status, "value", str(e.status)),
                final_grade=e.final_grade,
                final_score=e.final_score or 0,
                max_score=e.max_score or 0,
            )
            for e in enrolments
        ],
        module_progress=progress,
        quiz_attempts=sum(a for a, _ in attempts.values()),
        moodle_results=results,
        moodle=moodle,
        pending_publications=pending,
        running_publications=running,
    )


def _preserved(courses: list[WithdrawCourse]) -> dict[str, int]:
    return {
        "enrolments": sum(c.enrolments_total for c in courses),
        "graded_enrolments": sum(c.enrolments_graded for c in courses),
        "module_progress": sum(c.module_progress for c in courses),
        "quiz_attempts": sum(c.quiz_attempts for c in courses),
        "moodle_results": sum(c.moodle_results for c in courses),
    }


def _would_change(c: WithdrawCourse) -> list[str]:
    out = []
    if c.is_published:
        out.append(f"course {c.id} {c.name!r}: published -> draft")
    out += [f"quiz {q.id} {q.title!r}: published -> draft" for q in c.quizzes if q.is_published]
    out += [f"publication {pid}: requested/stalled -> failed (not resumed)" for pid in c.pending_publications]
    out += [
        f"moodle {m.platform_name} ({m.platform_id}) course {m.live_idnumber}: visible -> hidden"
        for m in c.moodle
        if m.visibility == "visible"
    ]
    return out


def _check_code(code: str) -> str:
    code = (code or "").strip().upper()
    if code not in FIXTURE_CODES:
        raise WithdrawRefusedError(
            f"{code or '(empty)'} is not a fixture course; only {', '.join(sorted(FIXTURE_CODES))} can be withdrawn",
            422,
        )
    return code


# ── The operation ───────────────────────────────────────────────────────────


def plan(db: Session, tenant_id: uuid.UUID, code: str) -> WithdrawReport:
    """What ``apply`` would change and what it keeps. Reads only; asks each Moodle the
    course was published to whether it is visible there (describe_course)."""
    code = _check_code(code)
    courses = [_course_report(db, c, ask_moodle=True) for c in fixture_courses(db, tenant_id, code)]
    changes = [line for c in courses for line in _would_change(c)]
    errors = [
        f"moodle {m.platform_name} ({m.platform_id}): {m.error}" for c in courses for m in c.moodle if m.error
    ]
    errors += [
        f"publication {pid} of course {c.id} is running now; apply refuses until it ends"
        for c in courses
        for pid in c.running_publications
    ]
    return WithdrawReport(
        code=code,
        tenant_id=tenant_id,
        dry_run=True,
        courses=courses,
        changes=changes,
        changed=False,
        errors=errors,
        preserved=_preserved(courses),
    )


def _audit(db: Session, actor_id: uuid.UUID | None, course: Course, system: str, changes: list[str]) -> None:
    db.add(
        AuditLog(
            user_id=actor_id,
            tenant_id=course.tenant_id,  # the tenant whose course changed sees the record
            action=AUDIT_ACTION,
            resource_type="course",
            resource_id=str(course.id),
            detail=json.dumps({"system": system, "changes": changes}, sort_keys=True),
        )
    )


def apply(db: Session, tenant_id: uuid.UUID, code: str, *, actor_id: uuid.UUID | None) -> WithdrawReport:
    """Withdraw the fixture course(s): TrueNorth first (committed, audited), then each
    Moodle (hidden, re-checked, audited). A Moodle that cannot be reached is an error in
    the report and nothing else: running again finishes the job."""
    code = _check_code(code)
    courses = fixture_courses(db, tenant_id, code)
    now = datetime.now(UTC)
    for course in courses:
        running = [p.id for p in _publications(db, course) if p.state in (*RUNNING, REQUESTED) and _lease_live(p, now)]
        if running:
            raise WithdrawRefusedError(
                f"publication {running[0]} of course {course.id} is running now and would show the course again;"
                " withdraw it after that job ends"
            )

    changes: list[str] = []
    for course in courses:
        tn: list[str] = []
        if course.is_published:
            course.is_published = False
            tn.append(f"course {course.id} {course.name!r}: published -> draft")
        for quiz in _quizzes(db, course):
            if quiz.is_published:
                quiz.is_published = False
                tn.append(f"quiz {quiz.id} {quiz.title!r}: published -> draft")
        for pub in _publications(db, course):
            if pub.state in (*RUNNING, REQUESTED):  # stalled or waiting: resume_stalled would re-show it
                tn.append(f"publication {pub.id}: {pub.state} -> {FAILED} (not resumed)")
                pub.state = FAILED
                pub.error = WITHDRAWN_ERROR
                pub.lease_until = pub.lease_holder = None
        if tn:
            _audit(db, actor_id, course, "truenorth", tn)
            changes += tn
    db.commit()

    errors: list[str] = []
    reported: set[uuid.UUID] = set()  # platforms an error above already names
    for course in courses:
        moodle_changes: list[str] = []
        for entry in _course_report(db, course, ask_moodle=False).moodle:
            platform = (
                db.query(ExternalPlatform)
                .filter(ExternalPlatform.id == entry.platform_id, ExternalPlatform.tenant_id == course.tenant_id)
                .one_or_none()
            )
            if platform is None:
                continue  # reported by plan(); nothing this tenant can hide
            label = f"moodle {platform.name} ({platform.id}) course {entry.live_idnumber}"
            before, err = _describe(db, platform, entry.live_idnumber)
            if before == "unknown":
                errors.append(f"{label}: {err}")
                reported.add(platform.id)
                continue
            if before != "visible":
                continue  # hidden or absent already: nothing to do
            try:
                publishing.backend_for(db, platform).set_visible(platform, entry.live_idnumber, False)
            except (MoodleError, ValueError) as exc:
                errors.append(f"{label}: hiding failed: {exc}")
                reported.add(platform.id)
                continue
            after, err = _describe(db, platform, entry.live_idnumber)
            if after != "hidden":
                errors.append(f"{label}: {after} after hiding {err}".rstrip())
                reported.add(platform.id)
                continue
            moodle_changes.append(f"{label}: visible -> hidden")
        if moodle_changes:
            _audit(db, actor_id, course, "moodle", moodle_changes)
            changes += moodle_changes
    db.commit()
    for line in changes:
        logger.warning("withdraw %s (tenant %s): %s", code, tenant_id, line)

    # Check both systems again: the report shows the state they are in now.
    reports = [_course_report(db, c, ask_moodle=True) for c in courses]
    for c in reports:
        if c.is_published or any(q.is_published for q in c.quizzes):
            errors.append(f"course {c.id} is still published in TrueNorth")
        errors += [f"publication {pid} of course {c.id} is running now" for pid in c.running_publications]
        errors += [
            f"moodle {m.platform_name} ({m.platform_id}) course {m.live_idnumber}: {m.visibility} {m.error}".rstrip()
            for m in c.moodle
            if m.visibility in ("visible", "unknown") and m.platform_id not in reported
        ]
    return WithdrawReport(
        code=code,
        tenant_id=tenant_id,
        dry_run=False,
        courses=reports,
        changes=changes,
        changed=bool(changes),
        errors=errors,
        preserved=_preserved(reports),
    )
