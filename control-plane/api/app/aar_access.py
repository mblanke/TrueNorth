"""Whose after-action report a Student may read.

``aar:read`` is held by Students, observers, instructors and admins (rbac.py). Every AAR
route already resolves the exercise inside the caller's tenant (``get_owned``). That was
the whole check, so a Student could read the AAR of any exercise in their tenant: the
report of a run they never sat, with the other Students' names, teams and evidence, and —
for a run still in progress — the inject playbook and the objectives' evidence, which is
the answer key (ADR 0005 §5).

A Student now reads an AAR only when both hold:

- the exercise is completed (nothing left to be scored, so nothing in it is an answer key);
- they took part in this run (``took_part``), by any of:
    * the roster: an assigned enrolment, made before the run ended and not withdrawn, in a
      course whose booking overlaps the run (``exercise_participants.roster_courses``; a
      self-enrolment is no seat; a later cohort on the same course is not this run's);
    * a member of one of the exercise's teams, as staff have them now (teams belong to the
      exercise, not to one run: a replay keeps them, and a Student taken off a team is out);
    * a detection submitted in this run (``window_start >= started_at``, as ``_attempts``
      counts them), so a run-1 submitter does not read run 2's report after
      ``run?reset=true``.

Anything else is 404, the same answer as for another tenant's exercise or a report not
generated yet (tenancy.py: "this exists but is not yours" is itself disclosure). Staff and
observers keep the tenant-wide view: they review every run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from .auth import CurrentUser
from .detections.models import DetectionSubmission
from .enrollment import ASSIGNED
from .exercise_participants import roster_courses
from .models import Enrollment, EnrollmentStatus, Exercise, ExerciseState, Team, TeamMembership, UserRole


def scoped_to_participation(user: CurrentUser) -> bool:
    """Students see only their own exercises' reports; every other role the tenant's."""
    return user.role == UserRole.student


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _rostered_during_run(db: Session, ex: Exercise, user_id: uuid.UUID) -> bool:
    """An assigned enrolment in a course booked over the run, made before the run ended.

    Not ``enrollment.rostered_course_ids``, which is "assigned and still studying, now":
    that let a later cohort assigned to the same course read an earlier run's report (its
    answer key, ADR 0005 §5) before sitting the same scenario, and shut out a real
    participant once their enrolment was completed. Here: created no later than the run's
    end, any status but withdrawn (completed counts).
    """
    courses = roster_courses(db, ex)
    if not courses:
        return False
    run_end = _aware(ex.completed_at or datetime.now(UTC))
    rows = (
        db.query(Enrollment.created_at)
        .filter(
            Enrollment.user_id == user_id,
            Enrollment.course_id.in_(courses),
            Enrollment.source == ASSIGNED,
            Enrollment.status != EnrollmentStatus.withdrawn,
        )
        .all()
    )
    return any(created is not None and _aware(created) <= run_end for (created,) in rows)


def took_part(db: Session, ex: Exercise, user_id: uuid.UUID) -> bool:
    if _rostered_during_run(db, ex, user_id):
        return True
    on_a_team = (
        db.query(TeamMembership.id)
        .join(Team, TeamMembership.team_id == Team.id)
        .filter(Team.exercise_id == ex.id, TeamMembership.user_id == user_id)
        .first()
    )
    if on_a_team:
        return True
    if ex.started_at is None:
        return False  # never ran: no submission belongs to a run of it
    submitted = (
        db.query(DetectionSubmission.id)
        .filter(
            DetectionSubmission.exercise_id == ex.id,
            DetectionSubmission.user_id == user_id,
            DetectionSubmission.window_start >= ex.started_at,
        )
        .first()
    )
    return submitted is not None


def may_read_aar(db: Session, ex: Exercise, user: CurrentUser) -> bool:
    """``ex`` is already the caller's tenant's (get_owned)."""
    if not scoped_to_participation(user):
        return True
    return ex.state == ExerciseState.completed and took_part(db, ex, uuid.UUID(str(user.id)))
