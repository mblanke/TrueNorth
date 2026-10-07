"""Lab sessions: a student's own small range for a range activity (app/lab_sessions).

Two ways in, same operations:
  /lab-sessions/...   signed-in TrueNorth users: the student who owns the session, or
                      staff of the tenant with learning-record rights
  /lab-access/{id}    the lab page reached from a Moodle LTI launch, carrying the
                      session's own access token in ``X-Lab-Token`` (it opens nothing else)

Reading a session advances it, so a page that polls always sees current state.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..lab_sessions import service, tokens
from ..lab_sessions.models import LabSession
from ..rbac import Permission, require_permission, user_has_permission
from ..tenancy import tenant_uuid

router = APIRouter(tags=["lab-sessions"])


class LaunchIn(BaseModel):
    course_id: uuid.UUID
    activity_id: str = Field(pattern=r"^mod_[0-9]{3}$")


class EvidenceIn(BaseModel):
    kind: str = Field(min_length=1, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)


class ConsoleOut(BaseModel):
    node: str
    kind: str
    url: str
    expires_in: int


class LabSessionOut(BaseModel):
    id: uuid.UUID
    state: str
    release_id: uuid.UUID
    activity_id: str
    attempt: int
    error: str
    probes: list[dict[str, Any]]
    evidence_count: int
    readiness_seconds: float | None
    created_at: datetime | None
    ready_at: datetime | None
    idle_expires_at: datetime | None
    max_expires_at: datetime | None
    ended_at: datetime | None
    end_reason: str
    access_token: str | None = None


def _out(s: LabSession, token: str | None = None) -> LabSessionOut:
    return LabSessionOut(
        id=s.id,
        state=s.state,
        release_id=s.release_id,
        activity_id=s.activity_id,
        attempt=s.attempt,
        error=s.error,
        probes=json.loads(s.probes or "[]"),
        evidence_count=len(json.loads(s.evidence or "[]")),
        readiness_seconds=s.readiness_seconds,
        created_at=s.created_at,
        ready_at=s.ready_at,
        idle_expires_at=s.idle_expires_at,
        max_expires_at=s.max_expires_at,
        ended_at=s.ended_at,
        end_reason=s.end_reason,
        access_token=token,
    )


def _refused(exc: service.LabRefusedError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


def _uuid(value: str) -> uuid.UUID:
    return uuid.UUID(str(value))


def _session_for(db: Session, user: CurrentUser, session_id: uuid.UUID, *, staff_permission: Permission) -> LabSession:
    """The caller's own session, or one in their tenant when they hold the staff right.
    404 otherwise (never "exists but not yours")."""
    s = db.get(LabSession, session_id)
    if s is None or s.tenant_id != tenant_uuid(user):
        raise HTTPException(404, "lab session not found")
    if str(s.user_id) != str(user.id) and not user_has_permission(user, staff_permission):
        raise HTTPException(404, "lab session not found")
    return s


def _token_session(db: Session, session_id: uuid.UUID, token: str | None, *, act: bool = True) -> LabSession:
    """The session a lab token is for, checked against the session as it is now, not only
    as it was when the token was minted (CR1-17): a token outlives a lab that ended and an
    enrollment that was withdrawn. Reading an ended lab (``act=False``) stays allowed, so the
    page can say it ended; acting on it does not."""
    if not token:
        raise HTTPException(401, "X-Lab-Token is required")
    try:
        claims = tokens.verify(db, token, session_id)
    except tokens.LabTokenError as exc:
        raise HTTPException(401, str(exc)) from exc
    s = db.get(LabSession, session_id)
    if s is None or claims.get("uid") != str(s.user_id):
        raise HTTPException(404, "lab session not found")
    if _withdrawn(db, s):
        raise HTTPException(403, "your enrollment in this course has been withdrawn")
    if act and s.state in (*service.ENDING, *service.TERMINAL):
        raise HTTPException(410, "this lab has ended")
    return s


def _withdrawn(db: Session, s: LabSession) -> bool:
    from ..course_releases.models import CourseRelease
    from ..models import Enrollment, EnrollmentStatus

    release = db.get(CourseRelease, s.release_id)
    if release is None:
        return False
    mine = db.query(Enrollment).filter(Enrollment.user_id == s.user_id, Enrollment.course_id == release.course_id)
    statuses = {e.status for e in mine}
    return bool(statuses) and statuses <= {EnrollmentStatus.withdrawn}


def _do(db: Session, fn, session: LabSession, *args, busy_ok: bool = False, **kwargs) -> Any:
    """Run one operation on a session under its lease; its worker tasks go out after the
    commit. A refusal is the API's answer, nothing is half-written."""
    try:
        return service.run_locked(db, session, fn, *args, busy_ok=busy_ok, **kwargs)
    except service.LabRefusedError as exc:
        raise _refused(exc) from exc


# ── signed-in users ──────────────────────────────────────────────────


@router.post("/lab-sessions", response_model=LabSessionOut, status_code=status.HTTP_201_CREATED)
def launch_lab_session(
    body: LaunchIn, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)
) -> LabSessionOut:
    """Start (or return) the caller's lab for a range activity of a course. Launching again,
    refreshing or retrying returns the same session; never a second range."""

    staff = user_has_permission(user, Permission.LEARNING_RECORD_WRITE)
    try:
        with db.begin_nested():
            release = service.release_for_student(
                db, tenant_id=tenant_uuid(user), user_id=_uuid(user.id), course_id=body.course_id, auto_enroll=staff
            )
            session, _ = service.launch(
                db,
                tenant_id=tenant_uuid(user),
                user_id=_uuid(user.id),
                release_id=release.id,
                activity_id=body.activity_id,
            )
    except service.LabRefusedError as exc:
        db.info.pop(service.OUTBOX, None)
        raise _refused(exc) from exc
    db.commit()
    service.flush_outbox(db)
    return _out(session)


