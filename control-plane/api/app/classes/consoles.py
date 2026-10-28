"""Browser consoles on a class's ranges: the Student's own, and the observers' live view
(ADR 0014 §19-20; issues #313, #321).

**Who gets a console, on what, and when.**

=========================  ============================================  ====================
Who                        Which machines                                When
=========================  ============================================  ====================
a Student (``student``)    their own seat (an individual session), or    a session of the
                           the class's shared range while a collective   class using that
                           session booking it is under way: exactly      range is live, or
                           ``ranges.student_participation``, the rule    its provisioning
                           of their credentials. Only the machines they  lead before it has
                           are handed accounts on (``student_visible``   been reached (the
                           and set by the last build): the same          shared range: under
                           machines on every team                        way only)
the class's observers      any machine of a range the session uses       the session's window
(``observer``)             (every seat, or its shared range)             as above
its lead, assistants and   as observers                                  as above
admins (``staff``)
=========================  ============================================  ====================

Everything else is refused: another Student's seat or another class's range is 404, as for
credentials; a team has no machines of its own (no template says which machine is whose), so
a Student on a collective range reaches the same Student machines as every other Student
there. A platform observer reaches only the classes they are an observer of (``ClassStaff``,
``access.role_in``), never another class of their tenant, never another tenant's.

**The ticket** comes from the range's own console adapter (``console_backends``, ADR 0001):
the vendor SDK is only there. An observer gets a view-only console where the adapter can
enforce one (``supports_view_only``); vSphere WebMKS cannot (its ticket carries no
read-only mode), so an observer gets a normal console marked ``observed``, and the page says
so.

**Every ticket issued is audited** (``range_console_open``): who, the range, machine and VM,
why (``student``, ``observer`` or ``staff``), the class, the session, and how the Student takes
part; never the ticket or its URL. A Student's console is also their attendance evidence, as
listing the accounts is (``attendance.note_range_open``).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..models import AuditLog, Range, RangeState, User
from ..scheduler.capacity import PROVISION_LEAD
from ..scheduler.lifecycle import HOLDING
from ..scheduler.models import ScheduledEvent
from ..tenancy import tenant_uuid
from . import access, attendance, teams
from .models import (
    OFF_ROLL_CALL,
    Class,
    ClassRange,
    ClassRangeKind,
    ClassSession,
    ClassStatus,
    RangeMode,
    RollCallEntry,
)
from .schemas import ClassRole

AUDIT_ACTION = "range_console_open"
# Why a ticket was issued, in its audit row.
AS_STUDENT, AS_OBSERVER, AS_STAFF = "student", "observer", "staff"
# A range whose machines take a console: built and powered on.
USABLE = (RangeState.ready, RangeState.running)
# What a staff reader is shown as "last seen" on a Student's range.
SEEN_ACTIONS = (*attendance.EVIDENCE_ACTIONS, AUDIT_ACTION)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# ── Machines ─────────────────────────────────────────────────────────────────


def vms_of(rng: Range | None) -> list[dict[str, Any]]:
    """The range's VMs as its provisioner recorded them (never raises on odd output)."""
    if rng is None:
        return []
    try:
        vms = json.loads(rng.provisioner_output or "{}").get("vms") or []
    except (ValueError, AttributeError):
        return []
    return [vm for vm in vms if isinstance(vm, dict)] if isinstance(vms, list) else []


def machine_of(vm: dict[str, Any], range_id: Any) -> str:
    """A VM's machine name: the template node id, as accounts name it (worker/range_accounts.py)."""
    return str(vm.get("node_id") or str(vm.get("name") or "").removeprefix(f"{str(range_id)[:8]}-"))


def machines(rng: Range) -> list[dict[str, Any]]:
    """``{machine, name, power}`` per VM of the range, in its recorded order."""
    return [
        {"machine": machine_of(vm, rng.id), "name": str(vm.get("name") or ""), "power": vm.get("status") or None}
        for vm in vms_of(rng)
        if machine_of(vm, rng.id)
    ]


