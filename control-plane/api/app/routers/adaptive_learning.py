"""TrueNorth Range — Adaptive Learning router.

Provides endpoints for auto-assessed competency results, AI-powered
learning recommendations, and aggregated progress summaries.
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import (
    CompetencyAutoAssessment,
    LearningRecommendation,
)
from ..rbac import Permission, require_permission
from ..schemas import (
    AutoAssessmentOut,
    LearningRecommendationOut,
    ProgressSummaryOut,
)

logger = logging.getLogger("truenorth.api.adaptive")

router = APIRouter(prefix="/adaptive", tags=["adaptive-learning"])


# ── Auto-Assessments ───────────────────────────────────────────────────


@router.get(
    "/users/{user_id}/auto-assessments",
    response_model=list[AutoAssessmentOut],
    dependencies=[Depends(require_permission(Permission.EXERCISE_READ))],
)
def list_auto_assessments(
    user_id: uuid.UUID = Path(...),
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """List competency auto-assessments for a user, most recent first."""
    return (
        db.query(CompetencyAutoAssessment)
        .filter(CompetencyAutoAssessment.user_id == user_id)
        .order_by(desc(CompetencyAutoAssessment.assessed_at))
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.get(
    "/users/{user_id}/auto-assessments/{assessment_id}",
    response_model=AutoAssessmentOut,
    dependencies=[Depends(require_permission(Permission.EXERCISE_READ))],
)
def get_auto_assessment(
    user_id: uuid.UUID = Path(...),
    assessment_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
):
    """Get a specific auto-assessment."""
    rec = (
        db.query(CompetencyAutoAssessment)
        .filter(
            CompetencyAutoAssessment.id == assessment_id,
            CompetencyAutoAssessment.user_id == user_id,
        )
        .first()
    )
    if not rec:
        raise HTTPException(404, "Auto-assessment not found")
    return rec


# ── Learning Recommendations ───────────────────────────────────────────


@router.get(
    "/users/{user_id}/recommendations",
    response_model=list[LearningRecommendationOut],
    dependencies=[Depends(require_permission(Permission.EXERCISE_READ))],
)
def list_recommendations(
    user_id: uuid.UUID = Path(...),
    limit: int = Query(default=10, le=50),
    db: Session = Depends(get_db),
):
    """List AI learning recommendations for a user, most recent first."""
    return (
        db.query(LearningRecommendation)
        .filter(LearningRecommendation.user_id == user_id)
        .order_by(desc(LearningRecommendation.generated_at))
        .limit(limit)
        .all()
    )


@router.get(
    "/users/{user_id}/recommendations/{rec_id}",
    response_model=LearningRecommendationOut,
    dependencies=[Depends(require_permission(Permission.EXERCISE_READ))],
)
def get_recommendation(
    user_id: uuid.UUID = Path(...),
    rec_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
):
    """Get a specific learning recommendation."""
    rec = (
        db.query(LearningRecommendation)
        .filter(
            LearningRecommendation.id == rec_id,
            LearningRecommendation.user_id == user_id,
        )
        .first()
    )
    if not rec:
        raise HTTPException(404, "Recommendation not found")
    return rec


@router.post(
    "/users/{user_id}/recommendations",
    response_model=dict,
    dependencies=[Depends(require_permission(Permission.EXERCISE_CREATE))],
)
def trigger_recommendation(
    user_id: uuid.UUID = Path(...),
    target_role: str = Query(default="", description="Target NICE work role code"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Trigger AI learning recommendation generation (dispatched async via Celery)."""
    from worker.celery_app import app as celery_app

    celery_app.send_task(
        "worker.tasks.generate_learning_recommendation",
        args=[str(user_id), target_role],
        queue="default",
    )
    logger.info("Triggered learning recommendation for user=%s role=%s", user_id, target_role)
    return {"status": "queued", "message": "Learning recommendation generation started"}


# ── Progress Summary ───────────────────────────────────────────────────


@router.get(
    "/users/{user_id}/progress",
    response_model=ProgressSummaryOut,
    dependencies=[Depends(require_permission(Permission.EXERCISE_READ))],
)
def get_progress_summary(
    user_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
):
    """Aggregated progress summary for a user."""
    # Total exercises completed by this user (via auto-assessments)
    assessments = (
        db.query(CompetencyAutoAssessment)
        .filter(CompetencyAutoAssessment.user_id == user_id)
        .order_by(desc(CompetencyAutoAssessment.assessed_at))
        .all()
    )

    total_exercises = len(assessments)
    avg_score = 0.0
    if total_exercises > 0:
        scores = [(a.raw_score / a.max_score * 100) if a.max_score > 0 else 0 for a in assessments]
        avg_score = round(sum(scores) / len(scores), 1)

    # Competency trend: aggregate competency_mappings across all assessments
    competency_counts: dict[str, list[float]] = {}
    for a in assessments:
        mappings = json.loads(a.competency_mappings) if a.competency_mappings else []
        for m in mappings:
            cat = m.get("category", "unknown")
            delta = m.get("delta", 0)
            competency_counts.setdefault(cat, []).append(delta)

    competency_trend = [
        {
            "category": cat,
            "avg_delta": round(sum(deltas) / len(deltas), 2),
            "count": len(deltas),
        }
        for cat, deltas in competency_counts.items()
    ]

    # Sort by avg_delta to find strongest/weakest
    sorted_trend = sorted(competency_trend, key=lambda x: x["avg_delta"], reverse=True)
    strongest = [t["category"] for t in sorted_trend[:3] if t["avg_delta"] > 0]
    weakest = [t["category"] for t in sorted_trend[-3:] if t["avg_delta"] <= 0]

    recent = assessments[:5]

    return ProgressSummaryOut(
        user_id=user_id,
        total_exercises=total_exercises,
        avg_score=avg_score,
        competency_trend=competency_trend,
        strongest_areas=strongest,
        weakest_areas=weakest,
        recent_assessments=[AutoAssessmentOut.model_validate(a) for a in recent],
    )
