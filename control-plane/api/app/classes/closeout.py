"""Close-out: results, closing, reopening and the export (ADR 0014 §13; issue #283).

**Pre-fill.** For each Student on the roll call the lead sees:

- *attendance*: sessions held, present, excused and absent (``attendance.py``);
- *the course*: their enrolment's module progress, which is where quiz results land
  (``routers/quizzes.py`` writes them into ``module_progress``), counted as
  ``enrollment.complete_enrollment`` counts it: graded modules only. Passed when that is at
  least ``PASS_PCT``;
- *the exercises*: detections credited to them in the exercises of the class's sessions
  (ADR 0005: Students earn exercise credit only by submitting detections);
- a *suggested* outcome: ``incomplete`` below ``ATTENDANCE_MIN`` of the sessions held
  (present or excused), else ``fail`` when the graded course work is below the pass mark,
  else ``pass``. The lead decides; the suggestion is kept beside the decision.

**Unclassified (DLP).** On an Unclassified deployment (``settings.classification_level``,
and whenever it cannot be read) the pre-fill carries pass/fail only: no percentage, no
score, no detection count. ``class_results`` has no numeric column at all, and the
enrolment a close-out completes keeps no score or letter grade there (``apply_result``).

**Closing.** The lead or an admin sets pass, fail or incomplete for every Student on the
roll call, each with an optional comment. In one transaction:

1. attendance is recorded for any ended session the clock has not reached;
2. each result is stored and goes to the Student's enrolment on the class's course through
   the existing completion path (``complete_enrollment``): ``pass`` completes it, ``fail``
   marks it failed, ``incomplete`` leaves it open. Qualifications follow, since the QSP
   spine reads enrolment and module progress (``qsp_progress``);
3. the class becomes ``closed``: read-only. Its sessions that have not started are
   cancelled, and its ranges are torn down by the clock as for any class no longer open
   (``ranges._why_done``: ``class_closed``);
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
from sqlalchemy.orm import Session

from ..enrollment import complete_enrollment
from ..models import Course, CourseModule, Enrollment, EnrollmentStatus, ModuleProgress, User, UserRole
from ..scheduler import service as scheduler
from . import attendance, service, sessions
from .models import (
    OFF_ROLL_CALL,
    AttendanceStatus,
    Class,
    ClassCloseout,
    ClassResult,
    ClassStatus,
    ResultOutcome,
    RollCallEntry,
)

PASS_PCT = 70  # the default module pass threshold (CourseModule.pass_threshold)
ATTENDANCE_MIN = 0.8  # of the sessions held, present or excused, for a pass to be suggested

# A spreadsheet runs a cell that starts with one of these as a formula (CSV injection).
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")


def _now() -> datetime:
    return datetime.now(UTC)


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
    """Who gets a result: everyone on the roll call now."""
    return [e for e in attendance.roll_call(db, cls) if e.status not in OFF_ROLL_CALL]


def _course(db: Session, cls: Class, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, tuple[float | None, int]]:
    """(graded percentage or None, modules) per Student on the class's course."""
    if not user_ids:
        return {}
    module_ids = {mid for (mid,) in db.query(CourseModule.id).filter(CourseModule.course_id == cls.course_id)}
    enrolments = {
        e.id: e.user_id for e in db.query(Enrollment).filter(
            Enrollment.course_id == cls.course_id, Enrollment.user_id.in_(user_ids))
    }
    sums: dict[uuid.UUID, list[int]] = {}
    if enrolments and module_ids:
        for p in db.query(ModuleProgress).filter(
                ModuleProgress.enrollment_id.in_(enrolments), ModuleProgress.module_id.in_(module_ids)):
            if p.max_score:
                acc = sums.setdefault(enrolments[p.enrollment_id], [0, 0])
                acc[0] += p.score or 0
                acc[1] += p.max_score
    return {uid: ((100 * s / m) if m else None, len(module_ids)) for uid, (s, m) in sums.items()}


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


def suggest(held: int, present: int, excused: int, course_passed: bool | None) -> str:
    if held and (present + excused) / held < ATTENDANCE_MIN:
        return ResultOutcome.incomplete.value
    if course_passed is False:
        return ResultOutcome.failed.value
    return ResultOutcome.passed.value


