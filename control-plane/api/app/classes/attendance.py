"""Automatic attendance per class session (ADR 0014 §12; issue #283).

**The rule.** A Student is *present* at a class session when they signed in and opened their
range during it: between the session's start (inclusive) and its end (exclusive). Otherwise
*absent*. Only the lead or an admin sets *excused*, or corrects either way; every correction
is audited and the clock never overwrites it.

**The signal: an audit row of the Student opening their own class range.** What counts:

- ``class_range_open``: written when a Student loads their class range's credential panel
  (``GET /ranges/{id}/credentials``, which every range page a Student opens calls) and the
  range is theirs through the class (``ranges.student_participation``: their own seat, or
  the class's shared range while a collective session is under way). At most one row per
  Student and range every ``OPEN_EVERY`` (``note_range_open``);
- ``range_credential_reveal``: a Student revealing one of that range's passwords (ADR 0013
  §3), which was already audited.

Why this and not another signal:

- *It proves both halves.* The request carries a valid session token for that account (it
  went through ``get_current_user``), so the Student was signed in, and it names the range,
  which ``student_participation`` has just confirmed is theirs. TrueNorth records no
  per-session sign-in time of its own (Keycloak holds that), so a separate "signed in" check
  would add nothing the range read does not already prove.
- *It is written in the same transaction as the read it records,* by the API, keyed by user,
  range and time, on the append-only audit log (``tests/api/test_audit_append_only_postgres``).
- *The alternatives are weaker.* Class ranges have no browser console (only lab sessions do,
  ``console_backends``), so no console URL is issued for them. The password reveal alone
  misses the second day of a week-long class, when the Student still has the password from
  Monday. Telemetry from the range's machines needs OpenSearch, says nothing about which
  Student on a shared range was at the keyboard, and is not in the database the clock reads.

Evidence only counts on the Student's own seat or the class's shared ranges (and the
session's own booked range), never another Student's seat or another class's range.

**When.** ``tick`` (the API's class clock, every ``CLASS_ATTENDANCE_TICK_SECONDS``) records
attendance for every approved session of an open class that ended in the last
``LOOKBACK``. A Student counts for a session if they were on the roll call before it ended
(removed and pending entries never; a forfeited one is absent). Close-out records any
session the clock has not reached yet (``record_class``).

**Idempotent.** Each (session, roll-call entry) row is inserted once under a unique
constraint, in a savepoint: a replica that loses the race gets an IntegrityError and moves
on, and a row that exists (auto or an instructor's) is never touched again. Evidence is read
from rows written before the session ended, so recomputing gives the same answer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AuditLog
from ..scheduler.models import EventState, ScheduledEvent
from . import service
from .models import (
    AttendanceSource,
    AttendanceStatus,
    Class,
    ClassAttendance,
    ClassRange,
    ClassRangeKind,
    ClassSession,
    ClassStatus,
    RollCallEntry,
    RollCallStatus,
)

logger = logging.getLogger("truenorth.api.classes.attendance")

TICK_SECONDS = float(os.getenv("CLASS_ATTENDANCE_TICK_SECONDS", "300"))
LOOKBACK = timedelta(days=float(os.getenv("CLASS_ATTENDANCE_LOOKBACK_DAYS", "3")))
OPEN_EVERY = timedelta(minutes=10)

RANGE_OPEN = "class_range_open"
EVIDENCE_ACTIONS = (RANGE_OPEN, "range_credential_reveal")
# Sessions an approver let through: they were held (or are being held). Drafts, sessions
# awaiting approval, rejected and cancelled ones never take attendance.
HELD = (EventState.scheduled, EventState.provisioning, EventState.active, EventState.completed)
# Never counted: taken off the roll call, or still waiting for an approver.
NOT_EXPECTED = (RollCallStatus.removed.value, RollCallStatus.pending.value)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# ── The signal ───────────────────────────────────────────────────────────────


def note_range_open(db: Session, user_id: uuid.UUID, rng, participation: dict | None, now: datetime | None = None) -> bool:
    """Record that a Student opened a range they take part in through a class (their own
    seat, or its shared range during a collective session), at most once per range every
    ``OPEN_EVERY``. Returns whether a row was added. Does not commit."""
    if not participation or not ({"class_seat_id", "class_id"} & set(participation)):
        return False
    now = now or _now()
    recent = (
        db.query(AuditLog.id)
        .filter(
            AuditLog.resource_type == "range",
            AuditLog.resource_id == str(rng.id),
            AuditLog.tenant_id == rng.tenant_id,
            AuditLog.user_id == user_id,
            AuditLog.action == RANGE_OPEN,
            AuditLog.timestamp >= now - OPEN_EVERY,
        )
        .first()
    )
    if recent is not None:
        return False
    detail = ", ".join(f"{k}={v}" for k, v in sorted(participation.items()))
    db.add(AuditLog(user_id=user_id, tenant_id=rng.tenant_id, action=RANGE_OPEN, resource_type="range",
                    resource_id=str(rng.id), detail=detail[:2000], timestamp=now))
    return True


# ── Reading the evidence ─────────────────────────────────────────────────────


def held_sessions(db: Session, cls: Class, *, ended_by: datetime | None = None,
                  started_by: datetime | None = None) -> list[tuple[ClassSession, ScheduledEvent]]:
    """The class's approved sessions, earliest first; only those over by ``ended_by``, or
    started by ``started_by``, when given."""
    q = (
        db.query(ClassSession, ScheduledEvent)
        .join(ScheduledEvent, ScheduledEvent.id == ClassSession.event_id)
        .filter(
            ClassSession.class_id == cls.id,
            ClassSession.tenant_id == cls.tenant_id,
            ScheduledEvent.tenant_id == cls.tenant_id,
            ScheduledEvent.state.in_(HELD),
        )
    )
    if ended_by is not None:
        q = q.filter(ScheduledEvent.end_time <= ended_by)
    if started_by is not None:
        q = q.filter(ScheduledEvent.start_time <= started_by)
    return q.order_by(ScheduledEvent.start_time, ScheduledEvent.id).all()


def expected(entries: list[RollCallEntry], evt: ScheduledEvent) -> list[RollCallEntry]:
    """Who counts for ``evt``: on the roll call (or forfeited) before it ended."""
    end = _aware(evt.end_time)
    out = []
    for e in entries:
        if e.status in NOT_EXPECTED:
            continue
        since = e.joined_at or e.created_at
        if since is not None and _aware(since) >= end:
            continue
        out.append(e)
    return out


def roll_call(db: Session, cls: Class) -> list[RollCallEntry]:
    return (
        db.query(RollCallEntry)
        .filter(RollCallEntry.class_id == cls.id, RollCallEntry.tenant_id == cls.tenant_id)
        .order_by(RollCallEntry.email)
        .all()
    )


def _ranges(db: Session, cls: Class) -> tuple[set[str], dict[uuid.UUID, set[str]]]:
    """(the class's shared range ids, each roll-call entry's own seat range ids)."""
    shared: set[str] = set()
    seats: dict[uuid.UUID, set[str]] = {}
    rows = (
        db.query(ClassRange.kind, ClassRange.entry_id, ClassRange.range_id)
        .filter(ClassRange.class_id == cls.id, ClassRange.tenant_id == cls.tenant_id, ClassRange.range_id.isnot(None))
        .all()
    )
    for kind, entry_id, range_id in rows:
        if kind == ClassRangeKind.shared.value:
            shared.add(str(range_id))
        elif entry_id is not None:
            seats.setdefault(entry_id, set()).add(str(range_id))
    return shared, seats


def evaluate(db: Session, cls: Class, evt: ScheduledEvent,
             entries: list[RollCallEntry]) -> dict[uuid.UUID, tuple[str, datetime | None]]:
    """(status, first evidence) per entry for one session, from the evidence alone. Reads
    only; one query for every Student."""
    shared, seats = _ranges(db, cls)
    if evt.range_id is not None:
        shared = shared | {str(evt.range_id)}
    uids = {e.user_id for e in entries if e.user_id is not None}
    every = shared.union(*seats.values()) if seats else set(shared)
    first: dict[tuple[uuid.UUID, str], datetime] = {}
    if uids and every:
        rows = (
            db.query(AuditLog.user_id, AuditLog.resource_id, func.min(AuditLog.timestamp))
            .filter(
                AuditLog.tenant_id == cls.tenant_id,
                AuditLog.resource_type == "range",
                AuditLog.resource_id.in_(every),
                AuditLog.user_id.in_(uids),
                AuditLog.action.in_(EVIDENCE_ACTIONS),
                AuditLog.timestamp >= evt.start_time,
                AuditLog.timestamp < evt.end_time,
            )
            .group_by(AuditLog.user_id, AuditLog.resource_id)
            .all()
        )
        first = {(uid, rid): _aware(ts) for uid, rid, ts in rows}
    out: dict[uuid.UUID, tuple[str, datetime | None]] = {}
    for e in entries:
        mine = shared | seats.get(e.id, set())
        seen = [ts for (uid, rid), ts in first.items() if uid == e.user_id and rid in mine] if e.user_id else []
        at = min(seen) if seen else None
        out[e.id] = (AttendanceStatus.present.value if at else AttendanceStatus.absent.value, at)
    return out


# ── Recording it ─────────────────────────────────────────────────────────────


def record_session(db: Session, cls: Class, cs: ClassSession, evt: ScheduledEvent,
                   entries: list[RollCallEntry] | None = None) -> int:
    """Insert the missing auto rows of one ended session. Existing rows (auto or an
    instructor's) are never touched. Returns how many this call inserted. Does not commit."""
    who = expected(entries if entries is not None else roll_call(db, cls), evt)
    have = {
        eid for (eid,) in db.query(ClassAttendance.entry_id).filter(
            ClassAttendance.session_id == cs.id, ClassAttendance.tenant_id == cls.tenant_id)
    }
    missing = [e for e in who if e.id not in have]
    if not missing:
        return 0
    verdicts = evaluate(db, cls, evt, missing)
    added = 0
    for e in missing:
        status, at = verdicts[e.id]
        try:
            with db.begin_nested():
                db.add(ClassAttendance(
                    tenant_id=cls.tenant_id, class_id=cls.id, session_id=cs.id, entry_id=e.id, user_id=e.user_id,
                    status=status, source=AttendanceSource.auto.value, evidence_at=at,
                ))
            added += 1
        except IntegrityError:
            continue  # another replica recorded it first; theirs stands
    return added


def record_class(db: Session, cls: Class, now: datetime | None = None) -> int:
    """Record every ended session of ``cls`` not recorded yet (close-out). Does not commit."""
    now = now or _now()
    entries = roll_call(db, cls)
    return sum(record_session(db, cls, cs, evt, entries) for cs, evt in held_sessions(db, cls, ended_by=now))


@dataclass
class TickResult:
    sessions: int = 0
    recorded: int = 0
    failed: list[uuid.UUID] = field(default_factory=list)


def tick(db: Session, now: datetime | None = None) -> TickResult:
    """Record attendance for the approved sessions of open classes that ended in the last
    ``LOOKBACK``. Commits per session, so one failure undoes nothing else."""
    now = now or _now()
    res = TickResult()
    due = (
        db.query(ClassSession.id)
        .join(ScheduledEvent, ScheduledEvent.id == ClassSession.event_id)
        .join(Class, Class.id == ClassSession.class_id)
        .filter(
            Class.status == ClassStatus.open.value,
            Class.tenant_id == ClassSession.tenant_id,
            ScheduledEvent.tenant_id == ClassSession.tenant_id,
            ScheduledEvent.state.in_(HELD),
            ScheduledEvent.end_time <= now,
            ScheduledEvent.end_time > now - LOOKBACK,
        )
        .order_by(ScheduledEvent.end_time)
        .all()
    )
    for (sid,) in due:
        try:
            cs = db.get(ClassSession, sid)  # tenant-safe: an id this query just read
            cls = db.get(Class, cs.class_id)  # tenant-safe: the session's own class
            evt = db.get(ScheduledEvent, cs.event_id)  # tenant-safe: the session's own booking
            res.recorded += record_session(db, cls, cs, evt)
            res.sessions += 1
            db.commit()
        except Exception:
            db.rollback()
            res.failed.append(sid)
            logger.exception("Attendance for class session %s failed; retrying next tick", sid)
    return res


# ── Corrections ──────────────────────────────────────────────────────────────


def correct(db: Session, cls: Class, cs: ClassSession, evt: ScheduledEvent, entry: RollCallEntry, status: str,
            note: str | None, actor, now: datetime | None = None) -> ClassAttendance:
    """The lead or an admin sets one Student's attendance at one session that has started.
    Audited, with what it was. Does not commit."""
    now = now or _now()
    if cls.status != ClassStatus.open.value:
        raise HTTPException(409, "The class is not open; its attendance cannot change")
    if _aware(evt.start_time) > now:
        raise HTTPException(409, "This session has not started yet")
    if entry.status in NOT_EXPECTED:
        raise HTTPException(409, "This Student is not on the roll call")
    row = (
        db.query(ClassAttendance)
        .filter(ClassAttendance.session_id == cs.id, ClassAttendance.entry_id == entry.id,
                ClassAttendance.tenant_id == cls.tenant_id)
        .first()
    )
    before = f"{row.status} ({row.source})" if row else "not recorded"
    if row is None:
        row = ClassAttendance(tenant_id=cls.tenant_id, class_id=cls.id, session_id=cs.id, entry_id=entry.id,
                              user_id=entry.user_id)
        db.add(row)
    row.status = status
    row.source = AttendanceSource.instructor.value
    row.note = note
    row.corrected_by = uuid.UUID(str(actor.id))
    row.corrected_at = now
    service.audit(
        db, actor.id, cls, "class_attendance_correct",
        f"session {cs.id} ({evt.start_time:%Y-%m-%d %H:%M}), {entry.email}: {before} -> {status}"
        + (f"; {note}" if note else ""),
    )
    db.flush()
    return row


def records(db: Session, cls: Class) -> dict[tuple[uuid.UUID, uuid.UUID], ClassAttendance]:
    """Every recorded row of the class, by (session, entry)."""
    rows = db.query(ClassAttendance).filter(
        ClassAttendance.class_id == cls.id, ClassAttendance.tenant_id == cls.tenant_id).all()
    return {(r.session_id, r.entry_id): r for r in rows}


def counts(rows: list[ClassAttendance]) -> dict[str, int]:
    out = {s.value: 0 for s in AttendanceStatus}
    for r in rows:
        out[r.status] = out.get(r.status, 0) + 1
    return out


# ── The clock ────────────────────────────────────────────────────────────────


def _tick_in_own_session() -> TickResult:
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        return tick(db)
    finally:
        db.close()


async def run_forever() -> None:
    """Started from the API lifespan. Sleeps first, so a test client does not tick."""
    logger.info("Class attendance clock started (every %ss)", TICK_SECONDS)
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            res = await asyncio.to_thread(_tick_in_own_session)
            if res.recorded:
                logger.info("Class attendance: %d row(s) over %d session(s)", res.recorded, res.sessions)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Class attendance tick failed; retrying next tick")

