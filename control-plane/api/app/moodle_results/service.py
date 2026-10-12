"""Pull what students did in Moodle and record it on their TrueNorth learning records.

    pull(db, platform)   claim the Moodle's cursor, ask the backend for the next pages
                         (``pull_results``, signed both ways), record every row, persist
                         the cursor after each page, release the claim

A row is recorded only when everything it names is the platform's own business:

- the person is a TrueNorth user of the platform's tenant (Moodle names them by the
  TrueNorth id TrueNorth sign-in gave the account, never by email);
- the course has been published to this Moodle (a course_publications row), which is
  what ties a Moodle course to this tenant;
- the person is enrolled on that course in TrueNorth (Moodle does not create enrolments
  here, and a withdrawn enrolment is not reopened).

Anything else is counted under ``skipped`` and dropped. Recording is idempotent through
``moodle_result_records``: a row seen again changes nothing, a changed grade updates the
one quiz attempt it recorded. Module progress only moves forward (``completed`` is never
undone by a later, lower grade), and an enrolment completes when all its modules have.

Matching is on immutable ids only: platform, TrueNorth user id, course id, activity
idnumber; each fact also keeps the release that made its activity and Moodle's attempt id
(``moodle_result_records``). An email is never read: two tenants may share one.

    sync_status(db, platform)            how fresh this Moodle's results are ("current",
                                         "stale", "failing", "never")
    enrolment_results(db, ...)           one enrolment's Moodle facts, with their source
                                         timestamps and each platform's sync status
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import lti13
from ..course_publishing.models import PUBLISHED, SUPERSEDED, CoursePublication
from ..enrollment import complete_enrollment
from ..models import (
    CourseModule,
    Enrollment,
    EnrollmentStatus,
    ExternalPlatform,
    ModuleProgress,
    ModuleProgressStatus,
    Quiz,
    QuizAttempt,
    User,
)
from ..moodle_backends import BaseMoodleBackend, MoodleError, get_moodle_backend
from .models import MoodleResultCursor, MoodleResultRecord

logger = logging.getLogger(__name__)

LEASE = timedelta(minutes=10)
PAGE_SIZE = 500
MAX_PAGES = 20  # one pull reads at most 10 000 rows; the next run carries on from the cursor
COMPLETE_STATES = (1, 2)  # Moodle COMPLETION_COMPLETE, COMPLETION_COMPLETE_PASS
_MODULE = re.compile(r"^tn:([^:]+):")
_ORDINAL = re.compile(r"^mod_(\d+)$")


class PullBusyError(RuntimeError):
    """Another process is pulling this Moodle now."""


class LeaseLostError(RuntimeError):
    """This run's claim lapsed and another process took it; this run wrote nothing more."""


@dataclass
class Summary:
    platform_id: uuid.UUID
    cursor: str = ""
    pages: int = 0
    rows: int = 0
    applied: int = 0
    unchanged: int = 0
    skipped: Counter = field(default_factory=Counter)
    more: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform_id": self.platform_id,
            "cursor": self.cursor,
            "pages": self.pages,
            "rows": self.rows,
            "applied": self.applied,
            "unchanged": self.unchanged,
            "skipped": dict(self.skipped),
            "more": self.more,
        }


def backend_for(db: Session, platform: ExternalPlatform) -> BaseMoodleBackend:
    def key() -> tuple[str, str]:
        k = lti13.get_tool_key(db)
        return lti13.signing_pem(k), k.kid

    return get_moodle_backend(platform.platform_type, key_provider=key)


def cursor_row(db: Session, platform: ExternalPlatform) -> MoodleResultCursor:
    """The platform's cursor, created at the beginning ('') the first time."""
    # tenant-safe: keyed by the platform the caller already resolved in its tenant.
    row = db.get(MoodleResultCursor, platform.id)
    if row is not None:
        return row
    row = MoodleResultCursor(platform_id=platform.id, tenant_id=platform.tenant_id, cursor="", last_error="")
    db.add(row)
    try:
        db.commit()
    except IntegrityError:  # another process created it first
        db.rollback()
        # tenant-safe: the same platform's cursor, as above.
        row = db.get(MoodleResultCursor, platform.id)
    return row