def student_machines(db: Session, rng: Range) -> set[str]:
    """The machines a Student is handed accounts on: what their credentials panel lists."""
    from ..range_credentials import service as credentials

    return {r.machine for r in credentials.visible_accounts(db, rng, credentials.STUDENT)}


def _vm(rng: Range, machine: str) -> dict[str, Any]:
    vm = next((v for v in vms_of(rng) if machine_of(v, rng.id) == machine), None)
    if vm is None or not vm.get("vm_id"):
        raise HTTPException(409, f"{machine} has no machine built yet")
    return vm


def view_only_supported(rng: Range) -> bool:
    from ..console_backends import console_backend_for

    backend = console_backend_for(rng.provisioner_backend)
    return bool(backend is not None and backend.supports_view_only)


def _open(db: Session, rng: Range, vm: dict[str, Any], *, view_only: bool) -> dict[str, Any]:
    """A ticket from the range's own console adapter (ADR 0001). 502 when it cannot give one."""
    from ..console_backends import ConsoleError, get_console_backend

    if rng.state not in USABLE:
        raise HTTPException(409, "The range's machines are not running; the console opens once they are")
    try:
        backend = get_console_backend(rng.provisioner_backend)
        if view_only and backend.supports_view_only:
            return {**backend.open_view_only(vm, db=db, rng=rng), "view_only": True}
        return {**backend.open(vm, db=db, rng=rng), "view_only": False}
    except (ValueError, ConsoleError) as exc:
        raise HTTPException(502, str(exc)) from exc


def _audit(db: Session, user: CurrentUser, rng: Range, why: str, vm: dict[str, Any], machine: str,
           context: dict[str, Any]) -> None:
    """One row per ticket issued: who, which VM, why. Never the ticket or its URL."""
    detail = {"as": why, "range_id": str(rng.id), "machine": machine, "vm_id": str(vm.get("vm_id") or ""),
              "vm_name": str(vm.get("name") or ""), **{k: str(v) for k, v in context.items() if v is not None}}
    db.add(AuditLog(user_id=uuid.UUID(str(user.id)), tenant_id=rng.tenant_id, action=AUDIT_ACTION,
                    resource_type="range", resource_id=str(rng.id), detail=json.dumps(detail, sort_keys=True)[:2000]))


# ── Session windows ──────────────────────────────────────────────────────────


def in_window(evt: ScheduledEvent, now: datetime) -> bool:
    """Approved and not over, its provisioning lead reached: live, or about to be."""
    return (evt.state in HOLDING and _aware(evt.start_time) - PROVISION_LEAD <= now < _aware(evt.end_time))


def _window_session(db: Session, class_id: uuid.UUID, tenant_id: uuid.UUID, *, mode: str, now: datetime,
                    template_id: uuid.UUID | None = None, range_id: uuid.UUID | None = None) -> ScheduledEvent | None:
    """The class's session of ``mode`` on that template (a seat) or booking that range (shared)
    whose window is open now; None when there is none."""
    q = (
        db.query(ScheduledEvent)
        .join(ClassSession, ClassSession.event_id == ScheduledEvent.id)
        .filter(
            ClassSession.class_id == class_id,
            ClassSession.tenant_id == tenant_id,
            ClassSession.range_mode == mode,
            ScheduledEvent.tenant_id == tenant_id,
            ScheduledEvent.state.in_(HOLDING),
            ScheduledEvent.start_time <= now + PROVISION_LEAD,
            ScheduledEvent.end_time > now,
        )
    )
    q = q.filter(ScheduledEvent.template_id == template_id) if template_id is not None else q
    q = q.filter(ScheduledEvent.range_id == range_id) if range_id is not None else q
    return q.order_by(ScheduledEvent.start_time).first()


# ── A Student's own console (#313) ───────────────────────────────────────────


