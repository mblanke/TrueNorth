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

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..lab_sessions import capacity_gate, service, tokens
from ..lab_sessions.models import LabSession
from ..range_credentials import service as cred_service
from ..range_credentials.router import RangeCredentialOut, RevealedCredentialOut, account_out
from ..rbac import Permission, require_permission, user_has_permission
from ..tenancy import tenant_uuid

router = APIRouter(tags=["lab-sessions"])


class LaunchIn(BaseModel):
    course_id: uuid.UUID
    activity_id: str = Field(pattern=r"^mod_[0-9]{3}$")


class HeartbeatIn(BaseModel):
    active: bool = Field(
        False,
        description="The Student interacted with the lab page (input, focus, console) since the last beat. "
        "Only that keeps the lab open (ADR 0014 §18); an open tab nobody uses does not.",
    )


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
    ends_with_class: bool = Field(
        False, description="The lab draws on the Student's class session and ends with it (max_expires_at).")
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
        ends_with_class=s.booking_id is not None,
        access_token=token,
    )


def _refused(exc: service.LabRefusedError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


def _shown(db: Session, s: LabSession) -> LabSessionOut:
    """``_out``, with when labs are next free for a lab queued for capacity (read here, after
    the operation committed: never under the site-wide lock; at most every 30 s per lab)."""
    out = _out(s)
    if s.state == service.QUEUED and s.error == capacity_gate.BUSY:
        try:
            profile = service._profile(db, s)
            out.error = capacity_gate.queued_message(
                db, s, capacity_gate.demand(profile), capacity_gate.lifetime(profile), service._now()
            )
        except service.LabRefusedError:
            pass
    return out


def _beat(db: Session, s: LabSession, body: HeartbeatIn | None) -> LabSessionOut:
    """A heartbeat: activity keeps the lab open (touch); a beat with none only reads it."""
    if body is not None and body.active:
        return _shown(db, _do(db, service.touch, s))
    return _shown(db, _do(db, service.advance, s, busy_ok=True))


def _uuid(value: str) -> uuid.UUID:
    return uuid.UUID(str(value))


def _session_for(
    db: Session, user: CurrentUser, session_id: uuid.UUID, *, staff_permission: Permission, act: bool = False
) -> LabSession:
    """The caller's own session, or one in their tenant when they hold the staff right.
    404 otherwise (never "exists but not yours"). A student whose enrollment has closed
    (withdrawn, failed) cannot act on their lab any more, signed in or by token (CR1-17)."""
    # tenant-safe: the tenant is compared on the very next line (404 on mismatch).
    s = db.get(LabSession, session_id)
    if s is None or s.tenant_id != tenant_uuid(user):
        raise HTTPException(404, "lab session not found")
    if str(s.user_id) != str(user.id) and not user_has_permission(user, staff_permission):
        raise HTTPException(404, "lab session not found")
    if act and str(s.user_id) == str(user.id) and _enrollment_closed(db, s):
        raise HTTPException(403, "your enrollment in this course has closed")
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
    # tenant-safe: no signed-in user here; the signed lab token was verified for this
    # session_id above and its uid must be the session's owner.
    s = db.get(LabSession, session_id)
    if s is None or claims.get("uid") != str(s.user_id):
        raise HTTPException(404, "lab session not found")
    if _enrollment_closed(db, s):
        raise HTTPException(403, "your enrollment in this course has closed")
    if act and s.state in (*service.ENDING, *service.TERMINAL):
        raise HTTPException(410, "this lab has ended")
    return s


def _enrollment_closed(db: Session, s: LabSession) -> bool:
    """Every enrollment of the student in the lab's course is withdrawn or failed, as
    release_for_student counts a closed enrollment. No enrollment (staff) is not closed."""
    from ..course_releases.models import CourseRelease
    from ..models import Enrollment, EnrollmentStatus

    # tenant-safe: the release a session (already tenant- or token-checked) points at.
    release = db.get(CourseRelease, s.release_id)
    if release is None:
        return False
    mine = db.query(Enrollment).filter(Enrollment.user_id == s.user_id, Enrollment.course_id == release.course_id)
    statuses = {e.status for e in mine}
    return bool(statuses) and statuses <= {EnrollmentStatus.withdrawn, EnrollmentStatus.failed}


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
            session, _ = service.launch_for_course(
                db, tenant_id=tenant_uuid(user), user_id=_uuid(user.id), course_id=body.course_id,
                activity_id=body.activity_id, auto_enroll=staff,
            )
    except service.LabsBusyError as exc:
        db.info.pop(service.OUTBOX, None)
        db.rollback()  # lets the site-wide lock go before the forecast is computed
        raise HTTPException(exc.status, exc.message(db)) from exc
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
    return _shown(db, _do(db, service.advance, s, busy_ok=True))


@router.post("/lab-sessions/{session_id}/heartbeat", response_model=LabSessionOut)
def heartbeat_lab_session(
    session_id: uuid.UUID,
    body: HeartbeatIn | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> LabSessionOut:
    s = _session_for(db, user, session_id, staff_permission=Permission.LEARNING_RECORD_WRITE, act=True)
    return _beat(db, s, body)


@router.post("/lab-sessions/{session_id}/reset", response_model=LabSessionOut)
def reset_lab_session(
    session_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)
) -> LabSessionOut:
    s = _session_for(db, user, session_id, staff_permission=Permission.LEARNING_RECORD_WRITE, act=True)
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
    s = _session_for(db, user, session_id, staff_permission=Permission.LEARNING_RECORD_WRITE, act=True)
    return ConsoleOut(**_do(db, service.console, s, node))


@router.post("/lab-sessions/{session_id}/evidence", response_model=LabSessionOut)
def add_lab_evidence(
    session_id: uuid.UUID,
    body: EvidenceIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> LabSessionOut:
    """Keep a submission or validator result with the session; it outlives the VMs."""
    s = _session_for(db, user, session_id, staff_permission=Permission.LEARNING_RECORD_WRITE, act=True)
    staff = user_has_permission(user, Permission.LEARNING_RECORD_WRITE)
    if body.kind != "submission" and not staff:
        raise HTTPException(403, "students submit work (kind 'submission'); results come from staff and validators")
    item = {"kind": body.kind, "data": body.data, "by": str(user.id)}
    return _out(_do(db, service.add_evidence, s, item))


class LabCredentialsOut(BaseModel):
    lab_session_id: uuid.UUID
    accounts: list[RangeCredentialOut] = Field(
        description="The accounts on the lab's machines, without passwords; empty until the lab is ready")


def _lab_range(db: Session, s: LabSession):
    """The session's range while the Student can use it (ready or active), else None."""
    from ..models import Range

    if s.state not in (service.READY, service.ACTIVE) or s.range_id is None:
        return None
    # tenant-safe: the session's own range, created by app/lab_sessions in its tenant.
    return db.get(Range, s.range_id)


def _lab_credentials(db: Session, s: LabSession) -> LabCredentialsOut:
    rng = _lab_range(db, s)
    rows = cred_service.visible_accounts(db, rng, cred_service.STUDENT) if rng is not None else []
    return LabCredentialsOut(lab_session_id=s.id, accounts=[account_out(r, set()) for r in rows])


def _lab_reveal(db: Session, s: LabSession, credential_id: uuid.UUID, via: str) -> RevealedCredentialOut:
    rng = _lab_range(db, s)
    if rng is None:
        raise HTTPException(404, "Account not found")
    row = cred_service.account(db, rng, credential_id, cred_service.STUDENT)
    password = cred_service.reveal(db, rng, row, cred_service.STUDENT, s.user_id, via,
                                   {"lab_session_id": str(s.id)})
    db.commit()
    return RevealedCredentialOut(id=row.id, machine=row.machine, username=row.username, password=password)


def _own_session(db: Session, user: CurrentUser, session_id: uuid.UUID) -> LabSession:
    """The caller's own session (404 otherwise, staff included: they read lab accounts on
    the range, GET /ranges/{id}/credentials, with range:credentials_read)."""
    s = _session_for(db, user, session_id, staff_permission=Permission.LEARNING_RECORD_WRITE, act=True)
    if str(s.user_id) != str(user.id):
        raise HTTPException(404, "lab session not found")
    return s


@router.get("/lab-sessions/{session_id}/credentials", response_model=LabCredentialsOut)
def get_lab_credentials(
    session_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)
) -> LabCredentialsOut:
    """The Student's own lab's accounts (ADR 0013 §3: generated per lab, new after every
    reset), without passwords. Only the session's owner."""
    return _lab_credentials(db, _own_session(db, user, session_id))


@router.post("/lab-sessions/{session_id}/credentials/{credential_id}/reveal", response_model=RevealedCredentialOut)
def reveal_lab_credential(
    response: Response,
    session_id: uuid.UUID,
    credential_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> RevealedCredentialOut:
    """One password of the Student's own lab. Audited (``range_credential_reveal``)."""
    out = _lab_reveal(db, _own_session(db, user, session_id), credential_id, "lab_session")
    response.headers["Cache-Control"] = "no-store"
    return out


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
    return _shown(db, _do(db, service.advance, _token_session(db, session_id, x_lab_token, act=False), busy_ok=True))


@router.post("/lab-access/{session_id}/heartbeat", response_model=LabSessionOut)
def heartbeat_lab_by_token(
    session_id: uuid.UUID,
    body: HeartbeatIn | None = None,
    x_lab_token: str | None = Header(None),
    db: Session = Depends(get_db),
) -> LabSessionOut:
    # Not an action on an ended lab (touch does nothing then): the answer tells the page the
    # lab ended. A 410 here left the page saying "running" (it polls only while waiting).
    return _beat(db, _token_session(db, session_id, x_lab_token, act=False), body)


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


@router.get("/lab-access/{session_id}/credentials", response_model=LabCredentialsOut)
def get_lab_credentials_by_token(
    session_id: uuid.UUID, x_lab_token: str | None = Header(None), db: Session = Depends(get_db)
) -> LabCredentialsOut:
    return _lab_credentials(db, _token_session(db, session_id, x_lab_token))


@router.post("/lab-access/{session_id}/credentials/{credential_id}/reveal", response_model=RevealedCredentialOut)
def reveal_lab_credential_by_token(
    response: Response,
    session_id: uuid.UUID,
    credential_id: uuid.UUID,
    x_lab_token: str | None = Header(None),
    db: Session = Depends(get_db),
) -> RevealedCredentialOut:
    """As the signed-in reveal; audited as the session's owner (``via: lab_token``)."""
    out = _lab_reveal(db, _token_session(db, session_id, x_lab_token), credential_id, "lab_token")
    response.headers["Cache-Control"] = "no-store"
    return out
