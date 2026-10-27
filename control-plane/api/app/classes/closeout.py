"""Close-out: results, closing, reopening and the export (ADR 0014 §13; issue #283).

**Pre-fill.** For each Student on the roll call (and each who lost their seat) the lead sees:

- *attendance*: sessions held, present, excused and absent (``attendance.py``);
- *the course*: their enrolment's module progress, which is where quiz results land
  (``routers/quizzes.py`` writes them into ``module_progress``), over every graded module of
  the course (required quiz, scenario or LTI modules), one not attempted counting as 0.
  Passed when every graded module reached its own ``pass_threshold`` (``_course``);
- *the exercises*: detections credited to them in the exercises of the class's sessions
  (ADR 0005: Students earn exercise credit only by submitting detections);
- a *suggested* outcome: ``incomplete`` when no session was held, below ``ATTENDANCE_MIN``
  of the sessions held (present or excused), or when the course has graded modules and the
  Student has no graded result yet; else ``fail`` when the graded work fell short; else
  ``pass``. The lead decides; the suggestion is kept beside the decision.

**Unclassified (DLP).** On an Unclassified deployment (``settings.classification_level``,
and whenever it cannot be read) the pre-fill carries pass/fail only: no percentage, no
score, no detection count. ``class_results`` has no numeric column at all, and a close-out
writes no score or letter grade to the enrolment, not even transiently (``apply_result``
sets the status itself instead of calling ``complete_enrollment``). Free-text comments and
notes stay allowed: an owner-approved exception (ADR 0014 decision 13).

**Closing.** The lead or an admin sets pass, fail or incomplete for every Student on the
roll call, each with an optional comment. Refused (409) while a session is provisioning or
under way: its ranges would be torn down under the Students and it would be missing from the
record. In one transaction, under a lock on the class row:

1. attendance is recorded for any ended session the clock has not reached;
2. each result is stored and goes to the Student's enrolment on the class's course through
   the existing completion path (``complete_enrollment``): ``pass`` completes it, ``fail``
   marks it failed, ``incomplete`` leaves it open. An enrolment completed some other way is
   never failed, reopened or re-graded; a later close-out undoes only what this class did
   (``class_results.enrolment_change``). Qualifications follow, since the QSP spine reads
   enrolment and module progress (``qsp_progress``);
3. the class becomes ``closed``: read-only. Its sessions still to come are cancelled, and its
   ranges are torn down by the clock as for any class no longer open (``ranges._why_done``:
   ``class_closed``). A closed class's views and export read what was recorded, never a
   fresh judgement;
4. every step is audited.

**Reopening** needs an admin, with a reason, and is audited. Results stay as they were
until the next close-out replaces them; enrolments are not touched. Cancelled sessions and
torn-down ranges are not brought back: the lead books again.

**Export.** CSV, one row per Student, each session's attendance and the result, with every
cell that a spreadsheet would run as a formula defused (``csv_cell``). Never a numeric grade.
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..enrollment import complete_enrollment
from ..models import (
    Course,
    CourseModule,
    Enrollment,
    EnrollmentStatus,
    ModuleContentType,
    ModuleProgress,
    ModuleProgressStatus,
    User,
    UserRole,
)
from ..scheduler import service as scheduler
from ..scheduler.models import EventState, ScheduledEvent
from . import attendance, service, sessions
from .models import (
    AttendanceStatus,
    Class,
    ClassCloseout,
    ClassResult,
    ClassStatus,
    ResultOutcome,
    RollCallEntry,
)

# Module content that carries a score (ModuleProgress.score / max_score); readings and videos
# are completed, not graded.
GRADED_TYPES = (ModuleContentType.quiz, ModuleContentType.scenario, ModuleContentType.external_lti)
ATTENDANCE_MIN = 0.8  # of the sessions held, present or excused, for a pass to be suggested

# A spreadsheet runs a cell that starts with one of these as a formula (CSV injection).
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def numbers_allowed() -> bool:
    """Whether numeric results may be shown here: never on an Unclassified deployment, and
    never when the classification cannot be read (fail closed)."""
    from ..settings import UnsafeSettingsError, classification_level

    try:
        return classification_level() != "unclassified"
    except UnsafeSettingsError:
        return False


def csv_cell(value: object) -> str:
    """``value`` as a CSV cell a spreadsheet will not run as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip(" ").startswith(_FORMULA_START) else text


# ── Pre-fill ─────────────────────────────────────────────────────────────────