@dataclass
class StudentRange:
    rng: Range
    participation: dict[str, str]
    class_id: uuid.UUID
    session: ScheduledEvent | None  # the session whose window is open now


def student_range(db: Session, user: CurrentUser, range_id: uuid.UUID, now: datetime | None = None) -> StudentRange:
    """A class range the Student takes part in (``ranges.student_participation``), in their
    tenant, or 404; with the session whose window is open now, if any."""
    from . import ranges

    now = now or _now()
    rng = (
        db.query(Range)
        .filter(Range.id == range_id, Range.tenant_id == tenant_uuid(user), Range.deleted_at.is_(None))
        .first()
    )
    if rng is None:
        raise HTTPException(404, "Range not found")
    if ranges.observes_only(db, user, rng):
        raise HTTPException(403, "Observers of this range's class do not get a Student's console")
    part = ranges.student_participation(db, uuid.UUID(str(user.id)), rng, now)
    if part is None:
        raise HTTPException(404, "Range not found")
    if "class_seat_id" in part:
        # tenant-safe: the seat student_participation just matched, in the range's tenant.
        row = (db.query(ClassRange)
               .filter(ClassRange.id == uuid.UUID(part["class_seat_id"]), ClassRange.tenant_id == rng.tenant_id)
               .first())
        if row is None:
            raise HTTPException(404, "Range not found")
        session = _window_session(db, row.class_id, rng.tenant_id, mode=RangeMode.individual.value,
                                  template_id=row.template_id, now=now)
        return StudentRange(rng, part, row.class_id, session)
    class_id = uuid.UUID(part["class_id"])
    session = _window_session(db, class_id, rng.tenant_id, mode=RangeMode.collective.value, range_id=rng.id, now=now)
    return StudentRange(rng, part, class_id, session)


def student_console(db: Session, user: CurrentUser, range_id: uuid.UUID, machine: str) -> dict[str, Any]:
    """A console ticket on one of the Student's own machines, during their session. Audited,
    and their attendance evidence. Commits."""
    found = student_range(db, user, range_id)
    rng = found.rng
    if machine not in student_machines(db, rng):
        raise HTTPException(404, "No console on that machine for you")
    if found.session is None:
        raise HTTPException(403, "Your console opens during your session (from shortly before it starts)")
    vm = _vm(rng, machine)
    ticket = _open(db, rng, vm, view_only=False)
    _audit(db, user, rng, AS_STUDENT, vm, machine,
           {"class_id": found.class_id, "event_id": found.session.id, **found.participation})
    attendance.note_range_open(db, uuid.UUID(str(user.id)), rng, found.participation)
    db.commit()
    return {"range_id": rng.id, "machine": machine, "observed": False, **ticket}


# ── The live view of a class session (ADR 0014 §19) ──────────────────────────


@dataclass
class LiveSession:
    cls: Class
    role: ClassRole
    link: ClassSession
    evt: ScheduledEvent


def live_session(db: Session, user: CurrentUser, class_id: uuid.UUID, event_id: uuid.UUID) -> LiveSession:
    """The class's session, for its lead, assistants, observers and admins only. Another
    tenant's class, or one the caller has no part in, is 404; its Students get 403."""
    cls, role = access.get_visible(db, class_id, user)
    access.require_staff(role)
    link, evt = teams.session_of(db, cls, event_id)
    return LiveSession(cls, role, link, evt)


