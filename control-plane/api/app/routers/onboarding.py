"""Post-approval first-run flow for a newly created trainee.

Only reachable once someone has a ``users`` row, so this depends on
``get_current_user`` in the normal way. The state machine is linear:

    not_started -> profile -> path -> tour -> complete

``POST /onboarding/profile`` exists as its own endpoint rather than reusing
``PATCH /admin/users/{id}`` because ``UserRole.student`` holds no
``USER_UPDATE`` permission — a trainee genuinely cannot edit their own record
through the admin router. It accepts a deliberately narrow field set: a trainee
may describe themselves, never promote themselves.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..enrollment import ensure_enrollment, ensure_path_enrollment
from ..models import Course, Enrollment, LearningPath, Qualification, User
from ..schemas import OnboardingPathIn, OnboardingProfileIn, OnboardingStateOut
from ..tenancy import get_owned_or_global

logger = logging.getLogger("truenorth.onboarding")

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

STATES = ("not_started", "profile", "path", "tour", "complete")

# Fields a trainee is prompted to complete. Used only to tell the UI what is
# still blank — none of them are enforced as mandatory.
PROFILE_FIELDS = ("rank", "service_branch", "unit", "callsign", "nation_id", "timezone")


def _load(db: Session, user: CurrentUser) -> User:
    row = db.query(User).filter(User.id == uuid.UUID(user.id)).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return row


def _data(row: User) -> dict:
    try:
        parsed = json.loads(row.onboarding_data or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _save_data(row: User, data: dict) -> None:
    row.onboarding_data = json.dumps(data)


def _mark_step(row: User, step: str) -> None:
    data = _data(row)
    steps = [s for s in data.get("steps_done", []) if isinstance(s, str)]
    if step not in steps:
        steps.append(step)
    data["steps_done"] = steps
    _save_data(row, data)
    if row.onboarding_state in ("not_started", "", None):
        row.onboarding_state = step


def _state_out(db: Session, row: User) -> OnboardingStateOut:
    data = _data(row)
    missing = [f for f in PROFILE_FIELDS if not getattr(row, f, None)]
    enrolled = db.query(Enrollment).filter(Enrollment.user_id == row.id).count()

    qual_id = data.get("qualification_id")
    path_id = data.get("learning_path_id")
    return OnboardingStateOut(
        state=row.onboarding_state or "not_started",
        steps_done=[s for s in data.get("steps_done", []) if isinstance(s, str)],
        missing_profile_fields=missing,
        qualification_id=uuid.UUID(qual_id) if qual_id else None,
        learning_path_id=uuid.UUID(path_id) if path_id else None,
        enrolled_course_count=enrolled,
        onboarded_at=row.onboarded_at,
    )


def default_progression() -> str:
    """The developmental progression a new trainee starts on.

    Defaults to DP1 (which leads into DP2). Configurable rather than hardcoded
    so a deployment with a different programme spine does not need a code change.
    """
    return os.getenv("REGISTRATION_DEFAULT_PROGRESSION", "DP1").strip() or "DP1"


@router.get("/state", response_model=OnboardingStateOut)
def get_state(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> OnboardingStateOut:
    """Where the caller is in first-run.  **Permission: any authenticated user**"""
    return _state_out(db, _load(db, user))


@router.post("/profile", response_model=OnboardingStateOut)
def complete_profile(
    body: OnboardingProfileIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> OnboardingStateOut:
    """Self-service profile completion.

    Never accepts role, tenant_id, clearance_level or is_active — those are set
    by approval and by the directory, not by the person being described.
    """
    row = _load(db, user)
    payload = body.model_dump(exclude_unset=True)
    for field in PROFILE_FIELDS:
        if field in payload and payload[field] is not None:
            setattr(row, field, payload[field])
    _mark_step(row, "profile")
    row.onboarding_state = "path" if row.onboarding_state in ("not_started", "profile") else row.onboarding_state
    db.commit()
    db.refresh(row)
    return _state_out(db, row)


@router.post("/select-path", response_model=OnboardingStateOut)
def select_path(
    body: OnboardingPathIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> OnboardingStateOut:
    """Enroll the caller on a qualification or learning path. Idempotent."""
    if not body.qualification_id and not body.learning_path_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Provide either qualification_id or learning_path_id",
        )

    row = _load(db, user)
    tenant_id = uuid.UUID(user.tenant_id)
    enrolled = 0

    if body.learning_path_id:
        # Scoped: a trainee must not be able to enrol on another tenant's path
        # by guessing its id. Shared catalogue rows (tenant_id NULL) are allowed.
        get_owned_or_global(
            db, LearningPath, body.learning_path_id, user, not_found="Learning path not found"
        )
        enrolled += len(
            ensure_path_enrollment(
                db, user_id=row.id, learning_path_id=body.learning_path_id, tenant_id=tenant_id
            )
        )

    if body.qualification_id:
        get_owned_or_global(
            db, Qualification, body.qualification_id, user, not_found="Qualification not found"
        )
        # tenant-safe: reached only through a qualification already scoped above.
        courses = db.query(Course).filter(Course.qualification_id == body.qualification_id).all()
        for course in courses:
            ensure_enrollment(db, user_id=row.id, course_id=course.id, tenant_id=tenant_id)
            enrolled += 1

    data = _data(row)
    if body.qualification_id:
        data["qualification_id"] = str(body.qualification_id)
    if body.learning_path_id:
        data["learning_path_id"] = str(body.learning_path_id)
    _save_data(row, data)
    _mark_step(row, "path")
    row.onboarding_state = "tour"
    db.commit()
    db.refresh(row)
    logger.info("User %s selected a path (%d courses enrolled)", row.id, enrolled)
    return _state_out(db, row)


@router.post("/complete", response_model=OnboardingStateOut)
def complete(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> OnboardingStateOut:
    """Mark first-run finished."""
    row = _load(db, user)
    _mark_step(row, "tour")
    row.onboarding_state = "complete"
    row.onboarded_at = datetime.now(UTC)
    db.commit()
    db.refresh(row)
    return _state_out(db, row)


@router.post("/skip", response_model=OnboardingStateOut)
def skip(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> OnboardingStateOut:
    """Dismiss first-run without completing it.

    Exists because onboarding is a hard gate in the UI: an administrator doing a
    first login should not be trapped behind a trainee profile wizard.
    """
    row = _load(db, user)
    row.onboarding_state = "complete"
    row.onboarded_at = datetime.now(UTC)
    data = _data(row)
    data["skipped"] = True
    _save_data(row, data)
    db.commit()
    db.refresh(row)
    return _state_out(db, row)
