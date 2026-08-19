"""TrueNorth Range — Ops Center router.

Provides live exercise collaboration endpoints: annotations CRUD,
shared commands, instructor injects, and ops statistics.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import Response
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..tenancy import get_owned
from ..db import get_db
from ..models import (
    AnalystAnnotation,
    Exercise,
    Objective,
    SharedCommand,
)
from ..rbac import Permission, require_permission
from ..schemas import (
    AnnotationIn,
    AnnotationOut,
    InstructorInjectIn,
    OpsStatsOut,
    SharedCommandIn,
    SharedCommandOut,
)

logger = logging.getLogger("truenorth.api.ops_center")

router = APIRouter(prefix="/ops", tags=["ops-center"])


# ── Annotations ────────────────────────────────────────────────────────


@router.get(
    "/exercises/{exercise_id}/annotations",
    response_model=list[AnnotationOut],
    dependencies=[Depends(require_permission(Permission.EXERCISE_READ))],
)
def list_annotations(
    exercise_id: uuid.UUID = Path(...),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """List annotations for an exercise, most recent first."""
    return (
        db.query(AnalystAnnotation)
        .filter(AnalystAnnotation.exercise_id == exercise_id)
        .order_by(desc(AnalystAnnotation.created_at))
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.post(
    "/exercises/{exercise_id}/annotations",
    response_model=AnnotationOut,
    status_code=201,
    dependencies=[Depends(require_permission(Permission.EXERCISE_CREATE))],
)
async def create_annotation(
    body: AnnotationIn,
    request: Request,
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Create an analyst annotation and broadcast to ops channel."""
    exercise = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")

    annotation = AnalystAnnotation(
        id=uuid.uuid4(),
        exercise_id=exercise_id,
        user_id=user.id,
        user_display_name=user.display_name or user.username,
        content=body.content,
        annotation_type=body.annotation_type,
        severity=body.severity,
        related_event_id=body.related_event_id,
        tags=json.dumps(body.tags),
        created_at=datetime.now(UTC),
    )
    db.add(annotation)
    db.commit()
    db.refresh(annotation)

    # Broadcast to WebSocket channel
    ws_mgr = getattr(request.app.state, "ws_manager", None)
    if ws_mgr:
        await ws_mgr.broadcast(
            f"exercise.{exercise_id}",
            "annotation_created",
            AnnotationOut.model_validate(annotation).model_dump(mode="json"),
        )

    return annotation


@router.delete(
    "/exercises/{exercise_id}/annotations/{annotation_id}",
    status_code=204,
    response_class=Response,
    dependencies=[Depends(require_permission(Permission.EXERCISE_CREATE))],
)
def delete_annotation(
    exercise_id: uuid.UUID = Path(...),
    annotation_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
):
    """Delete an annotation."""
    a = (
        db.query(AnalystAnnotation)
        .filter(
            AnalystAnnotation.id == annotation_id,
            AnalystAnnotation.exercise_id == exercise_id,
        )
        .first()
    )
    if not a:
        raise HTTPException(404, "Annotation not found")
    db.delete(a)
    db.commit()


# ── Shared Commands ────────────────────────────────────────────────────


@router.get(
    "/exercises/{exercise_id}/commands",
    response_model=list[SharedCommandOut],
    dependencies=[Depends(require_permission(Permission.EXERCISE_READ))],
)
def list_shared_commands(
    exercise_id: uuid.UUID = Path(...),
    limit: int = Query(default=50, le=200),
    db: Session = Depends(get_db),
):
    """List shared commands for an exercise."""
    return (
        db.query(SharedCommand)
        .filter(SharedCommand.exercise_id == exercise_id)
        .order_by(desc(SharedCommand.shared_at))
        .limit(limit)
        .all()
    )