def session_ranges(db: Session, live: LiveSession) -> tuple[list[RollCallEntry], dict, Range | None]:
    """The roll call, each Student's seat range for an individual session (entry id ->
    Range), and the shared range a collective session books."""
    cls, evt = live.cls, live.evt
    entries = (
        db.query(RollCallEntry)
        .filter(RollCallEntry.class_id == cls.id, RollCallEntry.tenant_id == cls.tenant_id,
                RollCallEntry.status.notin_(OFF_ROLL_CALL))
        .order_by(RollCallEntry.email)
        .all()
    )
    seats: dict[uuid.UUID, Range] = {}
    shared = None
    if live.link.range_mode == RangeMode.individual.value:
        if evt.template_id is not None and entries:
            rows = (
                db.query(ClassRange)
                .filter(ClassRange.class_id == cls.id, ClassRange.tenant_id == cls.tenant_id,
                        ClassRange.kind == ClassRangeKind.seat.value, ClassRange.template_id == evt.template_id,
                        ClassRange.torn_down_at.is_(None), ClassRange.range_id.isnot(None),
                        ClassRange.entry_id.in_([e.id for e in entries]))
                .all()
            )
            rngs = {r.id: r for r in db.query(Range).filter(
                Range.id.in_([r.range_id for r in rows]), Range.tenant_id == cls.tenant_id,
                Range.deleted_at.is_(None))} if rows else {}
            seats = {r.entry_id: rngs[r.range_id] for r in rows if r.range_id in rngs}
    elif evt.range_id is not None:
        shared = (db.query(Range)
                  .filter(Range.id == evt.range_id, Range.tenant_id == cls.tenant_id, Range.deleted_at.is_(None))
                  .first())
    return entries, seats, shared


def last_seen(db: Session, tenant_id: uuid.UUID, user_ids: list[uuid.UUID], range_ids: list[uuid.UUID],
              since: datetime) -> dict[tuple[uuid.UUID, str], datetime]:
    """(user, range id) -> the last time the Student opened that range (accounts, a password
    or a console) since ``since``, in one query."""
    if not user_ids or not range_ids:
        return {}
    rows = (
        db.query(AuditLog.user_id, AuditLog.resource_id, func.max(AuditLog.timestamp))
        .filter(
            AuditLog.tenant_id == tenant_id,
            AuditLog.resource_type == "range",
            AuditLog.resource_id.in_([str(r) for r in range_ids]),
            AuditLog.user_id.in_(user_ids),
            AuditLog.action.in_(SEEN_ACTIONS),
            AuditLog.timestamp >= since,
        )
        .group_by(AuditLog.user_id, AuditLog.resource_id)
        .all()
    )
    return {(uid, rid): _aware(at) for uid, rid, at in rows if at is not None}


def names_of(db: Session, tenant_id: uuid.UUID, entries: list[RollCallEntry]) -> dict[uuid.UUID, str]:
    ids = {e.user_id for e in entries if e.user_id}
    if not ids:
        return {}
    return dict(db.query(User.id, User.display_name).filter(User.id.in_(ids), User.tenant_id == tenant_id).all())


def staff_console(db: Session, user: CurrentUser, live: LiveSession, range_id: uuid.UUID,
                  machine: str) -> dict[str, Any]:
    """A console ticket on a machine of one of the session's ranges, for its observers (view
    only where the adapter can enforce it, else marked observed) and its lead, assistants and
    admins, while the session's window is open. Audited. Commits."""
    if live.cls.status != ClassStatus.open.value:
        raise HTTPException(409, "The class is not open")
    if not in_window(live.evt, _now()):
        raise HTTPException(403, "Consoles open during the session (from shortly before it starts)")
    entries, seats, shared = session_ranges(db, live)
    rng = shared if shared is not None and shared.id == range_id else next(
        (r for r in seats.values() if r.id == range_id), None)
    if rng is None:
        raise HTTPException(404, "That range is not one of this session's")
    vm = _vm(rng, machine)
    observer = live.role == ClassRole.observer
    ticket = _open(db, rng, vm, view_only=observer)
    _audit(db, user, rng, AS_OBSERVER if observer else AS_STAFF, vm, machine,
           {"class_id": live.cls.id, "class_role": live.role.value, "event_id": live.evt.id,
            "view_only": ticket["view_only"]})
    db.commit()
    return {"range_id": rng.id, "machine": machine, "observed": observer and not ticket["view_only"], **ticket}