@dataclass
class Standing:
    """One Student's pre-fill. ``percent`` and ``detections`` are None where numbers are not
    allowed (Unclassified)."""

    entry: RollCallEntry
    display_name: str | None
    held: int = 0
    present: int = 0
    excused: int = 0
    absent: int = 0
    course_passed: bool | None = None
    percent: float | None = None
    detections: int | None = None
    suggested: str = ResultOutcome.incomplete.value
    result: ClassResult | None = None
    per_session: dict[uuid.UUID, str | None] = field(default_factory=dict)


def students(db: Session, cls: Class) -> list[RollCallEntry]:
    """Who gets a result: everyone on the roll call now, and everyone who lost their seat
    (forfeited). A forfeited Student was expected and did not come: the class's record, and
    the unit's readiness page, must say so (as absent, suggested incomplete) rather than leave
    them out. Removed entries (taken off on purpose) and additions still waiting for an
    approver were never on it, and get none."""
    return [e for e in attendance.roll_call(db, cls) if e.status not in attendance.NOT_EXPECTED]


@dataclass(frozen=True)
class CourseWork:
    """One Student's graded course work. ``state``: ``none_graded`` (the course has no
    graded module), ``no_work`` (it has, and they have no graded result yet), ``passed`` or
    ``failed``. ``percent``: over every graded module, unattempted ones as 0."""

    state: str
    percent: float | None = None


NONE_GRADED, NO_WORK, PASSED, FAILED = "none_graded", "no_work", "passed", "failed"


def _course(db: Session, cls: Class, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, CourseWork]:
    """Each Student's standing on the class's course.

    The graded modules are the course's required modules whose content is scored (a quiz, a
    scenario or an external LTI tool), less any the Student completed with nothing to grade
    (``max_score`` 0: completion was its verdict, as in ``qsp_progress``). Every one of them
    counts: one not attempted is 0 of 100. Passed means every graded module reached its own
    ``pass_threshold``, as the QSP spine judges a module."""
    modules = (
        db.query(CourseModule)
        .filter(CourseModule.course_id == cls.course_id, CourseModule.is_required.is_(True),
                CourseModule.content_type.in_(GRADED_TYPES))
        .all()
    )
    if not user_ids:
        return {}
    if not modules:
        return {uid: CourseWork(NONE_GRADED) for uid in user_ids}
    enrolments = {
        e.id: e.user_id for e in db.query(Enrollment).filter(
            Enrollment.course_id == cls.course_id, Enrollment.user_id.in_(user_ids),
            Enrollment.tenant_id == cls.tenant_id)
    }
    progress: dict[tuple[uuid.UUID, uuid.UUID], ModuleProgress] = {}
    if enrolments:
        for p in db.query(ModuleProgress).filter(
                ModuleProgress.enrollment_id.in_(enrolments), ModuleProgress.module_id.in_([m.id for m in modules])):
            progress[(enrolments[p.enrollment_id], p.module_id)] = p
    out: dict[uuid.UUID, CourseWork] = {}
    for uid in user_ids:
        score = top = 0
        graded = attempted = passed = 0
        for m in modules:
            p = progress.get((uid, m.id))
            done = p is not None and p.status == ModuleProgressStatus.completed
            if done and not p.max_score:
                continue  # nothing to grade: completion is its verdict
            # Attempted: a graded try is on record. A failed quiz attempt leaves the module
            # in_progress with its best score (routers/quizzes.py), and that score counts.
            tried = p is not None and bool(p.max_score) and (
                (p.attempts or 0) > 0
                or p.status in (ModuleProgressStatus.in_progress, ModuleProgressStatus.completed))
            got, of = (p.score or 0, p.max_score) if tried else (0, (p.max_score if p and p.max_score else 100))
            graded += 1
            attempted += 1 if tried else 0
            score += got
            top += of
            threshold = m.pass_threshold if m.pass_threshold is not None else 70
            passed += 1 if done and got >= of * threshold / 100 else 0
        if not graded:
            out[uid] = CourseWork(NONE_GRADED)
        elif not attempted:
            out[uid] = CourseWork(NO_WORK, 0.0)
        else:
            out[uid] = CourseWork(PASSED if passed == graded else FAILED, 100 * score / top if top else None)
    return out


