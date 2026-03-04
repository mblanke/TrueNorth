"""TrueNorth Range — Exercises router.

Handles exercise CRUD, lifecycle transitions (start/pause/complete),
objective tracking, and After-Action Report generation.

Permissions required per endpoint:

==========================================  ==========================
Endpoint                                    Permission(s)
==========================================  ==========================
POST   /exercises                           EXERCISE_CREATE
GET    /exercises                           EXERCISE_READ
GET    /exercises/{id}                      EXERCISE_READ
POST   /exercises/{id}/start               EXERCISE_START
POST   /exercises/{id}/pause               EXERCISE_PAUSE
POST   /exercises/{id}/complete            EXERCISE_COMPLETE
GET    /exercises/{id}/objectives           EXERCISE_READ
POST   /exercises/{id}/objectives/{ref}/ack EXERCISE_COMPLETE
POST   /exercises/{id}/aar/generate        AAR_GENERATE
GET    /exercises/{id}/aar                  AAR_READ
GET    /exercises/{id}/aar/html            AAR_READ
==========================================  ==========================
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import (
    AfterActionReport,
    AuditLog,
    Exercise,
    ExerciseState,
    Objective,
    Range,
    Scenario,
)
from ..rbac import Permission, require_permission
from ..schemas import (
    AAROut,
    ExerciseIn,
    ExerciseListOut,
    ExerciseOut,
    ObjectiveAck,
    ObjectiveOut,
)

logger = logging.getLogger("truenorth.api.exercises")

router = APIRouter(prefix="/exercises", tags=["exercises"])


def _audit(db: Session, user: CurrentUser, action: str, rtype: str, rid: str, detail: str = "") -> None:
    db.add(AuditLog(user_id=uuid.UUID(user.id), action=action, resource_type=rtype, resource_id=rid, detail=detail))


# ── CRUD ───────────────────────────────────────────────────────────────
@router.post("", response_model=ExerciseOut, status_code=201)
def create_exercise(
    body: ExerciseIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_CREATE)),
) -> Exercise:
    """Create a new exercise.  **Permission: exercise:create**"""
    rng = db.query(Range).filter(Range.id == body.range_id).first()
    if not rng:
        raise HTTPException(404, "Range not found")
    sc = db.query(Scenario).filter(Scenario.id == body.scenario_id).first()
    if not sc:
        raise HTTPException(404, "Scenario not found")
    ex = Exercise(
        name=body.name,
        range_id=body.range_id,
        scenario_id=body.scenario_id,
        tenant_id=uuid.UUID(user.tenant_id),
        state=ExerciseState.pending,
        max_score=body.max_score or 100,
    )
    db.add(ex)
    db.commit()
    db.refresh(ex)
    _audit(db, user, "create", "exercise", str(ex.id))
    db.commit()
    return ex


@router.get("", response_model=list[ExerciseListOut])
def list_exercises(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_READ)),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> list[Exercise]:
    """List exercises for user's tenant.  **Permission: exercise:read**"""
    return (
        db.query(Exercise)
        .filter(Exercise.tenant_id == uuid.UUID(user.tenant_id))
        .order_by(Exercise.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.get("/{exercise_id}", response_model=ExerciseOut)
def get_exercise(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_READ)),
) -> Exercise:
    """Retrieve a single exercise.  **Permission: exercise:read**"""
    ex = db.query(Exercise).filter(Exercise.id == exercise_id).first()
    if not ex:
        raise HTTPException(404, "Exercise not found")
    return ex


# ── Lifecycle ──────────────────────────────────────────────────────────
@router.post("/{exercise_id}/start", response_model=ExerciseOut)
async def start_exercise(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_START)),
) -> Exercise:
    """Start a pending exercise.  **Permission: exercise:start**"""
    ex = db.query(Exercise).filter(Exercise.id == exercise_id).first()
    if not ex:
        raise HTTPException(404, "Exercise not found")
    if ex.state != ExerciseState.pending:
        raise HTTPException(409, f"Exercise is {ex.state.value}, expected pending")
    ex.state = ExerciseState.running
    ex.started_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(ex)
    _audit(db, user, "start", "exercise", str(ex.id))
    db.commit()
    return ex


@router.post("/{exercise_id}/pause", response_model=ExerciseOut)
async def pause_exercise(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_PAUSE)),
) -> Exercise:
    """Pause a running exercise.  **Permission: exercise:pause**"""
    ex = db.query(Exercise).filter(Exercise.id == exercise_id).first()
    if not ex:
        raise HTTPException(404, "Exercise not found")
    if ex.state != ExerciseState.running:
        raise HTTPException(409, f"Exercise is {ex.state.value}, expected running")
    ex.state = ExerciseState.paused
    db.commit()
    db.refresh(ex)
    _audit(db, user, "pause", "exercise", str(ex.id))
    db.commit()
    return ex