def _claim(db: Session, platform: ExternalPlatform) -> str:
    cursor_row(db, platform)
    now = datetime.now(UTC)
    holder = uuid.uuid4().hex
    taken = db.execute(
        update(MoodleResultCursor)
        .where(
            MoodleResultCursor.platform_id == platform.id,
            or_(MoodleResultCursor.lease_until.is_(None), MoodleResultCursor.lease_until < now),
        )
        .values(lease_until=now + LEASE, lease_holder=holder, last_run_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    if taken != 1:
        raise PullBusyError("results from this Moodle are being pulled by another process; try again shortly")
    return holder


def _save(db: Session, platform_id: uuid.UUID, holder: str, **values: Any) -> None:
    """Commit this page's records with the cursor, only while this run holds the claim."""
    kept = db.execute(
        update(MoodleResultCursor)
        .where(MoodleResultCursor.platform_id == platform_id, MoodleResultCursor.lease_holder == holder)
        .values(**values)
        .execution_options(synchronize_session=False)
    ).rowcount
    if kept != 1:
        db.rollback()
        raise LeaseLostError(f"results pull of platform {platform_id}: another process holds the claim now")
    db.commit()


def pull(
    db: Session,
    platform: ExternalPlatform,
    *,
    backend: BaseMoodleBackend | None = None,
    reset: bool = False,
    max_pages: int | None = None,
    page_size: int | None = None,
) -> Summary:
    """Pull and record. ``reset`` starts again from the beginning (records are idempotent).

    Raises PullBusyError, MoodleError (recorded as the cursor's ``last_error``; the cursor
    stays where it was, so nothing from a refused answer is recorded) or LeaseLostError."""
    holder = _claim(db, platform)
    summary = Summary(platform_id=platform.id)
    try:
        backend = backend or backend_for(db, platform)
        # tenant-safe: the cursor of the platform this run claimed.
        cursor = "" if reset else (db.get(MoodleResultCursor, platform.id).cursor or "")
        ctx = _Context(db, platform)
        for _ in range(max_pages or MAX_PAGES):
            page = backend.pull_results(platform, cursor, page_size or PAGE_SIZE)
            rows = page.get("rows") or []
            applied = 0
            for row in rows:
                outcome = _apply(ctx, row)
                if outcome == "applied":
                    applied += 1
                elif outcome == "unchanged":
                    summary.unchanged += 1
                else:
                    summary.skipped[outcome] += 1
            ctx.finish()
            cursor = str(page.get("cursor") or cursor)
            summary.pages += 1
            summary.rows += len(rows)
            summary.applied += applied
            summary.more = bool(page.get("more"))
            _save(
                db,
                platform.id,
                holder,
                cursor=cursor[:64],
                rows_seen=MoodleResultCursor.rows_seen + len(rows),
                rows_applied=MoodleResultCursor.rows_applied + applied,
                lease_until=datetime.now(UTC) + LEASE,
            )
            if not summary.more or not rows:
                break
    except MoodleError as exc:
        # Raised by the backend before any row of its page is applied; earlier pages are
        # committed with their cursor, so there is nothing to undo here.
        _save(db, platform.id, holder, last_error=str(exc)[:2000], lease_until=None, lease_holder=None)
        logger.warning("results pull from platform %s refused: %s", platform.id, exc)
        raise
    except LeaseLostError:
        raise
    except Exception:
        db.rollback()
        _save(db, platform.id, holder, last_error="the pull crashed; see the API log", lease_until=None, lease_holder=None)
        raise
    summary.cursor = cursor
    _save(db, platform.id, holder, last_error="", last_success_at=datetime.now(UTC), lease_until=None, lease_holder=None)
    if summary.rows:
        logger.info(
            "results pull from platform %s: %d rows, %d recorded, %d unchanged, skipped %s",
            platform.id, summary.rows, summary.applied, summary.unchanged, dict(summary.skipped),
        )
    return summary


# -- recording -------------------------------------------------------------------------
class _Context:
    """Per-pull caches: which courses this Moodle may report on, and their module maps."""

    def __init__(self, db: Session, platform: ExternalPlatform):
        self.db = db
        self.platform = platform
        self._courses: dict[uuid.UUID, dict[str, Any] | None] = {}
        self._touched: set[uuid.UUID] = set()  # enrollments whose roll-up may change

    def course(self, course_id: uuid.UUID) -> dict[str, Any] | None:
        """{"ordinals": {module id: ordinal}, "activities": {module id: [idnumber, ...]}} for a
        course published to this Moodle, else None (the Moodle may not report on it)."""
        if course_id in self._courses:
            return self._courses[course_id]
        # tenant-safe: course_publications rows of this platform only; request() created
        # them after checking the platform and the release are the same tenant's.
        pubs = (
            self.db.query(CoursePublication)
            .filter(
                CoursePublication.course_id == course_id,
                CoursePublication.platform_id == self.platform.id,
                CoursePublication.state.in_((PUBLISHED, SUPERSEDED)),
            )
            .order_by(CoursePublication.published_at.desc().nulls_last())
            .all()
        )
        live = next((p for p in pubs if p.state == PUBLISHED), None)
        info = None
        if pubs:
            receipt = json.loads((live or pubs[0]).receipt or "{}")
            activities: dict[str, list[str]] = {}
            for idnumber in receipt.get("activities") or {}:
                if m := _MODULE.match(idnumber):
                    activities.setdefault(m.group(1), []).append(idnumber)
            # Which release made each activity: the newest publication whose receipt has it
            # (a page kept across releases belongs to the latest; a quiz is per question set).
            releases: dict[str, uuid.UUID] = {}
            for pub in pubs:
                for idnumber in json.loads(pub.receipt or "{}").get("activities") or {}:
                    releases.setdefault(idnumber, pub.release_id)
            info = {"ordinals": _ordinals(self.db, live or pubs[0]), "activities": activities, "releases": releases}
        self._courses[course_id] = info
        return info

    def touch(self, enrollment: Enrollment) -> None:
        self._touched.add(enrollment.id)

    def finish(self) -> None:
        """Roll touched enrolments up: complete once every module is complete."""
        for enrollment_id in self._touched:
            enrollment = self.db.get(Enrollment, enrollment_id)  # tenant-safe: touched by _apply
            if enrollment is None or enrollment.status in (EnrollmentStatus.completed, EnrollmentStatus.withdrawn):
                continue
            modules = self.db.query(CourseModule.id).filter(CourseModule.course_id == enrollment.course_id).all()
            done = {
                p.module_id
                for p in self.db.query(ModuleProgress).filter(ModuleProgress.enrollment_id == enrollment.id)
                if p.status == ModuleProgressStatus.completed
            }
            if modules and all(m.id in done for m in modules):
                complete_enrollment(self.db, enrollment)
        self._touched.clear()
        self.db.flush()


def _ordinals(db: Session, pub: CoursePublication) -> dict[str, int]:
    """Release module id -> module ordinal, from the published release's own bundle."""
    from ..course_publishing.payload import _modules
    from ..course_releases.models import CourseRelease
    from ..course_releases.service import load_bundle

    release = db.get(CourseRelease, pub.release_id)  # tenant-safe: the publication's own release
    try:
        return {m["id"]: int(m["ordinal"]) for m in _modules(load_bundle(db, release))} if release else {}
    except Exception:  # noqa: BLE001 — fall back to the mod_NNN naming every release uses
        logger.warning("publication %s: release modules unreadable; using mod_NNN ordinals", pub.id)
        return {}


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _int(value: Any) -> int | None:
    """A positive integer id, else None (an older plugin reports no attempt id)."""
    try:
        n = int(value)
    except (ValueError, TypeError):
        return None
    return n if n > 0 else None


def _when(row: dict[str, Any]) -> datetime:
    try:
        return datetime.fromtimestamp(int(row.get("time") or 0), UTC)
    except (ValueError, OverflowError, OSError):
        return datetime.now(UTC)


def _apply(ctx: _Context, row: dict[str, Any]) -> str:
    """Record one row: "applied", "unchanged", or the reason it was skipped."""
    db, platform = ctx.db, ctx.platform
    kind = row.get("kind")
    if kind not in ("completion", "quiz_grade"):
        return "unknown_kind"
    user_id, course_id = _uuid(row.get("user")), _uuid(row.get("course"))
    if user_id is None or course_id is None:
        return "malformed"
    # tenant-safe: any id Moodle names is looked up, then refused unless it is this tenant's.
    user = db.get(User, user_id)
    if user is None or user.tenant_id != platform.tenant_id or user.deleted_at is not None:
        return "not_this_tenants_user"
    info = ctx.course(course_id)
    if info is None:
        return "course_not_published_here"
    enrollment = (
        db.query(Enrollment).filter(Enrollment.user_id == user_id, Enrollment.course_id == course_id).first()
    )
    if enrollment is None:
        return "not_enrolled"
    if enrollment.status == EnrollmentStatus.withdrawn:
        return "withdrawn"

    activity = str(row.get("activity") or "")
    # Activity idnumbers (tn:mod_NNN:...) repeat in every course: a fact is per course.
    ref = f"{kind}:{activity}"[:200]
    content = {k: row.get(k) for k in ("state", "grade", "grademax", "gradepass", "attempts", "attempt")}
    fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()
    record = (
        db.query(MoodleResultRecord)
        .filter(
            MoodleResultRecord.platform_id == platform.id,
            MoodleResultRecord.user_id == user_id,
            MoodleResultRecord.course_id == course_id,
            MoodleResultRecord.ref == ref,
        )
        .first()
    )
    if record is not None and record.fingerprint == fingerprint:
        return "unchanged"

    module = _module(db, info, course_id, activity)
    if module is None:
        return "unknown_activity"
    if record is None:
        record = MoodleResultRecord(platform_id=platform.id, user_id=user_id, course_id=course_id, ref=ref)
        db.add(record)
    record.fingerprint = fingerprint
    record.source_time = int(row.get("time") or 0)
    record.release_id = info.get("releases", {}).get(activity, record.release_id)
    if kind == "quiz_grade":
        record.source_attempt_id = _int(row.get("attempt")) or record.source_attempt_id
    when = _when(row)

    progress = _progress(db, enrollment, module)
    seen = progress.last_accessed_at
    if seen is None or (seen if seen.tzinfo else seen.replace(tzinfo=UTC)) < when:
        progress.last_accessed_at = when
    if kind == "quiz_grade":
        record.grade, record.grade_max = float(row.get("grade") or 0), float(row.get("grademax") or 0)
        _record_quiz(db, record, row, module, user, when)
        pct = round(100 * record.grade / record.grade_max) if record.grade_max else 0
        gradepass = float(row.get("gradepass") or 0)
        passed = record.grade >= gradepass if gradepass > 0 else pct >= module.pass_threshold
        progress.score = max(progress.score or 0, pct)
        progress.max_score = 100
        progress.attempts = max(progress.attempts or 0, int(row.get("attempts") or 0))
        if passed and pct >= module.pass_threshold:
            _complete(progress, when)
    else:
        record.state = int(row.get("state") or 0)
        db.flush()
        if _all_activities_complete(db, platform.id, user_id, course_id, info, activity):
            if not progress.score and not _graded(db, platform.id, user_id, course_id, activity):
                # Completed with no grade (a reading module): not scored, rather than
                # 0/100, which would read as a fail (qsp_progress) and drag the course grade.
                progress.score, progress.max_score = 0, 0
            _complete(progress, when)
    if progress.status == ModuleProgressStatus.not_started:
        progress.status = ModuleProgressStatus.in_progress
    if enrollment.status == EnrollmentStatus.enrolled:
        enrollment.status = EnrollmentStatus.in_progress
        enrollment.started_at = enrollment.started_at or when
    ctx.touch(enrollment)
    db.flush()
    return "applied"


def _module(db: Session, info: dict[str, Any], course_id: uuid.UUID, activity: str) -> CourseModule | None:
    m = _MODULE.match(activity)
    if not m:
        return None
    ordinal = info["ordinals"].get(m.group(1))
    if ordinal is None and (n := _ORDINAL.match(m.group(1))):
        ordinal = int(n.group(1))
    if ordinal is None:
        return None
    return db.query(CourseModule).filter(CourseModule.course_id == course_id, CourseModule.ordinal == ordinal).first()


def _progress(db: Session, enrollment: Enrollment, module: CourseModule) -> ModuleProgress:
    progress = (
        db.query(ModuleProgress)
        .filter(ModuleProgress.enrollment_id == enrollment.id, ModuleProgress.module_id == module.id)
        .first()
    )
    if progress is None:
        progress = ModuleProgress(
            enrollment_id=enrollment.id,
            module_id=module.id,
            status=ModuleProgressStatus.not_started,
            score=0,
            max_score=100,
            attempts=0,
        )
        db.add(progress)
    return progress


def _complete(progress: ModuleProgress, when: datetime) -> None:
    if progress.status != ModuleProgressStatus.completed:
        progress.status = ModuleProgressStatus.completed
        progress.completed_at = when


def _all_activities_complete(
    db: Session, platform_id: uuid.UUID, user_id: uuid.UUID, course_id: uuid.UUID, info: dict[str, Any], activity: str
) -> bool:
    """Every TrueNorth activity Moodle holds for this module of this course is complete."""
    m = _MODULE.match(activity)
    wanted = info["activities"].get(m.group(1) if m else "", [])
    if not wanted:
        return False
    states = dict(
        db.query(MoodleResultRecord.ref, MoodleResultRecord.state).filter(
            MoodleResultRecord.platform_id == platform_id,
            MoodleResultRecord.user_id == user_id,
            MoodleResultRecord.course_id == course_id,
            MoodleResultRecord.ref.in_([f"completion:{a}" for a in wanted]),
        )
    )
    return all(states.get(f"completion:{a}") in COMPLETE_STATES for a in wanted)


def _graded(db: Session, platform_id: uuid.UUID, user_id: uuid.UUID, course_id: uuid.UUID, activity: str) -> bool:
    """Whether Moodle has reported a grade in this module of this course for this person."""
    m = _MODULE.match(activity)
    return (
        db.query(MoodleResultRecord.id)
        .filter(
            MoodleResultRecord.platform_id == platform_id,
            MoodleResultRecord.user_id == user_id,
            MoodleResultRecord.course_id == course_id,
            MoodleResultRecord.ref.like(f"quiz_grade:tn:{m.group(1) if m else ''}:%"),
        )
        .first()
        is not None
    )


def mirrored_attempt_ids():
    """A subquery of the quiz attempts that mirror a Moodle grade (they are not attempts
    taken in TrueNorth, so they do not count against a quiz's max_attempts)."""
    return select(MoodleResultRecord.quiz_attempt_id).where(MoodleResultRecord.quiz_attempt_id.isnot(None))


def _record_quiz(
    db: Session, record: MoodleResultRecord, row: dict[str, Any], module: CourseModule, user: User, when: datetime
) -> None:
    """Mirror Moodle's grade for this quiz as one TrueNorth attempt on the module's quiz."""
    # tenant-safe: the attempt this ledger row created for this (tenant-checked) user.
    attempt = db.get(QuizAttempt, record.quiz_attempt_id) if record.quiz_attempt_id else None
    if attempt is None:
        quiz = (
            db.query(Quiz)
            .filter(Quiz.module_id == module.id, Quiz.deleted_at.is_(None))
            .order_by(Quiz.created_at.desc())
            .first()
        )
        if quiz is None:
            return  # the module has no TrueNorth quiz: progress still records the grade
        attempt = QuizAttempt(quiz_id=quiz.id, user_id=user.id, tenant_id=user.tenant_id, answers="{}", started_at=when)
        db.add(attempt)
        db.flush()
        record.quiz_attempt_id = attempt.id
    grademax = float(row.get("grademax") or 0)
    grade = float(row.get("grade") or 0)
    gradepass = float(row.get("gradepass") or 0)
    pct = 100 * grade / grademax if grademax else 0
    attempt.score = round(grade)
    attempt.max_score = round(grademax) or 100
    attempt.passed = grade >= gradepass if gradepass > 0 else pct >= module.pass_threshold
    attempt.submitted_at = when


# -- reading back ----------------------------------------------------------------------
NEVER, CURRENT, STALE, FAILING = "never", "current", "stale", "failing"


def _aware(when: datetime | None) -> datetime | None:
    return when if when is None or when.tzinfo else when.replace(tzinfo=UTC)


def sync_status(db: Session, platform: ExternalPlatform, *, now: datetime | None = None) -> dict[str, Any]:
    """How fresh what TrueNorth holds from this Moodle is.

    ``state``: "never" (no pull has succeeded yet), "failing" (the latest pull failed:
    Moodle down, or its answer refused; what TrueNorth shows may be behind Moodle), "stale"
    (the last success is older than two scheduled intervals, ``MOODLE_RESULTS_PULL_SECONDS``;
    with the schedule off only a failure makes it stale) or "current". ``stale`` is true
    for every state but "current". ``last_error`` is the operator's detail."""
    from .runner import interval

    now = now or datetime.now(UTC)
    every = interval()
    row = db.get(MoodleResultCursor, platform.id)  # tenant-safe: the caller resolved the platform
    success = _aware(row.last_success_at) if row else None
    lease = _aware(row.lease_until) if row else None
    if row is not None and row.last_error:
        state = FAILING
    elif success is None:
        state = NEVER
    elif every > 0 and now - success > timedelta(seconds=2 * every):
        state = STALE
    else:
        state = CURRENT
    return {
        "platform_id": platform.id,
        "platform_name": platform.name,
        "state": state,
        "stale": state != CURRENT,
        "last_success_at": success,
        "last_run_at": _aware(row.last_run_at) if row else None,
        "last_error": (row.last_error if row else "") or "",
        "sync_interval_seconds": int(every) if every > 0 else 0,
        "running": lease is not None and lease > now,
    }


def enrolment_results(
    db: Session, tenant_id: uuid.UUID, user_id: uuid.UUID, course_id: uuid.UUID, *, with_errors: bool
) -> dict[str, Any]:
    """One person's Moodle facts on one course, from every Moodle of ``tenant_id`` the course
    is published to, each with Moodle's own timestamp, and those Moodles' sync status.

    The caller has already checked the person and the course (``authorize_record_access``,
    ``get_owned_or_global``). ``with_errors`` keeps ``last_error`` (staff only)."""
    # tenant-safe: platforms of the caller's tenant only; publications tie them to the course.
    platforms = (
        db.query(ExternalPlatform)
        .join(CoursePublication, CoursePublication.platform_id == ExternalPlatform.id)
        .filter(
            ExternalPlatform.tenant_id == tenant_id,
            CoursePublication.course_id == course_id,
            CoursePublication.state.in_((PUBLISHED, SUPERSEDED)),
        )
        .distinct()
        .order_by(ExternalPlatform.name)
        .all()
    )
    ids = [p.id for p in platforms]
    records = (
        db.query(MoodleResultRecord)
        .filter(
            MoodleResultRecord.platform_id.in_(ids),
            MoodleResultRecord.user_id == user_id,
            MoodleResultRecord.course_id == course_id,
        )
        .order_by(MoodleResultRecord.ref)
        .all()
        if ids
        else []
    )
    # tenant-safe: the subject was checked by the caller (authorize_record_access).
    enrollment = db.query(Enrollment).filter(Enrollment.user_id == user_id, Enrollment.course_id == course_id).first()
    sync = []
    for p in platforms:
        status = sync_status(db, p)
        if not with_errors:
            status["last_error"] = ""
        sync.append(status)
    results = []
    for r in records:
        kind, _, activity = r.ref.partition(":")
        graded = kind == "quiz_grade"
        results.append(
            {
                "platform_id": r.platform_id,
                "kind": kind,
                "activity": activity,
                "release_id": r.release_id,
                "grade": r.grade if graded else None,
                "grade_max": r.grade_max if graded else None,
                "completion_state": None if graded else r.state,
                "complete": None if graded else r.state in COMPLETE_STATES,
                "source_time": datetime.fromtimestamp(r.source_time, UTC) if r.source_time else None,
                "source_attempt_id": r.source_attempt_id,
                "quiz_attempt_id": r.quiz_attempt_id,
                "recorded_at": _aware(r.updated_at or r.created_at),
            }
        )
    return {
        "course_id": course_id,
        "user_id": user_id,
        "enrollment_id": enrollment.id if enrollment else None,
        "enrollment_status": enrollment.status.value if enrollment else None,
        "sync": sync,
        "results": results,
    }
