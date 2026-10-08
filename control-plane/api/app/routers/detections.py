"""Student detections: credit for what the Student found, not for what the attack did (ADR 0005).

  POST /exercises/{id}/objectives/{ref}/detections   a Student submits a detection query;
                                                     the server judges it against the
                                                     objective's answer key, in the window
  GET  /exercises/{id}/detections                    attempts: a Student's own, or every
                                                     attempt for staff who may see the key

The answer key never leaves the server. A Student learns whether the detection was
credited, how many events their query matched, and how many attempts are left.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import exercise_completion as completion
from ..auth import CurrentUser
from ..db import get_db
from ..detections import credit
from ..detections.models import (
    ACHIEVED,
    ATTEMPTS,
    CLOSED,
    INVALID,
    MISSED,
    PENDING,
    UNSCORED,
    DetectionSubmission,
)
from ..models import AuditLog, Exercise, ExerciseState, Objective, Scenario, UserRole
from ..rbac import Permission, require_permission, user_has_permission
from ..search_backends import BaseSearchBackend, SearchBackendError, SearchQueryError, get_search_backend
from ..search_backends.query import QueryError
from ..tenancy import get_owned
from ..xapi import emit_lifecycle

router = APIRouter(prefix="/exercises", tags=["detections"])
STALE_PENDING = timedelta(minutes=5)
MAX_FREE_RETRIES = 20  # queries that did not parse, or met a store outage: free, but not unlimited
logger = logging.getLogger("truenorth.api.detections")


def search_backend() -> BaseSearchBackend:
    return get_search_backend()


class DetectionIn(BaseModel):
    query: str = Field(
        min_length=1,
        max_length=credit.MAX_QUERY_LENGTH,
        description="Detection query: field:value terms with AND/OR/NOT, wildcards, "
        "field:>N ranges and field:(a OR b); no free text, no platform label fields",
    )


class DetectionOut(BaseModel):
    """One attempt. ``on_target`` and ``precision`` are shown only to staff who may see the key."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    objective_ref: str
    user_id: uuid.UUID
    query: str
    submitted_at: datetime | None
    verdict: str
    events_matched: int | None
    on_target: int | None = None
    precision: float | None = None
    attempts_left: int | None = None
    reason: str | None = None


def _sees_key(user: CurrentUser) -> bool:
    return user_has_permission(user, Permission.SCENARIO_UPDATE)


def _out(row: DetectionSubmission, user: CurrentUser, attempts_left: int | None = None) -> DetectionOut:
    out = DetectionOut.model_validate(row)
    out.attempts_left = attempts_left
    if _sees_key(user):
        out.precision = round(row.on_target / row.events_matched, 3) if row.events_matched else 0.0
    else:
        out.on_target = None
    return out


def _attempts(db: Session, exercise_id: uuid.UUID, ref_id: str, user_id: uuid.UUID, start: datetime) -> tuple[int, int]:
    """(attempts used, free retries used) by this Student on this objective in this run.

    Only this run counts: a replayed exercise starts again (``window_start`` is the run's
    start). A pending attempt older than a few minutes is a request that died; it is freed.
    """
    stale = datetime.now(UTC) - STALE_PENDING
    q = db.query(DetectionSubmission.verdict, func.count(DetectionSubmission.id)).filter(
        DetectionSubmission.exercise_id == exercise_id,
        DetectionSubmission.objective_ref == ref_id,
        DetectionSubmission.user_id == user_id,
        DetectionSubmission.window_start >= start,
        (DetectionSubmission.verdict != PENDING) | (DetectionSubmission.submitted_at >= stale),
    )
    counts = dict(q.group_by(DetectionSubmission.verdict).all())
    return sum(counts.get(v, 0) for v in ATTEMPTS), counts.get(UNSCORED, 0) + counts.get(INVALID, 0)