def _detections(db: Session, cls: Class, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Detections credited to each Student in the exercises of the class's sessions."""
    from sqlalchemy import func

    from ..detections.models import ACHIEVED, DetectionSubmission

    exercise_ids = {evt.exercise_id for _, evt in attendance.held_sessions(db, cls) if evt.exercise_id}
    if not exercise_ids or not user_ids:
        return {}
    rows = (
        db.query(DetectionSubmission.user_id, func.count(DetectionSubmission.id))
        .filter(
            DetectionSubmission.exercise_id.in_(exercise_ids),
            DetectionSubmission.tenant_id == cls.tenant_id,
            DetectionSubmission.user_id.in_(user_ids),
            DetectionSubmission.verdict == ACHIEVED,
        )
        .group_by(DetectionSubmission.user_id)
        .all()
    )
    return dict(rows)


def suggest(held: int, present: int, excused: int, work: str) -> str:
    """Incomplete with no session held, below ``ATTENDANCE_MIN``, or with graded course work
    not done yet; fail when it was done and fell short; else pass. Only a suggestion."""
    if not held or (present + excused) / held < ATTENDANCE_MIN or work == NO_WORK:
        return ResultOutcome.incomplete.value
    if work == FAILED:
        return ResultOutcome.failed.value
    return ResultOutcome.passed.value


def prefill(db: Session, cls: Class, now: datetime | None = None) -> tuple[list, list[Standing]]:
    """(the sessions held so far, each Student's standing). Reads only: a session the clock
    has not recorded yet is judged from the evidence as the clock would judge it."""
    now = now or _now()
    held = attendance.held_sessions(db, cls, ended_by=now)
    entries = students(db, cls)
    recorded = attendance.records(db, cls)
    # A closed class shows what was recorded when it closed, never a fresh judgement.
    computed = {} if cls.status != ClassStatus.open.value else {
        cs.id: attendance.evaluate(db, cls, evt, attendance.expected(entries, evt)) for cs, evt in held
        if any((cs.id, e.id) not in recorded for e in entries)}
    uids = {e.user_id for e in entries if e.user_id}
    names = dict(db.query(User.id, User.display_name).filter(User.id.in_(uids), User.tenant_id == cls.tenant_id)) \
        if uids else {}
    course = _course(db, cls, uids)
    graded_course = db.query(CourseModule.id).filter(
        CourseModule.course_id == cls.course_id, CourseModule.is_required.is_(True),
        CourseModule.content_type.in_(GRADED_TYPES)).first() is not None
    course_default = CourseWork(NO_WORK, 0.0) if graded_course else CourseWork(NONE_GRADED)
    dets = _detections(db, cls, uids)
    results = {r.entry_id: r for r in db.query(ClassResult).filter(
        ClassResult.class_id == cls.id, ClassResult.tenant_id == cls.tenant_id)}
    numbers = numbers_allowed()
    out = []
    for e in entries:
        st = Standing(entry=e, display_name=names.get(e.user_id) if e.user_id else None, result=results.get(e.id))
        for cs, _evt in held:
            row = recorded.get((cs.id, e.id))
            status = row.status if row else (computed.get(cs.id, {}).get(e.id, (None, None))[0])
            st.per_session[cs.id] = status
            if status is None:
                continue  # joined after this session
            st.held += 1
            if status == AttendanceStatus.present.value:
                st.present += 1
            elif status == AttendanceStatus.excused.value:
                st.excused += 1
            else:
                st.absent += 1
        work = course.get(e.user_id) if e.user_id else None
        if work is None:  # no account: no course work at all
            work = course_default
        st.course_passed = {PASSED: True, FAILED: False}.get(work.state)
        if numbers:
            st.percent = None if work.percent is None else round(work.percent, 1)
            st.detections = dets.get(e.user_id, 0) if e.user_id else 0
        st.suggested = suggest(st.held, st.present, st.excused, work.state)
        out.append(st)
    return held, out


# ── Closing ──────────────────────────────────────────────────────────────────


@dataclass
class Decision:
    entry_id: uuid.UUID
    outcome: str
    comment: str | None = None


def apply_result(db: Session, cls: Class, entry: RollCallEntry, outcome: str, made: str | None,
                 now: datetime) -> tuple[str | None, str]:
    """Take one result to the Student's enrolment on the class's course, through the
    existing completion path. ``made``: what this class's previous close-out did to it
    (``completed``, ``failed`` or None). Returns (what this close-out did, for the next one;
    a line for the audit). Does not commit.

    A close-out only ever undoes its own work: an enrolment completed some other way
    (self-paced, Moodle, an earlier course run) is never failed, reopened or re-graded."""
    if entry.user_id is None:
        return None, "no account: kept on the class only"
    enrolment = (
        db.query(Enrollment)
        .filter(Enrollment.user_id == entry.user_id, Enrollment.course_id == cls.course_id,
                Enrollment.tenant_id == cls.tenant_id)
        .first()
    )
    if enrolment is None:
        return None, "not enrolled: kept on the class only"
    was = enrolment.status.value if enrolment.status else "none"
    # This class's own work, still as it left it. Anything else (completed or failed some
    # other way, or changed since) is never undone or re-graded by a close-out.
    ours = (made, enrolment.status) in (("completed", EnrollmentStatus.completed),
                                        ("failed", EnrollmentStatus.failed))
    if outcome == ResultOutcome.passed.value:
        if enrolment.status == EnrollmentStatus.completed:
            if ours:
                return "completed", f"enrolment already completed by this class: kept (was {was})"
            return None, f"enrolment completed elsewhere: kept (was {was})"
        if numbers_allowed():
            complete_enrollment(db, enrolment)
        else:
            # Unclassified: pass/fail only. complete_enrollment computes and stores a score and
            # a letter grade, so it is not called: no number reaches the database at all.
            enrolment.status = EnrollmentStatus.completed
            enrolment.completed_at = now
            _clear_grade(enrolment)
        db.flush()
        return "completed", f"enrolment completed (was {was})"
    if outcome == ResultOutcome.failed.value:
        if enrolment.status == EnrollmentStatus.failed:
            if ours:
                return "failed", f"enrolment already failed by this class: kept (was {was})"
            return None, f"enrolment failed elsewhere: kept (was {was})"
        if enrolment.status == EnrollmentStatus.completed and not ours:
            return None, f"enrolment completed elsewhere: kept (was {was})"
        if ours:  # this class's own completion: its grade goes with it
            _clear_grade(enrolment)
        enrolment.status = EnrollmentStatus.failed
        enrolment.completed_at = now
        db.flush()
        return "failed", f"enrolment failed (was {was})"
    # Incomplete leaves it open; this class's own completion or failure is undone.
    if ours:
        enrolment.status = EnrollmentStatus.in_progress
        enrolment.completed_at = None
        _clear_grade(enrolment)
        db.flush()
        return None, f"enrolment reopened (was {was})"
    return None, f"enrolment left as it was ({was})"


def _clear_grade(enrolment: Enrollment) -> None:
    enrolment.final_grade = None
    enrolment.final_score = 0
    enrolment.max_score = 0


def close(db: Session, cls: Class, decisions: list[Decision], actor, now: datetime | None = None) -> list:
    """Close the class with a result for every Student on its roll call. Returns the CANCEL
    invites of the sessions it cancelled, to send after commit. Does not commit."""
    now = now or _now()
    # One close at a time: the class row is locked (PostgreSQL) and its status read again.
    db.query(Class.id).filter(Class.id == cls.id, Class.tenant_id == cls.tenant_id).with_for_update().first()
    db.refresh(cls)
    if cls.status != ClassStatus.open.value:
        raise HTTPException(409, f"The class is {cls.status}; only an open class is closed")
    # Its sessions are locked too, before they are checked and cancelled: the scheduler's
    # clock or an approver cannot move one on in between.
    session_ids = sessions.session_ids(db, cls)
    if session_ids:
        db.query(ScheduledEvent.id).filter(
            ScheduledEvent.id.in_(session_ids), ScheduledEvent.tenant_id == cls.tenant_id
        ).order_by(ScheduledEvent.id).with_for_update().all()
    # Not while a session runs, or its ranges are being built for it: closing would tear them
    # down under the Students and leave the session out of the record.
    running = [evt.name for _, evt in attendance.held_sessions(db, cls)
               if evt.state in (EventState.provisioning, EventState.active)
               or (evt.state == EventState.scheduled and _aware(evt.start_time) <= now < _aware(evt.end_time))]
    if running:
        raise HTTPException(409, f"A session is under way or about to start ({', '.join(running)}); "
                                 "close the class after it ends")
    entries = {e.id: e for e in students(db, cls)}
    given = {d.entry_id: d for d in decisions}
    if len(given) != len(decisions):
        raise HTTPException(422, "A Student has more than one result")
    unknown = [str(i) for i in given if i not in entries]
    if unknown:
        raise HTTPException(422, f"Not on this class's roll call: {', '.join(unknown)}")
    missing = [e.email for i, e in entries.items() if i not in given]
    if missing:
        raise HTTPException(422, f"Set a result for every Student; missing: {', '.join(missing)}")
    attendance.record_class(db, cls, now)
    _, standing = prefill(db, cls, now)
    by_entry = {s.entry.id: s for s in standing}
    actor_id = uuid.UUID(str(actor.id))
    for entry_id, d in given.items():
        entry, st = entries[entry_id], by_entry[entry_id]
        row = st.result
        made = row.enrolment_change if row else None
        if row is None:
            row = ClassResult(tenant_id=cls.tenant_id, class_id=cls.id, entry_id=entry.id)
            db.add(row)
        row.user_id = entry.user_id
        row.outcome = d.outcome
        row.suggested_outcome = st.suggested
        row.comment = d.comment
        row.sessions_held, row.sessions_present, row.sessions_excused = st.held, st.present, st.excused
        row.decided_by, row.decided_at = actor_id, now
        row.enrolment_change, what = apply_result(db, cls, entry, d.outcome, made, now)
        service.audit(db, actor.id, cls, "class_result",
                      f"{entry.email}: {d.outcome} (suggested {st.suggested}; attended {st.present + st.excused}"
                      f"/{st.held}); {what}"[:2000])
    cls.status = ClassStatus.closed.value
    # tenant-safe: keyed by the class's own id.
    mark = db.get(ClassCloseout, cls.id) or ClassCloseout(class_id=cls.id, tenant_id=cls.tenant_id, closed_at=now)
    mark.closed_at, mark.closed_by = now, actor_id
    db.add(mark)
    # Sessions still to come are cancelled, and so are drafts and sessions still awaiting an
    # approver whatever their time (never held). Approved sessions that ended stay as they are,
    # even one the clock never moved on from scheduled: the record keeps every session held.
    to_come = [eid for (eid,) in db.query(ScheduledEvent.id).filter(
        ScheduledEvent.id.in_(session_ids), ScheduledEvent.tenant_id == cls.tenant_id,
        or_(ScheduledEvent.start_time > now,
            ScheduledEvent.state.in_((EventState.draft, EventState.pending_approval))))]
    cancels = scheduler.cancel_unstarted(db, actor, to_come)
    tally = {o.value: sum(1 for d in decisions if d.outcome == o.value) for o in ResultOutcome}
    service.audit(db, actor.id, cls, "class_close",
                  f"{cls.name}: " + ", ".join(f"{n} {o}" for o, n in tally.items()))
    db.flush()
    return cancels


def reopen(db: Session, cls: Class, actor, reason: str) -> None:
    """An admin reopens a closed class. Audited. Does not commit."""
    if actor.role != UserRole.admin:
        raise HTTPException(403, "Only an admin reopens a closed class")
    if cls.status != ClassStatus.closed.value:
        raise HTTPException(409, "Only a closed class is reopened")
    cls.status = ClassStatus.open.value
    mark = db.get(ClassCloseout, cls.id)  # tenant-safe: keyed by the class's own id
    if mark is not None:
        mark.reopened_at, mark.reopened_by, mark.reopen_reason = _now(), uuid.UUID(str(actor.id)), reason
    service.audit(db, actor.id, cls, "class_reopen", f"{cls.name}: {reason}"[:2000])
    db.flush()


# ── Export ───────────────────────────────────────────────────────────────────


def export_csv(db: Session, cls: Class) -> str:
    """The class's attendance and results as CSV, formula cells defused. Pass/fail only."""
    held, standing = prefill(db, cls)
    course = db.get(Course, cls.course_id)  # tenant-safe: the class's own course
    out = io.StringIO()
    writer = csv.writer(out)
    labels = [f"{evt.start_time:%Y-%m-%d %H:%M} {evt.name}" for _, evt in held]
    writer.writerow([csv_cell(v) for v in (
        "class", "course", "class_status", "email", "name", "sessions_held", "present", "excused", "absent",
        *labels, "suggested", "result", "comment", "decided_at")])
    for st in standing:
        r = st.result
        writer.writerow([csv_cell(v) for v in (
            cls.name, course.name if course else "", cls.status, st.entry.email, st.display_name or "",
            st.held, st.present, st.excused, st.absent,
            *[st.per_session.get(cs.id) or "" for cs, _ in held],
            st.suggested, r.outcome if r else "", r.comment if r else "",
            r.decided_at.isoformat() if r and r.decided_at else "")])
    return out.getvalue()


def closed_at(db: Session, cls: Class) -> datetime | None:
    """When the class was last closed, while it is closed."""
    if cls.status != ClassStatus.closed.value:
        return None
    mark = db.get(ClassCloseout, cls.id)  # tenant-safe: keyed by the class's own id
    return mark.closed_at if mark else None
