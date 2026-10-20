"""``GET /ops/exercises/{id}/class``: the live class for the Ops Center (issue #199).

Ops Center's own rule (web ``pageGuard('opsCenter')``): ``exercise:read`` and
``telemetry:read``, so instructors, admins, range ops and observers; Students hold no
``telemetry:read`` and get 403. Another tenant's exercise is 404 (``get_owned``). Detection
queries and precision are shown only to staff who may see the answer key
(``scenario:update``), as ``GET /exercises/{id}/detections`` does.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..models import Exercise
from ..rbac import Permission, require_permission, user_has_permission
from ..tenancy import get_owned
from . import service
from .schemas import ExerciseClassOut

router = APIRouter(prefix="/ops", tags=["ops-center"])


@router.get("/exercises/{exercise_id}/class", response_model=ExerciseClassOut)
def get_exercise_class(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_READ, Permission.TELEMETRY_READ)),
) -> ExerciseClassOut:
    """The roster of a live exercise: each Student's team, lab, detection attempts, credit
    earned from them, last activity and what may need attention.
    **Permission: exercise:read + telemetry:read** (staff and observers; never Students)."""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    return service.build(db, ex, sees_key=user_has_permission(user, Permission.SCENARIO_UPDATE))