def prefill(db: Session, cls: Class, now: datetime | None = None) -> tuple[list, list[Standing]]:
    """(the sessions held so far, each Student's standing). Reads only: a session the clock
    has not recorded yet is judged from the evidence as the clock would judge it."""
    now = now or _now()
    held = attendance.held_sessions(db, cls, ended_by=now)
    entries = students(db, cls)
    recorded = attendance.records(db, cls)
    computed = {cs.id: attendance.evaluate(db, cls, evt, attendance.expected(entries, evt)) for cs, evt in held
                if any((cs.id, e.id) not in recorded for e in entries)}
    uids = {e.user_id for e in entries if e.user_id}
    names = dict(db.query(User.id, User.display_name).filter(User.id.in_(uids), User.tenant_id == cls.tenant_id)) \
        if uids else {}
    course = _course(db, cls, uids)
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
        pct, _ = course.get(e.user_id, (None, 0)) if e.user_id else (None, 0)
        st.course_passed = None if pct is None else pct >= PASS_PCT
        if numbers:
            st.percent = None if pct is None else round(pct, 1)
            st.detections = dets.get(e.user_id, 0) if e.user_id else 0
        st.suggested = suggest(st.held, st.present, st.excused, st.course_passed)
        out.append(st)
    return held, out


# ── Closing ──────────────────────────────────────────────────────────────────


@dataclass
class Decision:
    entry_id: uuid.UUID
    outcome: str
    comment: str | None = None


def apply_result(db: Session, cls: Class, entry: RollCallEntry, outcome: str, before: str | None, now: datetime) -> str:
    """Take one result to the Student's enrolment on the class's course, through the
    existing completion path. Returns what happened, for the audit. Does not commit."""
    if entry.user_id is None:
        return "no account: kept on the class only"
    enrolment = (
        db.query(Enrollment)
        .filter(Enrollment.user_id == entry.user_id, Enrollment.course_id == cls.course_id,
                Enrollment.tenant_id == cls.tenant_id)
        .first()
    )
    if enrolment is None:
        return "not enrolled: kept on the class only"
    if outcome == ResultOutcome.passed.value:
        complete_enrollment(db, enrolment)
        what = "enrolment completed"
    elif outcome == ResultOutcome.failed.value:
        enrolment.status = EnrollmentStatus.failed
        enrolment.completed_at = now
        what = "enrolment failed"
    else:
        # Incomplete leaves it open; one this class's earlier close-out completed or failed
        # is opened again.
        if before in (ResultOutcome.passed.value, ResultOutcome.failed.value) and enrolment.status in (
                EnrollmentStatus.completed, EnrollmentStatus.failed):
            enrolment.status = EnrollmentStatus.in_progress
            enrolment.completed_at = None
            return "enrolment reopened"
        return "enrolment left open"
    if not numbers_allowed():
        # Unclassified: pass/fail only, never a numeric grade (ADR 0015's DLP rule).
        enrolment.final_grade = None
        enrolment.final_score = 0
        enrolment.max_score = 0
    db.flush()
    return what


def close(db: Session, cls: Class, decisions: list[Decision], actor, now: datetime | None = None) -> list:
    """Close the class with a result for every Student on its roll call. Returns the CANCEL
    invites of the sessions it cancelled, to send after commit. Does not commit."""
    now = now or _now()
    if cls.status != ClassStatus.open.value:
        raise HTTPException(409, f"The class is {cls.status}; only an open class is closed")
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
        before = row.outcome if row else None
        if row is None:
            row = ClassResult(tenant_id=cls.tenant_id, class_id=cls.id, entry_id=entry.id)
            db.add(row)
        row.user_id = entry.user_id
        row.outcome = d.outcome
        row.suggested_outcome = st.suggested
        row.comment = d.comment
        row.sessions_held, row.sessions_present, row.sessions_excused = st.held, st.present, st.excused
        row.decided_by, row.decided_at = actor_id, now
        what = apply_result(db, cls, entry, d.outcome, before, now)
        service.audit(db, actor.id, cls, "class_result",
                      f"{entry.email}: {d.outcome} (suggested {st.suggested}; attended {st.present + st.excused}"
                      f"/{st.held}); {what}"[:2000])
    cls.status = ClassStatus.closed.value
    # tenant-safe: keyed by the class's own id.
    mark = db.get(ClassCloseout, cls.id) or ClassCloseout(class_id=cls.id, tenant_id=cls.tenant_id, closed_at=now)
    mark.closed_at, mark.closed_by = now, actor_id
    db.add(mark)
    cancels = scheduler.cancel_unstarted(db, actor, sessions.session_ids(db, cls))
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
