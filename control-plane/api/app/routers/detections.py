"""Student detections in a team exercise: evidence for the instructor, never a grade (ADR 0020).

  POST /exercises/{id}/objectives/{ref}/detections   a participant submits a detection query;
                                                     the server runs it in the exercise
                                                     window and records what it matched
  GET  /exercises/{id}/detections                    submissions: a Student's own, or every
                                                     submission for staff

Nothing here credits an objective: an instructor judges it (``/ack``,
routers/exercises.py) and may cite the submissions they relied on, which marks them
``cited``. There is no answer key to compare with.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import exercise_participants as participants
from ..auth import CurrentUser
from ..db import get_db
from ..detections import evidence
from ..detections.models import PENDING, RECORDED, SUBMISSIONS, DetectionSubmission
from ..models import AuditLog, Exercise, ExerciseState, Objective
from ..rbac import Permission, require_permission, user_has_permission
from ..search_backends import BaseSearchBackend, SearchBackendError, SearchQueryError, get_search_backend
from ..search_backends.query import QueryError
from ..tenancy import get_owned

router = APIRouter(prefix="/exercises", tags=["detections"])
STALE_PENDING = timedelta(minutes=5)
MAX_SUBMISSIONS = 20  # per Student, per objective, per run: bounds the load on the event store
logger = logging.getLogger("truenorth.api.detections")


def search_backend() -> BaseSearchBackend:
    return get_search_backend()


class DetectionIn(BaseModel):
    query: str = Field(
        min_length=1,
        max_length=evidence.MAX_QUERY_LENGTH,
        description="Detection query: field:value terms with AND/OR/NOT, wildcards, "
        "field:>N ranges and field:(a OR b); no free text, no platform label fields",
    )


class DetectionOut(BaseModel):
    """One submission: ``recorded``, or ``cited`` once an instructor's acknowledgement relies on
    it. Rows from before ADR 0020 keep their old verdicts. ``submissions_left`` is set on the
    response to a submission."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    objective_ref: str
    user_id: uuid.UUID
    query: str
    submitted_at: datetime | None
    verdict: str
    events_matched: int | None
    submissions_left: int | None = None


def _staff(user: CurrentUser) -> bool:
    return user_has_permission(user, Permission.SCENARIO_UPDATE)


def _used(db: Session, exercise_id: uuid.UUID, ref_id: str, user_id: uuid.UUID, start: datetime) -> int:
    """Submissions by this Student on this objective in this run (``window_start`` is the run's
    start, so a replay starts again). A pending one older than a few minutes is a request
    that died; it is not counted."""
    stale = datetime.now(UTC) - STALE_PENDING
    return (
        db.query(func.count(DetectionSubmission.id))
        .filter(
            DetectionSubmission.exercise_id == exercise_id,
            DetectionSubmission.objective_ref == ref_id,
            DetectionSubmission.user_id == user_id,
            DetectionSubmission.window_start >= start,
            DetectionSubmission.verdict.in_(SUBMISSIONS),
            (DetectionSubmission.verdict != PENDING) | (DetectionSubmission.submitted_at >= stale),
        )
        .scalar()
    )


