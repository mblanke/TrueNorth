"""Who an exercise's results belong to (#375).

An exercise has no Student owner column. Its Students are what the class records:

- an individual exercise runs on a Student's **seat range** (``class_ranges`` kind seat, whose
  roll-call entry is that Student's);
- a team exercise: every member of the **teams** that joined it (``teams.exercise_id``, set by
  ``teams.on_exercise``). Owner decision 2026-11-03: every participant is credited.

A Student taken off the roll call (``OFF_ROLL_CALL``) or in a cancelled class is not credited.
Instructors and other staff never are: results are about the Students (owner decision
2026-10-06), so an exercise with no Students credits no one.

One filter, ``eligible_students``, decides who may be credited, whichever way they got named
(seat, team, or a detection a judgement cites): a Student of the exercise's tenant, not
deleted, and, when the exercise runs on a class's range, on that class's roll call (not
removed, pending or forfeited; class not cancelled). A team that has nobody to check against
(an exercise that is not on a class range) is credited as before.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy.orm import Session

from ..models import Exercise, Team, TeamMembership, User, UserRole
from .models import OFF_ROLL_CALL, Class, ClassRange, ClassRangeKind, ClassStatus, RollCallEntry


def _class_ids(db: Session, ex: Exercise) -> set[uuid.UUID]:
    """The classes whose range this exercise runs on (empty: it is not a class exercise)."""
    if ex.range_id is None:
        return set()
    rows = (
        # tenant-safe: class ranges of the exercise's own range, in the exercise's tenant.
        db.query(ClassRange.class_id)
        .filter(ClassRange.range_id == ex.range_id, ClassRange.tenant_id == ex.tenant_id)
        .all()
    )
    return {r[0] for r in rows}


def eligible_students(db: Session, ex: Exercise, user_ids: Iterable[uuid.UUID | str]) -> list[User]:
    """Of *user_ids*, the Students this exercise's results may be credited to, in name order."""
    ids: set[uuid.UUID] = set()
    for raw in user_ids:
        try:
            ids.add(raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw)))
        except ValueError:
            continue  # not an id: nobody to credit
    if not ids:
        return []
    classes = _class_ids(db, ex)
    if classes:
        # tenant-safe: roll-call entries of the exercise's own classes, in its tenant.
        on_roll = (
            db.query(RollCallEntry.user_id)
            .join(Class, Class.id == RollCallEntry.class_id)
            .filter(
                RollCallEntry.class_id.in_(classes),
                RollCallEntry.tenant_id == ex.tenant_id,
                RollCallEntry.user_id.in_(ids),
                RollCallEntry.status.notin_(OFF_ROLL_CALL),
                Class.status != ClassStatus.cancelled.value,
            )
            .all()
        )
        ids &= {r[0] for r in on_roll}
        if not ids:
            return []
    return (
        db.query(User)
        .filter(
            User.id.in_(ids),
            User.tenant_id == ex.tenant_id,
            User.role == UserRole.student,
            User.deleted_at.is_(None),
        )
        .order_by(User.display_name)
        .all()
    )


def exercise_students(db: Session, ex: Exercise) -> list[User]:
    """The Students this exercise's results belong to, in name order (empty if none)."""
    # tenant-safe: seats of the exercise's own range, in the exercise's tenant.
    seated = (
        db.query(RollCallEntry.user_id)
        .join(ClassRange, ClassRange.entry_id == RollCallEntry.id)
        .join(Class, Class.id == ClassRange.class_id)
        .filter(
            ClassRange.range_id == ex.range_id,
            ClassRange.tenant_id == ex.tenant_id,
            ClassRange.kind == ClassRangeKind.seat.value,
            RollCallEntry.user_id.isnot(None),
            RollCallEntry.status.notin_(OFF_ROLL_CALL),
            Class.status != ClassStatus.cancelled.value,
        )
    )
    # tenant-safe: members of teams that joined this exercise, in its tenant.
    on_teams = (
        db.query(TeamMembership.user_id)
        .join(Team, Team.id == TeamMembership.team_id)
        .filter(Team.exercise_id == ex.id, Team.tenant_id == ex.tenant_id)
    )
    ids = {row[0] for row in seated.all()} | {row[0] for row in on_teams.all()}
    return eligible_students(db, ex, ids)
