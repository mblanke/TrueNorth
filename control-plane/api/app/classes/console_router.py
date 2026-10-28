"""Consoles on a class's ranges and a class session's live view (ADR 0014 §19-20; #313, #321).

=====================================================  ========================================
Endpoint                                               Who
=====================================================  ========================================
GET  /classes/attending/ranges/{range_id}/consoles     class:attend: a Student's own class
                                                       range (``ranges.student_participation``)
POST /classes/attending/ranges/{range_id}/consoles/    as above, during their session; one of
     {vm}                                              their machines. Audited; attendance
GET  /classes/{id}/sessions/{event_id}/live            the class's lead, assistants, observers
                                                       and admins (``access.role_in``)
POST /classes/{id}/sessions/{event_id}/live/console    as above, during the session; observers
                                                       the Students' machines only. Audited
=====================================================  ========================================

The rules are ``consoles.py``'s docstring. Another tenant's class or range, another Student's
seat and a class the caller has no part in are 404; a Student asking for the live view is
403. No endpoint here carries a credential: a console ticket is single-use and expires
within a minute.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Path
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import Range, TeamMembership
from ..rbac import Permission, require_permission
from ..scheduler.schemas import as_utc
from . import consoles, teams
from .console_schemas import (
    MACHINE,
    ClassConsoleIn,
    ClassConsoleOut,
    ClassLiveOut,
    ConsoleMachineOut,
    LiveRangeOut,
    LiveStudentOut,
    LiveTeamOut,
    MyRangeConsolesOut,
)

router = APIRouter(prefix="/classes", tags=["classes"])

_DENIED = {404: {"description": "Not a class range of the caller's (another Student's seat, another tenant's)"}}


# ── A Student's own console (#313) ───────────────────────────────────────────


@router.get(
    "/attending/ranges/{range_id}/consoles", response_model=MyRangeConsolesOut, operation_id="listMyRangeConsoles",
    summary="My classes: the machines of my class range I may open a console on", responses=_DENIED,
)
def list_my_range_consoles(
    range_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.CLASS_ATTEND)),
) -> MyRangeConsolesOut:
    """The Student's own seat, or their class's shared range while a collective session on it
    is under way (the rule of their credentials); only the machines they are handed accounts
    on. ``open`` says whether a session's window is open now.  **class:attend**"""
    found = consoles.student_range(db, user, range_id)
    mine = consoles.student_machines(db, found.rng)
    return MyRangeConsolesOut(
        range_id=found.rng.id, open=found.session is not None,
        session_id=found.session.id if found.session is not None else None,
        machines=[ConsoleMachineOut(**m) for m in consoles.machines(found.rng, mine) if m["student_machine"]],
    )


@router.post(
    "/attending/ranges/{range_id}/consoles/{vm}", response_model=ClassConsoleOut,
    operation_id="openMyRangeConsole", summary="My classes: open a console on one of my machines",
    responses={**_DENIED, 403: {"description": "Outside the session's window"},
               409: {"description": "The machine is not built or not running"},
               502: {"description": "The hypervisor gave no console"}},
)
def open_my_range_console(
    range_id: uuid.UUID,
    vm: str = Path(..., pattern=MACHINE, description="The VM's name, as the list gives it"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.CLASS_ATTEND)),
) -> ClassConsoleOut:
    """A one-time console ticket on one of the Student's own machines, while a session of
    the class using that range is live or within its provisioning lead. Audited
    (``range_console_open``, as ``student``), and counted as attendance evidence like opening
    the accounts.  **class:attend**"""
    return ClassConsoleOut(**consoles.student_console(db, user, range_id, vm))


# ── The live view (ADR 0014 §19) ─────────────────────────────────────────────


def _range_out(db: Session, rng: Range) -> LiveRangeOut:
    return LiveRangeOut(range_id=rng.id, name=rng.name, state=getattr(rng.state, "value", str(rng.state)),
                        machines=[ConsoleMachineOut(**m)
                                  for m in consoles.machines(rng, consoles.student_machines(db, rng))])


