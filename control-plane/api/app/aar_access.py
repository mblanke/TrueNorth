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
from .enrollment import ACTIVE_STATUSES, ASSIGNED, seated_by
from .exercise_participants import roster_courses
from .models import Enrollment, EnrollmentStatus, Exercise, ExerciseState, Team, TeamMembership, UserRole


def scoped_to_participation(user: CurrentUser) -> bool:
    """Students see only their own exercises' reports; every other role the tenant's."""
    return user.role == UserRole.student


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _rostered_during_run(
    db: Session,
    ex: Exercise,
    user_id: uuid.UUID | None = None,
    *,
    active_only: bool = False,
    now: datetime | None = None,
) -> set[uuid.UUID]:
    """Who holds an assigned enrolment in a course booked over the run, seated before the run
    ended (``user_id`` narrows it to one person).

    Not ``enrollment.rostered_course_ids``, which is "assigned and still studying, now":
    that let a later cohort assigned to the same course read an earlier run's report (its
    answer key, ADR 0005 §5) before sitting the same scenario, and shut out a real
    participant once their enrolment was completed. Here: seated no later than the run's
    end, any status but withdrawn (completed counts).

    ``active_only`` (the live class, ``participants``): only enrolments still being studied
    (``enrollment.ACTIVE_STATUSES``), the rule ``exercise_participants.may_submit`` applies
    to who sits this run now. Without it a booked course's earlier cohorts (completed, a
    year ago) would be on today's class. This stays the one roster query for both uses.

    "Seated" is ``enrollment.seated_by`` (#200), the one rule for both uses: the seat began
    at the later of ``created_at`` and ``enrolled_at``. ``enrolled_at`` moves to the moment
    staff reopen a withdrawn enrolment or turn a self-enrolment into an assigned one, so a
    Student withdrawn before a run and re-enrolled after it is not a participant of that run,
    and one whose own self-enrolment pre-dates a run is not seated in it retroactively.
    Known limitation: only the current seat is recorded, so re-enrolling after a run the
    Student sat on the roster drops that run (unless a team or detection ties them to it).
    """
    courses = roster_courses(db, ex, now)
    if not courses:
        return set()
    run_end = _aware(ex.completed_at or now or datetime.now(UTC))
    q = db.query(Enrollment.user_id, Enrollment.created_at, Enrollment.enrolled_at).filter(
        Enrollment.course_id.in_(courses),
        Enrollment.source == ASSIGNED,
        Enrollment.status != EnrollmentStatus.withdrawn,
    )
    if active_only:
        q = q.filter(Enrollment.status.in_(ACTIVE_STATUSES))
    if user_id is not None:
        q = q.filter(Enrollment.user_id == user_id)
    return {uid for uid, created, enrolled in q.all() if seated_by(created, enrolled, run_end)}


def _on_a_team(db: Session, ex: Exercise, user_id: uuid.UUID | None = None) -> set[uuid.UUID]:
    q = (
        db.query(TeamMembership.user_id)
        .join(Team, TeamMembership.team_id == Team.id)
        .filter(Team.exercise_id == ex.id)
    )
    if user_id is not None:
        q = q.filter(TeamMembership.user_id == user_id)
    return {uid for (uid,) in q.all()}


def _submitted_in_run(db: Session, ex: Exercise, user_id: uuid.UUID | None = None) -> set[uuid.UUID]:
    if ex.started_at is None:
        return set()  # never ran: no submission belongs to a run of it
    q = db.query(DetectionSubmission.user_id).filter(
        DetectionSubmission.exercise_id == ex.id,
        DetectionSubmission.window_start >= ex.started_at,
    )
    if user_id is not None:
        q = q.filter(DetectionSubmission.user_id == user_id)
    return {uid for (uid,) in q.distinct().all()}


def took_part(db: Session, ex: Exercise, user_id: uuid.UUID) -> bool:
    return bool(
        _rostered_during_run(db, ex, user_id) or _on_a_team(db, ex, user_id) or _submitted_in_run(db, ex, user_id)
    )


def participants(db: Session, ex: Exercise, now: datetime | None = None) -> set[uuid.UUID]:
    """Who is in this run's class now, at once (the live class roster, app/live_class): the
    same three relations as ``took_part``, except that the roster counts only enrolments
    still being studied (``active_only``, as ``may_submit`` does), so a booked course's
    earlier cohorts are not on today's class. ``took_part`` (the AAR) is unchanged: a
    participant whose enrolment has since completed still reads their report. Callers
    filter to Students. ``now``: the caller's clock."""
    return _rostered_during_run(db, ex, active_only=True, now=now) | _on_a_team(db, ex) | _submitted_in_run(db, ex)


def may_read_aar(db: Session, ex: Exercise, user: CurrentUser) -> bool:
    """``ex`` is already the caller's tenant's (get_owned)."""
    if not scoped_to_participation(user):
        return True
    return ex.state == ExerciseState.completed and took_part(db, ex, uuid.UUID(str(user.id)))
