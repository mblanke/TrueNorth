"""Is this grant's holder still allowed this console? Asked at connect and at every renewal.

A grant is issued after the rules of ``classes/consoles.py`` (or the lab-session rules) said
yes; it then lives minutes and a socket on it much longer. The question is asked again, with
the same rules, while the socket is open: the session window closed, the Student was taken off
the roll call or forfeited the seat, an observer's consoles were switched off, the user lost
the role or was disabled, the range stopped, the lab session ended. A "no" closes the socket
with a fixed reason (``router.py``); the socket never outlives the window it was opened in
(``Verdict.until``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..classes import consoles
from ..classes.schemas import ClassRole
from ..lab_sessions import service as lab_service
from ..lab_sessions.models import ACTIVE, READY, LabSession
from ..models import Range, User
from ..rbac import Permission, user_has_permission
from . import service
from .models import ConsoleGrant

FORBIDDEN, GONE = 1008, 4404
NOT_ALLOWED = "You can no longer open this console"
NOT_RUNNING = "The machine is not running"


@dataclass(frozen=True)
class Verdict:
    ok: bool
    code: int = 0
    reason: str = ""
    until: float | None = None  # epoch seconds the socket must end by (the window's end)


def _no(reason: str = NOT_ALLOWED, code: int = FORBIDDEN) -> Verdict:
    return Verdict(False, code, reason)


def _epoch(t: datetime | None) -> float | None:
    return None if t is None else (t if t.tzinfo else t.replace(tzinfo=UTC)).timestamp()


def check(db: Session, grant: ConsoleGrant, user: CurrentUser) -> Verdict:
    """The cheap predicate for the grant's audience. Reads the user, range and session as they
    are now; never the grant's own say-so."""
    uid = uuid.UUID(str(user.id))
    # Never answer from the session's identity map: production sessions keep objects after a
    # commit (expire_on_commit=False), so a revocation made since would not be seen.
    db.expire_all()
    # tenant-safe: the grant's user, in the grant's tenant (also the connecting user's, claim).
    row = db.query(User).filter(User.id == uid, User.tenant_id == grant.tenant_id).first()
    if row is None or not row.is_active or row.deleted_at is not None:
        return _no()
    user = user.model_copy(update={"role": row.role})  # the role it is now
    rng = (db.query(Range)
           .filter(Range.id == grant.range_id, Range.tenant_id == grant.tenant_id, Range.deleted_at.is_(None))
           .first())
    if rng is None or rng.state not in consoles.USABLE:
        return _no(NOT_RUNNING, GONE)
    if grant.lab_session_id is not None:
        return _lab(db, grant, user)
    if grant.class_id is None or grant.event_id is None:
        return _no()
    try:
        if grant.audience == service.STUDENT:
            return _student(db, grant, user, rng)
        return _staff(db, grant, user, rng)
    except HTTPException:  # the rule that issued it now says no (404 or 403)
        return _no()


def _vm_machine(rng: Range, vm_name: str) -> str | None:
    vm = next((v for v in consoles.vms_of(rng) if str(v.get("name") or "") == vm_name), None)
    return None if vm is None else consoles.machine_of(vm, rng.id)


def _student(db: Session, grant: ConsoleGrant, user: CurrentUser, rng: Range) -> Verdict:
    """A Student's own seat or shared range, a session of the class live now, the roll call
    and seat as ``ranges.student_participation`` reads them, a machine they are handed."""
    found = consoles.student_range(db, user, grant.range_id)
    if found.session is None or found.class_id != grant.class_id or found.session.id != grant.event_id:
        return _no("Your session is not open")  # the grant is bound to the session it was issued for
    if _vm_machine(rng, grant.vm_name) not in consoles.student_machines(db, rng):
        return _no()
    return Verdict(True, until=_epoch(found.session.end_time))


def _staff(db: Session, grant: ConsoleGrant, user: CurrentUser, rng: Range) -> Verdict:
    live = consoles.live_session(db, user, grant.class_id, grant.event_id)
    if live.cls.status != "open" or not consoles.in_window(live.evt, datetime.now(UTC)):
        return _no("The session is not open")
    observer = live.role == ClassRole.observer
    if observer != (grant.audience == service.OBSERVER) or not consoles.may_open_console(live.role):
        return _no()
    if observer and _vm_machine(rng, grant.vm_name) not in consoles.student_machines(db, rng):
        return _no()
    _entries, seats, shared = consoles.session_ranges(db, live)
    if grant.range_id not in {r.id for r in seats.values()} | ({shared.id} if shared is not None else set()):
        return _no()
    return Verdict(True, until=_epoch(live.evt.end_time))


def _lab(db: Session, grant: ConsoleGrant, user: CurrentUser) -> Verdict:
    """A lab session still ready or active, on this range; its Student, or staff who may act on it."""
    # tenant-safe: the session of the grant's own tenant.
    s = (db.query(LabSession)
         .filter(LabSession.id == grant.lab_session_id, LabSession.tenant_id == grant.tenant_id).first())
    if s is None or s.state not in (READY, ACTIVE) or s.range_id != grant.range_id:
        return _no("The lab has ended")
    if str(s.user_id) == str(user.id) and lab_service.enrollment_closed(db, s):
        return _no("Your enrollment in this course has closed")  # as the issuing route
    if str(s.user_id) != str(user.id) and not user_has_permission(user, Permission.LEARNING_RECORD_WRITE):
        return _no()
    return Verdict(True, until=_epoch(s.max_expires_at))