@router.post("/{exercise_id}/complete", response_model=ExerciseOut)
async def complete_exercise(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_COMPLETE)),
) -> Exercise:
    """Complete an exercise and tally scores.  **Permission: exercise:complete**"""
    ex = db.query(Exercise).filter(Exercise.id == exercise_id).first()
    if not ex:
        raise HTTPException(404, "Exercise not found")
    if ex.state not in (ExerciseState.running, ExerciseState.paused):
        raise HTTPException(409, f"Exercise is {ex.state.value}, cannot complete")
    ex.state = ExerciseState.completed
    ex.completed_at = datetime.now(timezone.utc)
    objectives = db.query(Objective).filter(Objective.exercise_id == ex.id).all()
    ex.total_score = sum(o.points for o in objectives if o.achieved)
    db.commit()
    db.refresh(ex)
    _audit(db, user, "complete", "exercise", str(ex.id))
    db.commit()
    return ex


# ── Objectives ─────────────────────────────────────────────────────────
@router.get("/{exercise_id}/objectives", response_model=list[ObjectiveOut])
def list_objectives(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_READ)),
) -> list[Objective]:
    """List objectives for an exercise.  **Permission: exercise:read**"""
    return db.query(Objective).filter(Objective.exercise_id == exercise_id).all()


@router.post("/{exercise_id}/objectives/{ref_id}/ack", response_model=ObjectiveOut)
async def acknowledge_objective(
    exercise_id: uuid.UUID = Path(...),
    ref_id: str = Path(...),
    body: ObjectiveAck = Depends(),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_COMPLETE)),
) -> Objective:
    """Acknowledge (achieve) an objective.  **Permission: exercise:complete**"""
    obj = db.query(Objective).filter(
        Objective.exercise_id == exercise_id,
        Objective.ref_id == ref_id,
    ).first()
    if not obj:
        raise HTTPException(404, "Objective not found")
    if obj.achieved:
        raise HTTPException(409, "Objective already achieved")
    obj.achieved = True
    obj.evidence = body.evidence or f"Acknowledged by {user.display_name}"
    obj.achieved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(obj)
    return obj


# ── AAR ────────────────────────────────────────────────────────────────
@router.post("/{exercise_id}/aar/generate", response_model=AAROut, status_code=201)
def generate_aar(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.AAR_GENERATE)),
) -> AfterActionReport:
    """Generate After-Action Report.  **Permission: aar:generate**"""
    ex = db.query(Exercise).filter(Exercise.id == exercise_id).first()
    if not ex:
        raise HTTPException(404, "Exercise not found")
    sc = db.query(Scenario).filter(Scenario.id == ex.scenario_id).first()
    objectives = db.query(Objective).filter(Objective.exercise_id == ex.id).all()

    report = {
        "exercise": {
            "id": str(ex.id), "name": ex.name, "state": ex.state.value,
            "started_at": str(ex.started_at) if ex.started_at else None,
            "completed_at": str(ex.completed_at) if ex.completed_at else None,
        },
        "scenario": {"id": str(sc.id), "name": sc.name} if sc else None,
        "scores": {
            "total": ex.total_score, "max": ex.max_score,
            "pct": round(ex.total_score / max(ex.max_score, 1) * 100, 1),
        },
        "objectives": [
            {
                "ref_id": o.ref_id, "type": o.objective_type.value,
                "description": o.description, "points": o.points,
                "achieved": o.achieved, "evidence": o.evidence,
            }
            for o in objectives
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": user.display_name,
    }
    report_json = json.dumps(report, indent=2)
    pct = report["scores"]["pct"]
    html = f"<html><body><h1>AAR: {ex.name}</h1><p>Score: {pct}%</p></body></html>"

    existing = db.query(AfterActionReport).filter(AfterActionReport.exercise_id == ex.id).first()
    if existing:
        existing.report_json = report_json
        existing.report_html = html
        aar = existing
    else:
        aar = AfterActionReport(exercise_id=ex.id, report_json=report_json, report_html=html)
        db.add(aar)
    db.commit()
    db.refresh(aar)
    return aar


@router.get("/{exercise_id}/aar", response_model=AAROut)
def get_aar(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.AAR_READ)),
) -> AfterActionReport:
    """Retrieve AAR JSON.  **Permission: aar:read**"""
    aar = db.query(AfterActionReport).filter(AfterActionReport.exercise_id == exercise_id).first()
    if not aar:
        raise HTTPException(404, "AAR not found — generate it first")
    return aar


@router.get("/{exercise_id}/aar/html", response_class=HTMLResponse)
def get_aar_html(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.AAR_READ)),
) -> HTMLResponse:
    """Retrieve AAR as rendered HTML.  **Permission: aar:read**"""
    aar = db.query(AfterActionReport).filter(AfterActionReport.exercise_id == exercise_id).first()
    if not aar:
        raise HTTPException(404, "AAR not found")
    return HTMLResponse(content=aar.report_html)