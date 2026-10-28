"""A class's attendance and close-out (ADR 0014 §12-13; issue #283).

=================================================  ==========================================
Endpoint                                           Who
=================================================  ==========================================
GET  /classes/{id}/attendance                      its lead, assistants, observers, admins
PUT  /classes/{id}/attendance/{session_id}/        its lead or an admin: present or excused
     {entry_id}                                    (ADR 0014 §12), with a note. Audited
GET  /classes/{id}/closeout                        its lead, assistants, observers, admins:
                                                   the pre-filled results
POST /classes/{id}/close                           its lead or an admin: a result per Student
POST /classes/{id}/reopen                          an admin of its tenant, with a reason
GET  /classes/{id}/results.csv                     its lead, assistants, observers, admins
=================================================  ==========================================

Students on the roll call see none of this (403); another tenant's class, or one the caller
has no part in, is not found (404, ``access``). The rules are in ``attendance`` and
``closeout``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..models import User
from ..rbac import Permission, require_permission
from ..scheduler import service as scheduler
from ..scheduler.models import ScheduledEvent
from . import access, attendance, closeout
from .closeout_schemas import (
    AttendanceCellOut,
    AttendanceCorrectIn,
    AttendanceGridOut,
    AttendanceRowOut,
    AttendanceSessionOut,
    CloseIn,
    CloseoutOut,
    ReopenIn,
    StandingOut,
)
from .models import Class, ClassSession, RollCallEntry
from .router import _lock_conflicts

router = APIRouter(prefix="/classes", tags=["classes"])

_CHANGE = require_permission(Permission.CLASS_CREATE)
_STAFF_READ = require_permission(Permission.CLASS_READ)


def _staff(db: Session, class_id: uuid.UUID, user: CurrentUser) -> tuple[Class, bool]:
    cls, role = access.get_visible(db, class_id, user)
    access.require_staff(role)
    return cls, role in access.EDITORS


def _editable(db: Session, class_id: uuid.UUID, user: CurrentUser) -> Class:
    cls, role = access.get_visible(db, class_id, user)
    access.require_editor(role)
    return cls


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# ── Attendance ───────────────────────────────────────────────────────────────


@router.get(
    "/{class_id}/attendance", response_model=AttendanceGridOut, operation_id="getClassAttendance",
    summary="A class's attendance, per session and Student",
)
def get_attendance(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_STAFF_READ),
) -> AttendanceGridOut:
    """The approved sessions that have started, and each Student's attendance at them: as
    the clock recorded it after the session, or as the lead or an admin set it.
    **class:read**, then its staff only."""
    cls, can_edit = _staff(db, class_id, user)
    now = datetime.now(UTC)
    held = attendance.held_sessions(db, cls, started_by=now)
    entries = [e for e in attendance.roll_call(db, cls) if e.status not in attendance.NOT_EXPECTED]
    recorded = attendance.records(db, cls)
    uids = {e.user_id for e in entries if e.user_id}
    names = dict(db.query(User.id, User.display_name).filter(User.id.in_(uids), User.tenant_id == cls.tenant_id)) \
        if uids else {}
    rows = []
    for e in entries:
        cells = []
        for cs, _ in held:
            r = recorded.get((cs.id, e.id))
            cells.append(AttendanceCellOut(
                session_id=cs.id, status=r.status if r else None, source=r.source if r else None,
                evidence_at=r.evidence_at if r else None, note=r.note if r else None,
                corrected_at=r.corrected_at if r else None))
        rows.append(AttendanceRowOut(entry_id=e.id, email=e.email, display_name=names.get(e.user_id),
                                     roll_call_status=e.status, cells=cells))
    return AttendanceGridOut(
        class_id=cls.id, class_status=cls.status, can_edit=can_edit and cls.status == "open",
        sessions=[AttendanceSessionOut(session_id=cs.id, event_id=evt.id, name=evt.name, start_time=evt.start_time,
                                       end_time=evt.end_time, ended=_aware(evt.end_time) <= now)
                  for cs, evt in held],
        students=rows,
    )


@router.put(
    "/{class_id}/attendance/{session_id}/{entry_id}", response_model=AttendanceCellOut,
    operation_id="correctClassAttendance", summary="Set a Student's attendance at a class session",
)
def correct_attendance(
    class_id: uuid.UUID,
    session_id: uuid.UUID,
    entry_id: uuid.UUID,
    body: AttendanceCorrectIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_CHANGE),
) -> AttendanceCellOut:
    """Its lead or an admin, once the session has started, while the class is open. The
    clock never overwrites it. Audited (``class_attendance_correct``), with what it was.
    **class:create**"""
    cls = _editable(db, class_id, user)
    cs = (
        db.query(ClassSession)
        .filter(ClassSession.id == session_id, ClassSession.class_id == cls.id, ClassSession.tenant_id == cls.tenant_id)
        .first()
    )
    evt = None
    if cs is not None:
        evt = (
            db.query(ScheduledEvent)
            .filter(ScheduledEvent.id == cs.event_id, ScheduledEvent.tenant_id == cls.tenant_id,
                    ScheduledEvent.state.in_(attendance.HELD))
            .first()
        )
    if cs is None or evt is None:
        raise HTTPException(404, "Session not found")
    entry = (
        db.query(RollCallEntry)
        .filter(RollCallEntry.id == entry_id, RollCallEntry.class_id == cls.id, RollCallEntry.tenant_id == cls.tenant_id)
        .first()
    )
    if entry is None:
        raise HTTPException(404, "Roll-call entry not found")
    row = attendance.correct(db, cls, cs, evt, entry, body.status.value, body.note, user)
    db.commit()
    return AttendanceCellOut(session_id=cs.id, status=row.status, source=row.source, evidence_at=row.evidence_at,
                             note=row.note, corrected_at=row.corrected_at)


# ── Close-out ────────────────────────────────────────────────────────────────


@router.get(
    "/{class_id}/closeout", response_model=CloseoutOut, operation_id="getClassCloseout",
    summary="A class's pre-filled results",
)
def get_closeout(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_STAFF_READ),
) -> CloseoutOut:
    """Each Student's attendance, course standing and suggested outcome, and the result set
    at the last close-out. Pass/fail only on an Unclassified deployment.  **class:read**,
    then its staff only."""
    cls, _ = _staff(db, class_id, user)
    held, standing = closeout.prefill(db, cls)
    return CloseoutOut(
        class_id=cls.id, class_status=cls.status, numbers_shown=closeout.numbers_allowed(), sessions_held=len(held),
        closed_at=closeout.closed_at(db, cls),
        students=[StandingOut(
            entry_id=s.entry.id, email=s.entry.email, display_name=s.display_name, sessions_held=s.held,
            present=s.present, excused=s.excused, absent=s.absent, course_passed=s.course_passed,
            course_percent=s.percent, detections_credited=s.detections, suggested=s.suggested,
            outcome=s.result.outcome if s.result else None, comment=s.result.comment if s.result else None,
            decided_at=s.result.decided_at if s.result else None,
        ) for s in standing],
    )


@router.post(
    "/{class_id}/close", response_model=CloseoutOut, operation_id="closeClass",
    summary="Close a class with a result for every Student",
)
def close_class(
    class_id: uuid.UUID,
    body: CloseIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_CHANGE),
) -> CloseoutOut:
    """Its lead or an admin. Pass, fail or incomplete for every Student on the roll call
    (422 when one is missing). Results go to each Student's enrolment on the course; the
    class becomes read-only, its unstarted sessions are cancelled and its ranges torn down.
    Audited.  **class:create**"""
    cls = _editable(db, class_id, user)
    decisions = [closeout.Decision(r.entry_id, r.outcome.value, r.comment) for r in body.results]
    # A deadlock or serialization failure against the clock or an approver is 409, try again.
    with _lock_conflicts(db):
        cancels = closeout.close(db, cls, decisions, user)
        db.commit()
    background.add_task(scheduler.send_invites, cancels)
    db.refresh(cls)
    return get_closeout(class_id, db, user)


@router.post(
    "/{class_id}/reopen", response_model=CloseoutOut, operation_id="reopenClass",
    summary="Reopen a closed class",
)
def reopen_class(
    class_id: uuid.UUID,
    body: ReopenIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_CHANGE),
) -> CloseoutOut:
    """An admin of the class's tenant only, with a reason. Results stay until the next
    close-out. Audited (``class_reopen``).  **class:create**, then admin"""
    cls, _ = access.get_visible(db, class_id, user)
    closeout.reopen(db, cls, user, body.reason)
    db.commit()
    db.refresh(cls)
    return get_closeout(class_id, db, user)


@router.get(
    "/{class_id}/results.csv", response_class=Response, operation_id="exportClassResults",
    summary="A class's attendance and results as CSV",
    responses={200: {"content": {"text/csv": {"schema": {"type": "string"}}}}},
)
def export_results(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_STAFF_READ),
) -> Response:
    """One row per Student: each session's attendance, the suggestion and the result. Cells a
    spreadsheet would run as a formula are defused. Never a numeric grade.  **class:read**,
    then its staff only."""
    cls, _ = _staff(db, class_id, user)
    text = closeout.export_csv(db, cls)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in cls.name)[:60] or "class"
    return Response(content=text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{safe}-results.csv"',
                             "Cache-Control": "no-store"})
