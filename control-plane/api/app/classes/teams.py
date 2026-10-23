"""A collective session's teams (ADR 0014 §11; #282).

A collective session (an exercise: everyone on one shared range) splits its class's roll call
into balanced teams: the Students with an account, round-robin in name order, so team sizes
differ by at most one. Re-splitting rebalances everyone. The class's lead (or an admin) moves
a Student from one team to another, before the session starts; afterwards the teams are the
exercise's record. Assistants and observers read them.

The teams are ordinary ``teams`` rows with their members in ``team_memberships``, linked to
the session in ``class_session_teams``. They join the session's exercise when it has one (a
linked exercise at once; the clock's auto-created one when it builds the range), so the
exercise's AAR, Live Class and team views find them as they find any team. The ``/teams``
endpoints (routers/admin.py) treat them as the class's: only its lead or an admin, before
the session starts, and only Students on the roll call (``authorize``, ``member_target``).

A Student taken off the roll call leaves the teams of the class's sessions that have not
started. A Student without an account yet (invited, never signed in) cannot be on a team; they
are listed as unassigned until they sign in and the lead splits again or places them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..models import Team, TeamMembership, User, UserRole
from ..scheduler.models import EventState, ScheduledEvent
from . import service
from .models import OFF_ROLL_CALL, Class, ClassSession, ClassSessionTeam, ClassStatus, RangeMode, RollCallEntry

NOT_STARTED = (EventState.draft, EventState.pending_approval, EventState.scheduled)
MAX_TEAMS = 26


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def session_of(db: Session, cls: Class, event_id: uuid.UUID) -> tuple[ClassSession, ScheduledEvent]:
    """The class's session ``event_id`` (404 when it is not one of this class's)."""
    link = (
        db.query(ClassSession)
        .filter(ClassSession.class_id == cls.id, ClassSession.event_id == event_id,
                ClassSession.tenant_id == cls.tenant_id)
        .first()
    )
    # tenant-safe: the booking of the class's own session link, checked against the class's tenant.
    evt = db.get(ScheduledEvent, event_id) if link is not None else None
    if link is None or evt is None or evt.tenant_id != cls.tenant_id:
        raise HTTPException(404, "Session not found")
    return link, evt


def started(evt: ScheduledEvent, now: datetime | None = None) -> bool:
    return evt.state not in NOT_STARTED or _aware(evt.start_time) <= (now or _now())


def require_changeable(cls: Class, link: ClassSession, evt: ScheduledEvent) -> None:
    """Teams are a collective session's (409 otherwise), of an open class, before it starts."""
    if link.range_mode != RangeMode.collective.value:
        raise HTTPException(409, "Teams are for collective sessions; in an individual one every Student has a range")
    if cls.status != ClassStatus.open.value:
        raise HTTPException(409, "The class is not open; its teams cannot change")
    if evt.state in (EventState.cancelled, EventState.rejected):
        raise HTTPException(409, f"The session is {evt.state.value}")
    if started(evt):
        raise HTTPException(409, "The session has started; its teams are the exercise's record now")


def roster(db: Session, cls: Class) -> tuple[list[User], list[RollCallEntry]]:
    """(Students who can be on a team, entries who cannot yet): the roll call's active
    Student accounts of the class's tenant, in name order; and entries with no account."""
    entries = (
        db.query(RollCallEntry)
        .filter(RollCallEntry.class_id == cls.id, RollCallEntry.tenant_id == cls.tenant_id,
                RollCallEntry.status.notin_(OFF_ROLL_CALL))
        .order_by(RollCallEntry.email)
        .all()
    )
    ids = {e.user_id for e in entries if e.user_id}
    users = (
        db.query(User)
        .filter(User.id.in_(ids), User.tenant_id == cls.tenant_id, User.role == UserRole.student,
                User.is_active == True, User.deleted_at.is_(None))  # noqa: E712
        .all()
        if ids else []
    )
    placeable = {u.id for u in users}
    return (sorted(users, key=lambda u: ((u.display_name or "").lower(), str(u.id))),
            [e for e in entries if e.user_id not in placeable])


def teams_of(db: Session, link: ClassSession) -> list[Team]:
    rows = (
        db.query(Team)
        .join(ClassSessionTeam, ClassSessionTeam.team_id == Team.id)
        .filter(ClassSessionTeam.session_id == link.id, Team.tenant_id == link.tenant_id)
        .order_by(ClassSessionTeam.position)
        .all()
    )
    return rows