@router.post(
    "/{exercise_id}/objectives/{ref_id}/detections",
    response_model=DetectionOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        403: {"description": "The Student is not a participant of the exercise (ADR 0005)"},
        409: {"description": "Exercise not running, objective already achieved, or not a detection objective"},
        422: {"description": "The query is outside the detection language or does not parse; nothing recorded"},
        429: {"description": "The submission bound for this objective is reached"},
        503: {"description": "The event store could not answer; nothing recorded"},
    },
)
async def submit_detection(
    body: DetectionIn,
    exercise_id: uuid.UUID = Path(...),
    ref_id: str = Path(..., max_length=100),
    db: Session = Depends(get_db),
    backend: BaseSearchBackend = Depends(search_backend),
    user: CurrentUser = Depends(require_permission(Permission.DETECTION_SUBMIT)),
) -> DetectionOut:
    """Record a detection for an objective as evidence for the instructor who judges it; it
    credits nothing (ADR 0020).  **Permission: detection:submit**"""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    if not participants.may_submit(db, user, ex):  # ADR 0005 (M3): participants only
        raise HTTPException(403, "You are not a participant of this exercise (enrol in its booked course)")
    if ex.state != ExerciseState.running or ex.started_at is None:
        raise HTTPException(409, f"Exercise is {ex.state.value}; detections are accepted while it is running")
    obj = db.query(Objective).filter(Objective.exercise_id == ex.id, Objective.ref_id == ref_id).first()
    if obj is None:
        raise HTTPException(404, "Objective not found")
    if not evidence.is_detection_objective(obj.validator):
        raise HTTPException(409, "This objective is not worked by a detection")
    if obj.achieved:
        raise HTTPException(409, "Objective already achieved")
    try:
        evidence.student_query(body.query)  # the closed detection grammar; labels refused
    except QueryError as exc:
        raise HTTPException(422, f"Your query is outside the detection language (nothing recorded): {exc}") from exc

    user_id = uuid.UUID(user.id)
    start = ex.started_at if ex.started_at.tzinfo else ex.started_at.replace(tzinfo=UTC)
    row, left = _reserve(db, ex, obj.id, ref_id, user_id, start, body.query)

    try:
        found = await evidence.matches(backend, ex.range_id, body.query, start, row.window_end)
    except SearchQueryError as exc:
        _drop(db, row)
        raise HTTPException(422, f"Your query could not be parsed (nothing recorded): {exc}") from exc
    except SearchBackendError as exc:
        _drop(db, row)
        logger.warning("[detections] exercise %s objective %s not recorded: %s", ex.id, ref_id, exc)
        raise HTTPException(503, "The event store could not run this detection; try again (nothing recorded)") from exc

    # tenant-safe: re-reading the row this request reserved for the caller's own exercise.
    row = db.get(DetectionSubmission, row.id)
    row.verdict, row.events_matched, row.matched_ids = RECORDED, found.events_matched, json.dumps(found.matched_ids)
    db.add(AuditLog(user_id=user_id, action="detection_recorded", resource_type="objective", resource_id=str(obj.id)))
    db.commit()
    db.refresh(row)
    out = DetectionOut.model_validate(row)
    out.submissions_left = left
    return out


def _reserve(db: Session, ex: Exercise, objective_id, ref_id: str, user_id, start: datetime, query: str):
    """Take one of the Student's submissions before the query runs, so concurrent requests
    cannot all see the same count. The objective row lock serialises them (a no-op on SQLite)."""
    db.query(Objective).filter(Objective.id == objective_id).with_for_update().one()
    used = _used(db, ex.id, ref_id, user_id, start)
    if used >= MAX_SUBMISSIONS:
        raise HTTPException(429, f"You have submitted {MAX_SUBMISSIONS} detections for this objective in this run")
    row = DetectionSubmission(
        tenant_id=ex.tenant_id,
        exercise_id=ex.id,
        objective_ref=ref_id,
        user_id=user_id,
        query=query,
        verdict=PENDING,
        window_start=start,
        window_end=datetime.now(UTC),
    )
    db.add(row)
    db.commit()
    return row, MAX_SUBMISSIONS - used - 1


def _drop(db: Session, row: DetectionSubmission) -> None:
    """Nothing is recorded when the query could not run."""
    # tenant-safe: the row the caller's own request reserved (submit_detection), re-read.
    db.query(DetectionSubmission).filter(DetectionSubmission.id == row.id).delete(synchronize_session=False)
    db.commit()


@router.get("/{exercise_id}/detections", response_model=list[DetectionOut])
def list_detections(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_READ)),
) -> list[DetectionOut]:
    """Detection submissions on an exercise: a Student's own; every submission for staff, who
    judge the objectives.  **Permission: exercise:read**"""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    q = db.query(DetectionSubmission).filter(DetectionSubmission.exercise_id == ex.id)
    if not _staff(user):
        q = q.filter(DetectionSubmission.user_id == uuid.UUID(user.id))
    return [DetectionOut.model_validate(r) for r in q.order_by(DetectionSubmission.submitted_at).all()]