@router.post(
    "/exercises/{exercise_id}/commands",
    response_model=SharedCommandOut,
    status_code=201,
    dependencies=[Depends(require_permission(Permission.EXERCISE_CREATE))],
)
async def share_command(
    body: SharedCommandIn,
    request: Request,
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Share a command with other analysts and broadcast to ops channel."""
    cmd = SharedCommand(
        id=uuid.uuid4(),
        exercise_id=exercise_id,
        user_id=user.id,
        user_display_name=user.display_name or user.username,
        command=body.command,
        description=body.description,
        host_tag=body.host_tag,
        shared_at=datetime.now(UTC),
    )
    db.add(cmd)
    db.commit()
    db.refresh(cmd)

    ws_mgr = getattr(request.app.state, "ws_manager", None)
    if ws_mgr:
        await ws_mgr.broadcast(
            f"exercise.{exercise_id}",
            "command_shared",
            SharedCommandOut.model_validate(cmd).model_dump(mode="json"),
        )

    return cmd


# ── Instructor Inject ──────────────────────────────────────────────────


@router.post(
    "/exercises/{exercise_id}/inject",
    response_model=dict,
    dependencies=[Depends(require_permission(Permission.EXERCISE_CREATE))],
)
async def instructor_inject(
    body: InstructorInjectIn,
    request: Request,
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Instructor sends a live inject during an exercise."""
    exercise = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")

    inject_event = {
        "inject_type": body.inject_type,
        "params": body.params,
        "description": body.description,
        "injected_by": str(user.id),
        "injected_at": datetime.now(UTC).isoformat(),
    }

    ws_mgr = getattr(request.app.state, "ws_manager", None)
    if ws_mgr:
        await ws_mgr.broadcast(
            f"exercise.{exercise_id}",
            "instructor_inject",
            inject_event,
        )

    logger.info("Instructor inject exercise=%s type=%s by=%s", exercise_id, body.inject_type, user.id)
    return {"status": "injected", "inject": inject_event}


# ── Ops Stats ──────────────────────────────────────────────────────────


@router.get(
    "/exercises/{exercise_id}/stats",
    response_model=OpsStatsOut,
    dependencies=[Depends(require_permission(Permission.EXERCISE_READ))],
)
def get_ops_stats(
    exercise_id: uuid.UUID = Path(...),
    request: Request = None,
    db: Session = Depends(get_db),
    # permission is enforced by the decorator's dependencies=[...]; this binds the
    # identity so lookups can be tenant-scoped.
    user: CurrentUser = Depends(get_current_user),
):
    """Get live ops statistics for an exercise."""
    exercise = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")

    annotations_count = (
        db.query(func.count(AnalystAnnotation.id)).filter(AnalystAnnotation.exercise_id == exercise_id).scalar()
    )
    commands_count = db.query(func.count(SharedCommand.id)).filter(SharedCommand.exercise_id == exercise_id).scalar()

    # Count objectives
    objectives_total = db.query(func.count(Objective.id)).filter(Objective.exercise_id == exercise_id).scalar()
    objectives_completed = (
        db.query(func.count(Objective.id))
        .filter(
            Objective.exercise_id == exercise_id,
            Objective.achieved.is_(True),
        )
        .scalar()
    )

    # Active analysts via WebSocket
    active_analysts = 0
    ws_mgr = getattr(request.app.state, "ws_manager", None) if request else None
    if ws_mgr:
        channel = f"exercise.{exercise_id}"
        active_analysts = len(ws_mgr.channels.get(channel, set()))

    # Elapsed time
    elapsed = 0
    if exercise.started_at:
        now = datetime.now(UTC)
        started = exercise.started_at if exercise.started_at.tzinfo else exercise.started_at.replace(tzinfo=UTC)
        elapsed = int((now - started).total_seconds())

    return OpsStatsOut(
        exercise_id=exercise_id,
        active_analysts=active_analysts,
        annotations_count=annotations_count or 0,
        shared_commands_count=commands_count or 0,
        objectives_completed=objectives_completed or 0,
        objectives_total=objectives_total or 0,
        elapsed_seconds=max(elapsed, 0),
    )