def split(db: Session, cls: Class, link: ClassSession, evt: ScheduledEvent, count: int, actor: CurrentUser) -> None:
    """Balanced teams from the roll call: ``count`` teams, sizes differing by at most one.
    Re-splitting reuses the session's teams (adds or drops some) and places everyone again.
    Audited. Does not commit."""
    require_changeable(cls, link, evt)
    if not 2 <= count <= MAX_TEAMS:
        raise HTTPException(422, f"Split into 2 to {MAX_TEAMS} teams")
    students, _ = roster(db, cls)
    teams = teams_of(db, link)
    ids = [t.id for t in teams]
    if ids:
        db.query(TeamMembership).filter(TeamMembership.team_id.in_(ids)).delete(synchronize_session=False)
    for extra in teams[count:]:  # pure dependents of a session that has not started (ADR 0010 §3)
        db.query(ClassSessionTeam).filter(ClassSessionTeam.team_id == extra.id).delete(synchronize_session=False)
        db.delete(extra)
    teams = teams[:count]
    for i in range(len(teams), count):
        team = Team(name=f"Team {i + 1}", tenant_id=cls.tenant_id, exercise_id=evt.exercise_id, is_persistent=False,
                    description=f"{cls.name}: {evt.name}"[:1000])
        db.add(team)
        db.flush()
        db.add(ClassSessionTeam(tenant_id=cls.tenant_id, session_id=link.id, team_id=team.id, position=i))
        teams.append(team)
    now = _now()
    for i, u in enumerate(students):
        db.add(TeamMembership(user_id=u.id, team_id=teams[i % count].id, joined_at=now))
    db.flush()
    service.audit(db, actor.id, cls, "class_teams_split",
                  f"session {evt.id}: {len(students)} Student(s) into {count} teams")


def move(
    db: Session, cls: Class, link: ClassSession, evt: ScheduledEvent, user_id: uuid.UUID, team_id: uuid.UUID,
    actor: CurrentUser,
) -> None:
    """Put a Student of the roll call on one of the session's teams (off any other of them).
    Audited. Does not commit."""
    require_changeable(cls, link, evt)
    teams = teams_of(db, link)
    target = next((t for t in teams if t.id == team_id), None)
    if target is None:
        raise HTTPException(404, "Team not found")
    students, _ = roster(db, cls)
    if user_id not in {u.id for u in students}:
        raise HTTPException(422, "Only Students on the class's roll call (with an account) can be on its teams")
    db.query(TeamMembership).filter(
        TeamMembership.user_id == user_id, TeamMembership.team_id.in_([t.id for t in teams])
    ).delete(synchronize_session=False)
    db.add(TeamMembership(user_id=user_id, team_id=target.id, joined_at=_now()))
    db.flush()
    service.audit(db, actor.id, cls, "class_team_move", f"session {evt.id}: {user_id} -> {target.name} ({target.id})")


def drop_member(db: Session, cls: Class, user_id: uuid.UUID | None) -> None:
    """A Student taken off the roll call leaves the teams of the class's sessions that have
    not started. Does not commit."""
    if user_id is None:
        return
    now = _now()
    team_ids = [
        tid
        for tid, state, start in db.query(ClassSessionTeam.team_id, ScheduledEvent.state, ScheduledEvent.start_time)
        .join(ClassSession, ClassSession.id == ClassSessionTeam.session_id)
        .join(ScheduledEvent, ScheduledEvent.id == ClassSession.event_id)
        .filter(ClassSession.class_id == cls.id, ClassSessionTeam.tenant_id == cls.tenant_id)
        if state in NOT_STARTED and _aware(start) > now
    ]
    if team_ids:
        db.query(TeamMembership).filter(
            TeamMembership.user_id == user_id, TeamMembership.team_id.in_(team_ids)
        ).delete(synchronize_session=False)


def on_exercise(db: Session, evt: ScheduledEvent) -> None:
    """The session now runs ``evt.exercise_id``: its teams join it. Does not commit."""
    if evt.exercise_id is None:
        return
    link = db.query(ClassSession).filter(ClassSession.event_id == evt.id).first()
    if link is None or link.tenant_id != evt.tenant_id:
        return
    for team in teams_of(db, link):
        team.exercise_id = evt.exercise_id


# ── The /teams endpoints on a class session's teams ─────────────────────────


def class_of_team(db: Session, team: Team) -> tuple[Class, ClassSession, ScheduledEvent] | None:
    """The class, session and booking a team belongs to; None for any other team."""
    row = (
        db.query(ClassSessionTeam, ClassSession, Class)
        .join(ClassSession, ClassSession.id == ClassSessionTeam.session_id)
        .join(Class, Class.id == ClassSession.class_id)
        .filter(ClassSessionTeam.team_id == team.id, Class.tenant_id == team.tenant_id)
        .first()
    )
    if row is None:
        return None
    _, link, cls = row
    evt = db.get(ScheduledEvent, link.event_id)  # tenant-safe: the class's own session
    return (cls, link, evt) if evt is not None else None


def authorize(db: Session, user: CurrentUser, team: Team) -> bool:
    """For ``/teams`` (class_setup.scope): a class session's team is changed by the class's
    lead or an admin, before the session starts (403 / 409). True when the team is one; then
    the general rules (an exercise you run) do not apply. False: not a class's team."""
    from . import access

    found = class_of_team(db, team)
    if found is None:
        return False
    cls, link, evt = found
    if access.role_in(db, cls, user) not in access.EDITORS:
        raise HTTPException(403, "Only the class's lead instructor or an admin changes its teams")
    require_changeable(cls, link, evt)
    return True


def member_target(db: Session, user: CurrentUser, team: Team, subject_id: uuid.UUID) -> User | None:
    """For ``/teams/{id}/members``: who may join a class session's team: a Student on the roll
    call (422 otherwise). None when the team is not a class's."""
    found = class_of_team(db, team)
    if found is None:
        return None
    cls = found[0]
    students, _ = roster(db, cls)
    target = next((u for u in students if u.id == subject_id), None)
    if target is None:
        raise HTTPException(422, "Only Students on the class's roll call (with an account) can be on its teams")
    return target
