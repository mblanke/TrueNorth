"""Whose after-action report a Student may read.

``aar:read`` is held by Students, observers, instructors and admins (rbac.py). Every AAR
route already resolves the exercise inside the caller's tenant (``get_owned``). That was
the whole check, so a Student could read the AAR of any exercise in their tenant: the
report of a run they never sat, with the other Students' names, teams and evidence, and —
for a run still in progress — the inject playbook and the objectives' evidence, which is
the answer key (ADR 0005 §5).

A Student now reads an AAR only when both hold:

- the exercise is completed (nothing left to be scored, so nothing in it is an answer key);
- they took part: a member of one of its teams, or they submitted a detection in it.

Anything else is 404, the same answer as for another tenant's exercise or a report not
generated yet (tenancy.py: "this exists but is not yours" is itself disclosure). Staff and
observers keep the tenant-wide view: they review every run.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from .auth import CurrentUser
from .detections.models import DetectionSubmission
from .models import Exercise, ExerciseState, Team, TeamMembership, UserRole


def scoped_to_participation(user: CurrentUser) -> bool:
    """Students see only their own exercises' reports; every other role the tenant's."""
    return user.role == UserRole.student


def took_part(db: Session, ex: Exercise, user_id: uuid.UUID) -> bool:
    on_a_team = (
        db.query(TeamMembership.id)
        .join(Team, TeamMembership.team_id == Team.id)
        .filter(Team.exercise_id == ex.id, TeamMembership.user_id == user_id)
        .first()
    )
    if on_a_team:
        return True
    submitted = (
        db.query(DetectionSubmission.id)
        .filter(DetectionSubmission.exercise_id == ex.id, DetectionSubmission.user_id == user_id)
        .first()
    )
    return submitted is not None


def may_read_aar(db: Session, ex: Exercise, user: CurrentUser) -> bool:
    """``ex`` is already the caller's tenant's (get_owned)."""
    if not scoped_to_participation(user):
        return True
    return ex.state == ExerciseState.completed and took_part(db, ex, uuid.UUID(str(user.id)))
