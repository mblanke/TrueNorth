"""The unit readiness page (ADR 0014 decision 7; issue #289): commander = observer + this page.

For one tenant (the caller's, never another) and a date range, every class that overlaps it
(cancelled ones left out) with, for each Student on it:

- *enrolment*: their roll-call status and their enrolment on the class's course;
- *attendance*: the sessions recorded for them (``class_attendance``, ``attendance.py``):
  present, excused, absent. What was recorded, never a fresh judgement;
- *result*: pass, fail or incomplete with the instructor's comment, as set at close-out
  (``class_results``, ``closeout.py``). Never a score or grade;
- *qualification earned*: the course's QSP qualification (``Course.qualification_id``, its
  NQual) when the result reached the Student's record: a pass on a closed class whose
  enrolment is completed (close-out sends results "to the Student's record and
  qualification", ADR 0014 §13). A pass for someone with no account or enrolment, or on a
  class an admin reopened, earns nothing here.

**Unit.** TrueNorth has no unit record: a Student's unit is ``users.unit``, the free-text
field the directory sync and registration fill (rank, unit, callsign). It is the closest real
grouping (organizational units group teams and security groups, not people). The filter keeps
the Students whose unit matches, case and surrounding spaces ignored, and the classes they
are on. Someone on a roll call who never signed in has no account, so no unit: they show only
unfiltered.

**Reads only.** No row is written by ``build``; an export adds one audit row (``audit_export``).

**Scale.** A fixed number of queries, whatever the number of classes and Students: one each
for the classes, the roll call (with names and units), attendance counts, sessions held,
results, enrolments, close-out times, qualifications and the unit list. Each filters by class,
so no parameter list grows with the number of Students. Tested in
``tests/api/test_unit_readiness_289.py``.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from ..models import AuditLog, Course, Enrollment, EnrollmentStatus, Qualification, User
from ..scheduler.models import ScheduledEvent
from .attendance import HELD, NOT_EXPECTED
from .closeout import csv_cell
from .models import (
    AttendanceStatus,
    Class,
    ClassAttendance,
    ClassCloseout,
    ClassResult,
    ClassSession,
    ClassStatus,
    ResultOutcome,
    RollCallEntry,
)
from .readiness_schemas import (
    ReadinessClassOut,
    ReadinessQualificationOut,
    ReadinessStudentOut,
    ReadinessSummaryOut,
    UnitReadinessOut,
)

DEFAULT_BACK = timedelta(days=365)  # the default window: the last year ...
DEFAULT_AHEAD = timedelta(days=90)  # ... and the next three months
MAX_SPAN = timedelta(days=3660)  # about ten years: a bigger range is refused (422)

CSV_COLUMNS = (
    "class", "course", "class_start", "class_end", "class_status", "unit", "name", "email", "roll_call_status",
    "enrolment_status", "present", "excused", "absent", "result", "comment", "decided_at", "qualification_earned",
)


def _now() -> datetime:
    return datetime.now(UTC)


def unit_key(unit: str | None) -> str:
    """How units compare: case and surrounding spaces ignored."""
    return (unit or "").strip().lower()


def window(date_from: date | None, date_to: date | None, today: date | None = None) -> tuple[date, date]:
    """The date range asked for, with the defaults filled in. ValueError when it is
    back to front or too long."""
    today = today or _now().date()
    start = date_from or (today - DEFAULT_BACK)
    end = date_to or max(today + DEFAULT_AHEAD, start)
    if start > end:
        raise ValueError("date_from is after date_to")
    if end - start > MAX_SPAN:
        raise ValueError(f"the date range is longer than {MAX_SPAN.days} days")
    return start, end


def _units(db: Session, tenant_id: uuid.UUID, class_ids: list[uuid.UUID]) -> list[str]:
    """The units of the Students on these classes, one spelling each, sorted."""
    if not class_ids:
        return []
    rows = (
        db.query(User.unit)
        .join(RollCallEntry, RollCallEntry.user_id == User.id)
        .filter(RollCallEntry.class_id.in_(class_ids), RollCallEntry.tenant_id == tenant_id,
                RollCallEntry.status.notin_(NOT_EXPECTED), User.tenant_id == tenant_id, User.unit.isnot(None))
        .distinct()
        .all()
    )
    spelt: dict[str, str] = {}
    for (unit,) in rows:
        key, shown = unit_key(unit), (unit or "").strip()
        if key and (key not in spelt or shown < spelt[key]):
            spelt[key] = shown
    return sorted(spelt.values(), key=lambda u: (u.lower(), u))


def build(db: Session, tenant_id: uuid.UUID, date_from: date, date_to: date, unit: str | None,
          now: datetime | None = None) -> UnitReadinessOut:
    """The readiness of ``tenant_id``'s classes overlapping [date_from, date_to]. Reads only."""
    now = now or _now()
    want = unit_key(unit) or None
    classes = (
        db.query(Class, Course.name, Course.qualification_id)
        # The class's course is its tenant's own or a shared one (``service.course_for``);
        # checked again here, so another tenant's course is never named.
        .outerjoin(Course, and_(Course.id == Class.course_id,
                                or_(Course.tenant_id == tenant_id, Course.tenant_id.is_(None))))
        .filter(Class.tenant_id == tenant_id, Class.status != ClassStatus.cancelled.value,
                Class.start_date <= date_to, Class.end_date >= date_from)
        .order_by(Class.start_date, Class.name, Class.id)
        .all()
    )
    class_ids = [c.id for c, _, _ in classes]
    units = _units(db, tenant_id, class_ids)

    roll: dict[uuid.UUID, list[tuple[RollCallEntry, str | None, str | None]]] = defaultdict(list)
    if class_ids:
        q = (
            db.query(RollCallEntry, User.display_name, User.unit)
            .outerjoin(User, and_(User.id == RollCallEntry.user_id, User.tenant_id == tenant_id))
            .filter(RollCallEntry.class_id.in_(class_ids), RollCallEntry.tenant_id == tenant_id,
                    RollCallEntry.status.notin_(NOT_EXPECTED))
        )
        if want:
            q = q.filter(func.lower(func.trim(User.unit)) == want)
        for entry, name, entry_unit in q.all():
            roll[entry.class_id].append((entry, name, entry_unit))
    # By class, not by entry: no parameter list that grows with the Students. Rows of
    # Students the unit filter left out are read and never shown.
    any_entry = any(roll.values())

    counts: dict[uuid.UUID, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    results: dict[uuid.UUID, ClassResult] = {}
    if any_entry:
        for entry_id, status, n in (
            db.query(ClassAttendance.entry_id, ClassAttendance.status, func.count(ClassAttendance.id))
            .filter(ClassAttendance.class_id.in_(class_ids), ClassAttendance.tenant_id == tenant_id)
            .group_by(ClassAttendance.entry_id, ClassAttendance.status)
        ):
            counts[entry_id][status] += n
        results = {r.entry_id: r for r in db.query(ClassResult).filter(
            ClassResult.class_id.in_(class_ids), ClassResult.tenant_id == tenant_id)}

    held: dict[uuid.UUID, int] = {}
    closed: dict[uuid.UUID, datetime] = {}
    if class_ids:
        held = dict(
            db.query(ClassSession.class_id, func.count(ClassSession.id))
            .join(ScheduledEvent, ScheduledEvent.id == ClassSession.event_id)
            .filter(ClassSession.class_id.in_(class_ids), ClassSession.tenant_id == tenant_id,
                    ScheduledEvent.tenant_id == tenant_id, ScheduledEvent.state.in_(HELD),
                    ScheduledEvent.end_time <= now)
            .group_by(ClassSession.class_id)
        )
        closed = dict(db.query(ClassCloseout.class_id, ClassCloseout.closed_at).filter(
            ClassCloseout.class_id.in_(class_ids), ClassCloseout.tenant_id == tenant_id))

    # Each Student's enrolment on their class's course, reached through the roll call (by
    # class, again: no parameter per Student).
    enrolments: dict[tuple[uuid.UUID, uuid.UUID], str] = {}
    if any_entry:
        for uid, course_id, status in (
            db.query(Enrollment.user_id, Enrollment.course_id, Enrollment.status)
            .join(RollCallEntry, RollCallEntry.user_id == Enrollment.user_id)
            .join(Class, and_(Class.id == RollCallEntry.class_id, Class.course_id == Enrollment.course_id))
            .filter(RollCallEntry.class_id.in_(class_ids), RollCallEntry.tenant_id == tenant_id,
                    Class.tenant_id == tenant_id, Enrollment.tenant_id == tenant_id)
            .distinct()
        ):
            enrolments[(uid, course_id)] = getattr(status, "value", status)

    qual_ids = {q for _, _, q in classes if q}
    quals = {q.id: q for q in db.query(Qualification).filter(
        Qualification.id.in_(qual_ids),
        or_(Qualification.tenant_id == tenant_id, Qualification.tenant_id.is_(None)))} if qual_ids else {}

    out_classes: list[ReadinessClassOut] = []
    earned: dict[uuid.UUID, int] = defaultdict(int)
    people: set[str] = set()
    for cls, course_name, qual_id in classes:
        rows = roll.get(cls.id, [])
        if want and not rows:
            continue  # nobody of that unit on it
        qual = quals.get(qual_id) if qual_id else None
        students = []
        for e, name, entry_unit in rows:
            c = counts.get(e.id, {})
            r = results.get(e.id)
            enrolment = enrolments.get((e.user_id, cls.course_id)) if e.user_id else None
            # Earned only when it reached the Student's record: a pass on a class that is
            # closed (a reopened one is being decided again), whose enrolment is completed.
            # A pass for someone with no account or no enrolment stays on the class only.
            earns = (qual is not None and cls.status == ClassStatus.closed.value and r is not None
                     and r.outcome == ResultOutcome.passed.value and enrolment == EnrollmentStatus.completed.value)
            if earns:
                earned[qual.id] += 1
            people.add(e.email.strip().lower())  # the roll call matches people by email
            students.append(ReadinessStudentOut(
                entry_id=e.id, email=e.email, display_name=name if e.user_id else None,
                unit=((entry_unit or "").strip() or None) if e.user_id else None, roll_call_status=e.status,
                enrolment_status=enrolment,
                present=c.get(AttendanceStatus.present.value, 0), excused=c.get(AttendanceStatus.excused.value, 0),
                absent=c.get(AttendanceStatus.absent.value, 0),
                outcome=r.outcome if r else None, comment=r.comment if r else None,
                decided_at=r.decided_at if r else None,
                qualification_earned=qual.nqual if earns else None,
            ))
        students.sort(key=lambda s: ((s.display_name or s.email).lower(), s.email))
        tally = defaultdict(int)
        for s in students:
            tally[s.outcome.value if s.outcome else None] += 1
        out_classes.append(ReadinessClassOut(
            class_id=cls.id, name=cls.name, course_id=cls.course_id, course_name=course_name or "",
            qualification=qual.nqual if qual else None, start_date=cls.start_date, end_date=cls.end_date,
            status=cls.status, closed_at=closed.get(cls.id) if cls.status == ClassStatus.closed.value else None,
            sessions_held=held.get(cls.id, 0), enrolled=len(students),
            passed=tally[ResultOutcome.passed.value], failed=tally[ResultOutcome.failed.value],
            incomplete=tally[ResultOutcome.incomplete.value], awaiting_result=tally[None], students=students,
        ))

    every = [s for c in out_classes for s in c.students]
    summary = ReadinessSummaryOut(
        classes=len(out_classes), enrolments=len(every), students=len(people),
        sessions_held=sum(c.sessions_held for c in out_classes),
        present=sum(s.present for s in every), excused=sum(s.excused for s in every),
        absent=sum(s.absent for s in every),
        passed=sum(c.passed for c in out_classes), failed=sum(c.failed for c in out_classes),
        incomplete=sum(c.incomplete for c in out_classes),
        awaiting_result=sum(c.awaiting_result for c in out_classes),
        qualifications_earned=sum(earned.values()),
    )
    qualifications = sorted(
        (ReadinessQualificationOut(qualification_id=q.id, code=q.nqual, title=q.title or q.nqual, earned=earned[q.id])
         for q in quals.values() if earned.get(q.id)),
        key=lambda q: (q.code, q.title))
    return UnitReadinessOut(date_from=date_from, date_to=date_to, unit=(unit or "").strip() or None, units=units,
                            summary=summary, classes=out_classes, qualifications=qualifications)


# ── Exports ──────────────────────────────────────────────────────────────────


def csv_rows(report: UnitReadinessOut) -> list[list[str]]:
    """One row per Student per class, every cell defused (``closeout.csv_cell``)."""
    rows = [[csv_cell(v) for v in CSV_COLUMNS]]
    for c in report.classes:
        for s in c.students:
            rows.append([csv_cell(v) for v in (
                c.name, c.course_name, c.start_date.isoformat(), c.end_date.isoformat(), c.status, s.unit or "",
                s.display_name or "", s.email, s.roll_call_status, s.enrolment_status or "", s.present, s.excused,
                s.absent, s.outcome.value if s.outcome else "", s.comment or "",
                s.decided_at.isoformat() if s.decided_at else "", s.qualification_earned or "")])
    return rows


def audit_export(db: Session, actor_id: str, tenant_id: uuid.UUID, fmt: str, report: UnitReadinessOut) -> None:
    """An export takes Students' records off the platform: who, what and how many."""
    detail = (f"{fmt}: {report.date_from.isoformat()}..{report.date_to.isoformat()}"
              f" unit={report.unit or 'all'} classes={report.summary.classes} rows={report.summary.enrolments}")
    db.add(AuditLog(user_id=uuid.UUID(str(actor_id)), tenant_id=tenant_id, action="readiness_export",
                    resource_type="readiness", resource_id=str(tenant_id), detail=detail[:2000]))