@router.get(
    "/{class_id}/sessions/{event_id}/live", response_model=ClassLiveOut, operation_id="getClassSessionLive",
    summary="A class session's live view: each Student's machines and status",
)
def get_class_session_live(
    class_id: uuid.UUID,
    event_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> ClassLiveOut:
    """Its lead, assistants, observers and admins: every Student on the roll call with their
    seat's machines (individual) or team (collective, with the shared range), and when they
    last opened their range. Another tenant's class, or one the caller has no part in, is 404;
    a Student is 403. No credentials here."""
    live = consoles.live_session(db, user, class_id, event_id)
    cls, evt, now = live.cls, live.evt, datetime.now(UTC)
    entries, seats, shared = consoles.session_ranges(db, live)
    names = consoles.names_of(db, cls.tenant_id, entries)
    team_of: dict[uuid.UUID, LiveTeamOut] = {}
    session_teams = teams.teams_of(db, live.link)
    if session_teams:
        by_id = {t.id: t for t in session_teams}
        for uid, tid in db.query(TeamMembership.user_id, TeamMembership.team_id).filter(
                TeamMembership.team_id.in_(list(by_id))):
            team_of.setdefault(uid, LiveTeamOut(id=tid, name=by_id[tid].name))
    range_ids = [r.id for r in seats.values()] + ([shared.id] if shared is not None else [])
    seen = consoles.last_seen(db, cls.tenant_id, [e.user_id for e in entries if e.user_id], range_ids,
                              as_utc(evt.start_time) - consoles.PROVISION_LEAD)
    students = []
    for e in entries:
        rng = seats.get(e.id) or shared
        students.append(LiveStudentOut(
            entry_id=e.id, user_id=e.user_id, display_name=names.get(e.user_id) or e.email,
            team=team_of.get(e.user_id) if e.user_id else None,
            range=_range_out(db, seats[e.id]) if e.id in seats else None,
            last_seen_at=seen.get((e.user_id, str(rng.id))) if rng is not None and e.user_id else None,
        ))
    start, end = as_utc(evt.start_time), as_utc(evt.end_time)
    return ClassLiveOut(
        class_id=cls.id, class_name=cls.name, event_id=evt.id, session_name=evt.name, start_time=start,
        end_time=end, range_mode=live.link.range_mode, role=live.role.value,
        live=evt.state in consoles.HOLDING and start <= now < end,
        consoles_open=consoles.in_window(evt, now) and cls.status == "open",
        view_only_consoles=any(consoles.view_only_supported(r) for r in [*seats.values(), *filter(None, [shared])]),
        shared_range=_range_out(db, shared) if shared is not None else None,
        students=students, generated_at=now,
    )


@router.post(
    "/{class_id}/sessions/{event_id}/live/console", response_model=ClassConsoleOut,
    operation_id="openClassSessionConsole", summary="Open a console on a machine of a class session's range",
    responses={403: {"description": "A Student, or outside the session's window"},
               404: {"description": "Not a range of this session, or a class the caller has no part in"},
               409: {"description": "The machine is not built or not running, or the class is not open"},
               502: {"description": "The hypervisor gave no console"}},
)
def open_class_session_console(
    body: ClassConsoleIn,
    class_id: uuid.UUID,
    event_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> ClassConsoleOut:
    """A one-time console ticket on a machine of one of the session's ranges (a Student's
    seat or the shared range), during the session's window: any machine for its lead,
    assistants and admins; the Students' machines only for observers, view-only where the
    hypervisor enforces it, otherwise a normal console marked ``observed``.
    Audited (``range_console_open``, as ``observer`` or ``staff``)."""
    live = consoles.live_session(db, user, class_id, event_id)
    return ClassConsoleOut(**consoles.staff_console(db, user, live, body.range_id, body.vm))