@router.get("/lab-sessions", response_model=list[LabSessionOut])
def list_lab_sessions(
    all_students: bool = False, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)
) -> list[LabSessionOut]:
    """The caller's labs; ``all_students`` (staff) lists the tenant's."""
    q = db.query(LabSession).filter(LabSession.tenant_id == tenant_uuid(user))
    if not (all_students and user_has_permission(user, Permission.LEARNING_RECORD_READ)):
        q = q.filter(LabSession.user_id == _uuid(user.id))
    return [_out(s) for s in q.order_by(LabSession.created_at.desc()).limit(200).all()]


@router.get("/lab-sessions/{session_id}", response_model=LabSessionOut)
def get_lab_session(
    session_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)
) -> LabSessionOut:
    s = _session_for(db, user, session_id, staff_permission=Permission.LEARNING_RECORD_READ)
    return _out(_do(db, service.advance, s, busy_ok=True))


@router.post("/lab-sessions/{session_id}/heartbeat", response_model=LabSessionOut)
def heartbeat_lab_session(
    session_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)
) -> LabSessionOut:
    s = _session_for(db, user, session_id, staff_permission=Permission.LEARNING_RECORD_WRITE)
    return _out(_do(db, service.touch, s))


@router.post("/lab-sessions/{session_id}/reset", response_model=LabSessionOut)
def reset_lab_session(
    session_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)
) -> LabSessionOut:
    s = _session_for(db, user, session_id, staff_permission=Permission.LEARNING_RECORD_WRITE)
    return _out(_do(db, service.reset, s))


@router.post("/lab-sessions/{session_id}/end", response_model=LabSessionOut)
def end_lab_session(
    session_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)
) -> LabSessionOut:
    s = _session_for(db, user, session_id, staff_permission=Permission.LEARNING_RECORD_WRITE)
    return _out(_do(db, service.end, s, reason="completed"))


@router.post("/lab-sessions/{session_id}/console", response_model=ConsoleOut)
def open_lab_console(
    session_id: uuid.UUID,
    node: str | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> ConsoleOut:
    s = _session_for(db, user, session_id, staff_permission=Permission.LEARNING_RECORD_WRITE)
    return ConsoleOut(**_do(db, service.console, s, node))


@router.post("/lab-sessions/{session_id}/evidence", response_model=LabSessionOut)
def add_lab_evidence(
    session_id: uuid.UUID,
    body: EvidenceIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> LabSessionOut:
    """Keep a submission or validator result with the session; it outlives the VMs."""
    s = _session_for(db, user, session_id, staff_permission=Permission.LEARNING_RECORD_WRITE)
    staff = user_has_permission(user, Permission.LEARNING_RECORD_WRITE)
    if body.kind != "submission" and not staff:
        raise HTTPException(403, "students submit work (kind 'submission'); results come from staff and validators")
    item = {"kind": body.kind, "data": body.data, "by": str(user.id)}
    return _out(_do(db, service.add_evidence, s, item))


@router.post("/lab-sessions/reconcile")
def reconcile_lab_sessions(
    db: Session = Depends(get_db), user: CurrentUser = Depends(require_permission(Permission.INFRA_CONTROL))
) -> dict[str, int]:
    """Ask the worker to remove anything this tenant's finished labs left on the hypervisor."""
    return {"ranges_checked": service.reconcile_all(db, tenant_uuid(user))}


# ── the lab page after an LTI launch ─────────────────────────────────


@router.get("/lab-access/{session_id}", response_model=LabSessionOut)
def get_lab_by_token(
    session_id: uuid.UUID, x_lab_token: str | None = Header(None), db: Session = Depends(get_db)
) -> LabSessionOut:
    return _out(_do(db, service.advance, _token_session(db, session_id, x_lab_token, act=False), busy_ok=True))


@router.post("/lab-access/{session_id}/heartbeat", response_model=LabSessionOut)
def heartbeat_lab_by_token(
    session_id: uuid.UUID, x_lab_token: str | None = Header(None), db: Session = Depends(get_db)
) -> LabSessionOut:
    return _out(_do(db, service.touch, _token_session(db, session_id, x_lab_token)))


@router.post("/lab-access/{session_id}/reset", response_model=LabSessionOut)
def reset_lab_by_token(
    session_id: uuid.UUID, x_lab_token: str | None = Header(None), db: Session = Depends(get_db)
) -> LabSessionOut:
    return _out(_do(db, service.reset, _token_session(db, session_id, x_lab_token)))


@router.post("/lab-access/{session_id}/end", response_model=LabSessionOut)
def end_lab_by_token(
    session_id: uuid.UUID, x_lab_token: str | None = Header(None), db: Session = Depends(get_db)
) -> LabSessionOut:
    return _out(_do(db, service.end, _token_session(db, session_id, x_lab_token), reason="completed"))


@router.post("/lab-access/{session_id}/console", response_model=ConsoleOut)
def open_console_by_token(
    session_id: uuid.UUID,
    node: str | None = None,
    x_lab_token: str | None = Header(None),
    db: Session = Depends(get_db),
) -> ConsoleOut:
    return ConsoleOut(**_do(db, service.console, _token_session(db, session_id, x_lab_token), node))