@router.post(
    "/{exercise_id}/objectives/{ref_id}/detections",
    response_model=DetectionOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "Exercise not running, objective already achieved, or not a detection objective"},
        429: {"description": "No attempts left for this objective"},
        503: {"description": "The event store could not answer; the attempt was not counted"},
    },
)
async def submit_detection(
    body: DetectionIn,
    background_tasks: BackgroundTasks,
    exercise_id: uuid.UUID = Path(...),
    ref_id: str = Path(..., max_length=100),
    db: Session = Depends(get_db),
    backend: BaseSearchBackend = Depends(search_backend),
    user: CurrentUser = Depends(require_permission(Permission.DETECTION_SUBMIT)),
) -> DetectionOut:
    """Submit a detection for an objective; it is credited if it finds the attack.  **Permission: detection:submit**"""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    if ex.state != ExerciseState.running or ex.started_at is None:
        raise HTTPException(409, f"Exercise is {ex.state.value}; detections are accepted while it is running")
    obj = db.query(Objective).filter(Objective.exercise_id == ex.id, Objective.ref_id == ref_id).first()
    if obj is None:
        raise HTTPException(404, "Objective not found")
    if obj.achieved:
        raise HTTPException(409, "Objective already achieved")
    # tenant-safe: reached only through ex.scenario_id of an exercise fetched above with
    # get_owned; only its YAML (the answer key) is read, never returned.
    scenario = db.get(Scenario, ex.scenario_id) if ex.scenario_id else None
    try:
        key = credit.answer_key(obj.validator, obj.validator_params, obj.ref_id, scenario.yaml if scenario else None)
    except credit.NotScorable as exc:
        raise HTTPException(409, f"Objective cannot be credited by a detection: {exc}") from exc

    try:
        credit.student_query(body.query)  # the closed detection grammar; labels refused
    except QueryError as exc:
        raise HTTPException(422, f"Your query is outside the detection language (no attempt used): {exc}") from exc

    user_id = uuid.UUID(user.id)
    start = ex.started_at if ex.started_at.tzinfo else ex.started_at.replace(tzinfo=UTC)
    row, attempts_left = _reserve(db, ex.id, obj.id, ref_id, user_id, start, key, body.query, ex.tenant_id)

    try:
        judged = await credit.judge(backend, ex.range_id, key, body.query, start, row.window_end)
    except SearchQueryError as exc:
        _settle(db, row, INVALID, reason=str(exc))
        raise HTTPException(422, f"Your query could not be parsed (no attempt used): {exc}") from exc
    except SearchBackendError as exc:
        _settle(db, row, UNSCORED, reason=str(exc)[:500])
        logger.warning("[detections] exercise %s objective %s unscored: %s", ex.id, ref_id, exc)
        raise HTTPException(503, "The event store could not judge this detection; try again (no attempt used)") from exc

    # tenant-safe: re-reading the row this request reserved for the caller's own exercise.
    row = db.get(DetectionSubmission, row.id)
    row.events_matched, row.on_target = judged.events_matched, judged.on_target
    row.matched_ids = json.dumps(judged.matched_ids)
    credited = False
    if not judged.achieved:
        row.verdict = MISSED
    else:
        # Under the exercise lock: an instructor's /complete (or the clock) either sees this
        # credit in its total or has already closed the exercise and this credits nothing.
        locked = completion.lock(db, ex.id)
        if locked is None or locked.state != ExerciseState.running:
            row.verdict, row.reason = CLOSED, "The exercise closed before this detection was judged; nothing credited"
        else:
            row.verdict = ACHIEVED
            credited = _credit(db, locked, obj.id, row, user)
            if not credited:
                row.reason = "Objective was already achieved"
    db.add(
        AuditLog(user_id=user_id, action=f"detection_{row.verdict}", resource_type="objective", resource_id=str(obj.id))
    )
    db.commit()
    db.refresh(row)

    if credited:
        emit_lifecycle(
            background_tasks,
            verb_key="passed",
            user_email=user.email or f"{user.id}@truenorth.local",
            user_name=user.display_name,
            activity_type="objective",
            activity_id=str(obj.id),
            activity_name=obj.description or obj.ref_id,
            result={"score": {"raw": obj.points}, "success": True},
            context_extensions={"exercise_id": str(ex.id), "ref_id": obj.ref_id, "submission_id": str(row.id)},
        )
    return _out(row, user, attempts_left=attempts_left)


def _reserve(db, exercise_id, objective_id, ref_id, user_id, start, key, query, tenant_id):
    """Take one of the Student's attempts before judging, so concurrent submissions cannot all
    see the same count. The objective row lock serialises them (a no-op on SQLite)."""
    db.query(Objective).filter(Objective.id == objective_id).with_for_update().one()
    used, free = _attempts(db, exercise_id, ref_id, user_id, start)
    if used >= key.max_attempts:
        raise HTTPException(429, f"No attempts left for this objective ({key.max_attempts} used)")
    if free >= MAX_FREE_RETRIES:
        raise HTTPException(429, "Too many queries that could not be judged; ask your instructor")
    row = DetectionSubmission(
        tenant_id=tenant_id,
        exercise_id=exercise_id,
        objective_ref=ref_id,
        user_id=user_id,
        query=query,
        verdict=PENDING,
        threshold=key.threshold,
        min_precision=key.min_precision,
        window_start=start,
        window_end=datetime.now(UTC),
    )
    db.add(row)
    db.commit()
    return row, key.max_attempts - used - 1


def _settle(db: Session, row: DetectionSubmission, verdict: str, reason: str) -> None:
    # tenant-safe: the row the caller's own request reserved (submit_detection), re-read.
    row = db.get(DetectionSubmission, row.id)
    row.verdict, row.reason = verdict, reason
    db.commit()


def _credit(db: Session, ex: Exercise, objective_id: uuid.UUID, row: DetectionSubmission, user: CurrentUser) -> bool:
    """Achieve the objective once and re-total; False if it was already achieved."""
    evidence = {
        # Staff may try detections too (to check a scenario); only a Student's counts as theirs.
        "source": "student_detection" if user.role == UserRole.student else "staff_detection",
        "submission_id": str(row.id),
        "student_id" if user.role == UserRole.student else "user_id": user.id,
        "student" if user.role == UserRole.student else "user": user.display_name,
        "events_matched": row.events_matched,
        "on_target": row.on_target,
    }
    updated = (
        db.query(Objective)
        .filter(Objective.id == objective_id, Objective.achieved.is_(False))
        .update(
            {"achieved": True, "achieved_at": datetime.now(UTC), "evidence": json.dumps(evidence)},
            synchronize_session=False,
        )
    )
    if updated:
        ex.total_score = completion.total(db, ex.id)
    return bool(updated)


@router.get("/{exercise_id}/detections", response_model=list[DetectionOut])
def list_detections(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_READ)),
) -> list[DetectionOut]:
    """Detection attempts on an exercise: a Student's own; every attempt for staff.  **Permission: exercise:read**"""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    q = db.query(DetectionSubmission).filter(DetectionSubmission.exercise_id == ex.id)
    if not _sees_key(user):
        q = q.filter(DetectionSubmission.user_id == uuid.UUID(user.id))
    return [_out(r, user) for r in q.order_by(DetectionSubmission.submitted_at).all()]
