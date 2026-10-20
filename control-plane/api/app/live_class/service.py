"""Build the live class read model for one exercise (issue #199). Read only: writes nothing.

Who is in the class is not decided here. It is ``aar_access.participants`` (an assigned
enrolment in a course booked over the run, a member of one of the exercise's teams, a
detection submitted in this run) plus whoever has their own lab session on the exercise's
range (``telemetry_access.lab_sessions_on``, the relation ``exercise_participants.may_submit``
already accepts), kept to the tenant's Students.

Per-Student credit comes from that Student's own judged attempts in this run (ADR 0005):
``Objective.achieved`` is exercise-wide and says nothing about who earned it.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ..aar_access import participants
from ..detections.models import ACHIEVED, ATTEMPTS, MISSED, PENDING, DetectionSubmission
from ..lab_sessions import models as lab
from ..models import Exercise, ExerciseState, Objective, Range, Team, TeamMembership, User, UserRole
from ..telemetry_access import lab_sessions_on
from .schemas import (
    AttentionOut,
    ClassAttemptOut,
    ClassLabOut,
    ClassObjectiveOut,
    ClassObjectiveStateOut,
    ClassRangeOut,
    ClassStudentOut,
    ClassTeamOut,
    ExerciseClassOut,
)

# A learning signal is evidence, never idleness: this many judged misses on one objective
# with no credit on it.
LEARNING_SIGNAL_MISSES = 3
# Labs that are down through no doing of the Student's, and labs on their way up.
LAB_FAULT = (lab.FAILED, lab.EXPIRED, lab.RECONCILE)
LAB_WAIT = (lab.QUEUED, lab.PROVISIONING, lab.BASELINING, lab.RESETTING)


def not_seen_minutes() -> int:
    """``LIVE_CLASS_NOT_SEEN_MINUTES`` (default 15): quiet this long while the exercise runs."""
    try:
        return max(1, int(os.getenv("LIVE_CLASS_NOT_SEEN_MINUTES", "15")))
    except ValueError:
        return 15


def _aware(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _roster(db: Session, ex: Exercise, labs: list) -> list[User]:
    ids = participants(db, ex) | {s.user_id for s in labs}
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
        .order_by(User.display_name, User.id)
        .all()
    )


def _teams(db: Session, ex: Exercise, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, ClassTeamOut]:
    rows = (
        db.query(TeamMembership.user_id, Team)
        .join(Team, TeamMembership.team_id == Team.id)
        .filter(Team.exercise_id == ex.id, TeamMembership.user_id.in_(user_ids))
        .order_by(Team.name)
        .all()
    )
    out: dict[uuid.UUID, ClassTeamOut] = {}
    for uid, team in rows:
        out.setdefault(uid, ClassTeamOut(id=team.id, name=team.name, color_hex=team.color_hex))
    return out


def _attempts(db: Session, ex: Exercise, user_ids: list[uuid.UUID]) -> list[DetectionSubmission]:
    """This run's attempts (``window_start >= started_at``, as the attempt cap counts them).
    Unscored and unparseable queries use no attempt, so they are not shown as attempts."""
    if ex.started_at is None or not user_ids:
        return []
    return (
        db.query(DetectionSubmission)
        .filter(
            DetectionSubmission.exercise_id == ex.id,
            DetectionSubmission.user_id.in_(user_ids),
            DetectionSubmission.window_start >= ex.started_at,
            DetectionSubmission.verdict.in_(ATTEMPTS),
        )
        .order_by(DetectionSubmission.submitted_at, DetectionSubmission.id)
        .all()
    )


def _attempt_out(row: DetectionSubmission, sees_key: bool) -> ClassAttemptOut:
    out = ClassAttemptOut(
        id=row.id,
        objective_ref=row.objective_ref,
        verdict=row.verdict,
        submitted_at=_aware(row.submitted_at),
        events_matched=row.events_matched,
    )
    if sees_key:
        out.query = row.query
        out.precision = round(row.on_target / row.events_matched, 3) if row.events_matched else 0.0
    return out


def _lab_out(s) -> ClassLabOut:
    return ClassLabOut(
        session_id=s.id,
        state=s.state,
        error=s.error or "",
        attempt=s.attempt or 1,
        ready_at=_aware(s.ready_at),
        last_seen_at=_aware(s.last_seen_at),
        idle_expires_at=_aware(s.idle_expires_at),
        max_expires_at=_aware(s.max_expires_at),
    )


def _minutes(delta) -> int:
    return max(0, int(delta.total_seconds() // 60))


def _student(
    user: User,
    ex: Exercise,
    objectives: list[Objective],
    rows: list[DetectionSubmission],
    team: ClassTeamOut | None,
    lab_session,
    sees_key: bool,
    now: datetime,
    quiet_after: int,
) -> ClassStudentOut:
    points = {o.ref_id: o.points or 0 for o in objectives}
    by_ref: dict[str, list[DetectionSubmission]] = {}
    for r in rows:
        by_ref.setdefault(r.objective_ref, []).append(r)

    states: list[ClassObjectiveStateOut] = []
    credited: list[str] = []
    attention: list[AttentionOut] = []
    for o in objectives:
        mine = by_ref.get(o.ref_id, [])
        achieved = [r for r in mine if r.verdict == ACHIEVED]
        misses = [r for r in mine if r.verdict == MISSED]
        if achieved:
            credited.append(o.ref_id)
            states.append(ClassObjectiveStateOut(ref_id=o.ref_id, state="credited", misses=len(misses),
                                                 credited_at=_aware(achieved[0].submitted_at)))
            continue
        if any(r.verdict == PENDING for r in mine):
            state = "pending"
        elif misses:
            state = "missed"
        else:
            state = "not_attempted"
        states.append(ClassObjectiveStateOut(ref_id=o.ref_id, state=state, misses=len(misses)))
        if len(misses) >= LEARNING_SIGNAL_MISSES:
            attention.append(AttentionOut(
                kind="learning", objective_ref=o.ref_id, since=_aware(misses[0].submitted_at),
                detail=f"{len(misses)} judged misses on {o.ref_id} and no credit on it yet",
            ))

    lab_out = _lab_out(lab_session) if lab_session is not None else None
    lab_down = False
    if lab_out is not None:
        if lab_out.state in LAB_FAULT:
            lab_down = True
            detail = f"Lab {lab_out.state}" + (f": {lab_out.error}" if lab_out.error else "")
            attention.insert(0, AttentionOut(kind="lab_fault", detail=detail, since=_aware(lab_session.state_since)))
        elif lab_out.state in LAB_WAIT:
            lab_down = True
            attention.insert(0, AttentionOut(kind="lab_wait", detail=f"Lab {lab_out.state}",
                                             since=_aware(lab_session.state_since)))

    last_detection = _aware(rows[-1].submitted_at) if rows else None
    seen = [t for t in (last_detection, lab_out.last_seen_at if lab_out else None) if t is not None]
    last_activity = max(seen) if seen else None

    # Neutral, and only while the exercise runs: a paused or closed class is not "quiet",
    # and a lab that is down is already in the lab lane.
    started = _aware(ex.started_at)
    if ex.state == ExerciseState.running and started is not None and not lab_down:
        ref_time = last_activity or started
        quiet = _minutes(now - ref_time)
        if quiet >= quiet_after:
            detail = (f"No activity seen for {quiet} min" if last_activity
                      else f"No activity seen since the start ({quiet} min)")
            attention.append(AttentionOut(kind="not_seen", detail=detail, since=ref_time))

    pending = [r for r in rows if r.verdict == PENDING]
    first_open = next((s.ref_id for s in states if s.state != "credited"), None)
    current = pending[-1].objective_ref if pending else first_open

    return ClassStudentOut(
        user_id=user.id,
        display_name=user.display_name,
        team=team,
        lab=lab_out,
        attempts_total=len(rows),
        attempts_credited=sum(1 for r in rows if r.verdict == ACHIEVED),
        credited_refs=credited,
        score=sum(points.get(ref, 0) for ref in credited),
        current_objective=current,
        last_detection_at=last_detection,
        last_activity_at=last_activity,
        objectives=states,
        attempts=[_attempt_out(r, sees_key) for r in rows],
        attention=attention,
    )


def build(db: Session, ex: Exercise, *, sees_key: bool, now: datetime | None = None) -> ExerciseClassOut:
    """The class for ``ex`` (already resolved in the caller's tenant)."""
    now = now or datetime.now(UTC)
    quiet_after = not_seen_minutes()
    labs = lab_sessions_on(db, ex.tenant_id, ex.range_id) if ex.range_id is not None else []
    students = _roster(db, ex, labs)
    user_ids = [u.id for u in students]
    newest_lab: dict[uuid.UUID, object] = {}
    for s in labs:  # newest first
        newest_lab.setdefault(s.user_id, s)
    teams = _teams(db, ex, user_ids) if user_ids else {}
    objectives = (
        db.query(Objective).filter(Objective.exercise_id == ex.id).order_by(Objective.created_at, Objective.ref_id).all()
    )
    attempts = _attempts(db, ex, user_ids)
    per_user: dict[uuid.UUID, list[DetectionSubmission]] = {}
    for r in attempts:
        per_user.setdefault(r.user_id, []).append(r)

    rng = (
        db.query(Range).filter(Range.id == ex.range_id, Range.tenant_id == ex.tenant_id).first()
        if ex.range_id is not None
        else None
    )
    return ExerciseClassOut(
        exercise_id=ex.id,
        exercise_name=ex.name,
        state=ex.state.value if hasattr(ex.state, "value") else str(ex.state),
        started_at=_aware(ex.started_at),
        completed_at=_aware(ex.completed_at),
        range=ClassRangeOut(
            id=rng.id, name=rng.name, state=rng.state.value if hasattr(rng.state, "value") else str(rng.state),
            error=rng.error_message,
        ) if rng is not None else None,
        max_score=ex.max_score or sum(o.points or 0 for o in objectives),
        objectives=[
            ClassObjectiveOut(
                ref_id=o.ref_id, description=o.description or "",
                objective_type=o.objective_type.value if hasattr(o.objective_type, "value") else str(o.objective_type),
                validator=o.validator or "", points=o.points or 0,
            )
            for o in objectives
        ],
        students=[
            _student(u, ex, objectives, per_user.get(u.id, []), teams.get(u.id), newest_lab.get(u.id), sees_key, now,
                     quiet_after)
            for u in students
        ],
        learning_signal_misses=LEARNING_SIGNAL_MISSES,
        not_seen_minutes=quiet_after,
        generated_at=now,
    )
