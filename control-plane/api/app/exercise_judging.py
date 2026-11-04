"""Instructors judge a team exercise's objectives (ADR 0020).

The only way an exercise objective is achieved: an instructor acknowledges it, with a note
and, optionally, the Student detections they relied on (``cites``). A cited submission
becomes ``cited``, which is how a Student's part in a team result is recorded (class
closeout counts them). An instructor can withdraw a judgement while the exercise is open;
the submissions it cited go back to ``recorded``.

Both run under the exercise row lock and re-total the score in the same transaction, so a
``/complete`` (or the duration clock) either includes the change or has already closed the
exercise and refuses it.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import exercise_completion as completion
from .auth import CurrentUser
from .detections.models import ACHIEVED, CITED, RECORDED, DetectionSubmission
from .models import Exercise, ExerciseState, Objective

OPEN = (ExerciseState.running, ExerciseState.paused)
MAX_CITES = 20


class JudgementIn(BaseModel):
    evidence: str | None = Field(None, max_length=4000, description="The instructor's note: what the team did")
    cites: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=MAX_CITES,
        description="Detection submissions on this objective, in this run, that the judgement relies on",
    )


def _open_objective(db: Session, exercise_id: uuid.UUID, ref_id: str) -> tuple[Exercise, Objective]:
    """The locked exercise (already tenant-checked by the caller) and its objective, or 404/409."""
    ex = completion.lock(db, exercise_id)
    if ex is None:
        raise HTTPException(404, "Exercise not found")
    if ex.state not in OPEN:
        raise HTTPException(409, f"Exercise is {ex.state.value}, expected running or paused")
    obj = db.query(Objective).filter(Objective.exercise_id == ex.id, Objective.ref_id == ref_id).first()
    if obj is None:
        raise HTTPException(404, "Objective not found")
    return ex, obj


def _cited(db: Session, ex: Exercise, ref_id: str, ids: list[uuid.UUID]) -> list[DetectionSubmission]:
    """The submissions an acknowledgement cites: each on this objective, in this run, recorded."""
    unique = list(dict.fromkeys(ids))
    if not unique:
        return []
    rows = (
        db.query(DetectionSubmission)
        .filter(
            DetectionSubmission.id.in_(unique),
            DetectionSubmission.tenant_id == ex.tenant_id,
            DetectionSubmission.exercise_id == ex.id,
            DetectionSubmission.objective_ref == ref_id,
            DetectionSubmission.verdict.in_((RECORDED, CITED)),
        )
        .all()
    )
    start = ex.started_at if ex.started_at is None or ex.started_at.tzinfo else ex.started_at.replace(tzinfo=UTC)
    rows = [r for r in rows if start is None or _aware(r.window_start) >= start]
    if len(rows) != len(unique):
        raise HTTPException(422, "Every cited submission must be a recorded detection on this objective in this run")
    return rows


def _aware(when: datetime | None) -> datetime:
    if when is None:
        return datetime.min.replace(tzinfo=UTC)
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def acknowledge(db: Session, exercise_id: uuid.UUID, ref_id: str, body: JudgementIn, user: CurrentUser) -> Objective:
    """Achieve the objective on the instructor's judgement; record who, why and what they relied on."""
    ex, obj = _open_objective(db, exercise_id, ref_id)
    if obj.achieved:
        raise HTTPException(409, "Objective already achieved")
    rows = _cited(db, ex, ref_id, body.cites)
    who = user.display_name or user.email or user.id
    obj.achieved = True
    obj.achieved_at = datetime.now(UTC)
    obj.evidence = json.dumps(
        {
            "source": "instructor",
            "by": who,
            "by_id": user.id,
            "note": body.evidence or "",
            "cites": [str(r.id) for r in rows],
            "students": sorted({str(r.user_id) for r in rows}),
        }
    )
    for r in rows:
        r.verdict = CITED
    db.flush()
    completion.retotal(db, ex)
    return obj


def withdraw(db: Session, exercise_id: uuid.UUID, ref_id: str) -> Objective:
    """Take a judgement back while the exercise is open; its cited submissions are recorded again."""
    ex, obj = _open_objective(db, exercise_id, ref_id)
    if not obj.achieved:
        raise HTTPException(409, "Objective is not achieved")
    try:
        judged = json.loads(obj.evidence or "{}")
    except ValueError:
        judged = {}
    judged = judged if isinstance(judged, dict) else {}
    # This judgement's cites, or, for credit given automatically before ADR 0020, the one
    # submission that earned it: either way it no longer credits anyone.
    cites = list(judged.get("cites") or []) + ([judged["submission_id"]] if judged.get("submission_id") else [])
    ids = [uuid.UUID(c) for c in cites if isinstance(c, str) and _is_uuid(c)]
    if ids:
        db.query(DetectionSubmission).filter(
            DetectionSubmission.id.in_(ids),
            DetectionSubmission.tenant_id == ex.tenant_id,
            DetectionSubmission.exercise_id == ex.id,
            DetectionSubmission.verdict.in_((CITED, ACHIEVED)),
        ).update({"verdict": RECORDED}, synchronize_session=False)
    obj.achieved, obj.achieved_at, obj.evidence = False, None, None
    db.flush()
    completion.retotal(db, ex)
    return obj


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
