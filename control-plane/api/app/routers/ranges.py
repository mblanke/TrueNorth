"""TrueNorth Range — Ranges router.

Handles range CRUD, provision/destroy/stop lifecycle actions, batch
provisioning (70k-VM scale), and range statistics.

Permissions required per endpoint (enforced via ``rbac.require_permission``):

=================================  ==========================
Endpoint                           Permission(s)
=================================  ==========================
POST   /ranges                     RANGE_CREATE
GET    /ranges                     RANGE_READ
GET    /ranges/stats               STATS_READ
GET    /ranges/{range_id}          RANGE_READ
PUT    /ranges/{range_id}          RANGE_UPDATE
DELETE /ranges/{range_id}          RANGE_DELETE
POST   /ranges/{range_id}/provision  RANGE_PROVISION
POST   /ranges/{range_id}/destroy    RANGE_DESTROY
POST   /ranges/{range_id}/stop       RANGE_PROVISION
POST   /ranges/batch-provision       RANGE_BATCH_PROVISION
=================================  ==========================
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import Exercise, ExerciseState, Range, RangeState, Template, UserRole
from ..rbac import Permission, require_permission, require_range_access
from ..schemas import (
    BatchProvisionIn,
    BatchProvisionOut,
    RangeIn,
    RangeListOut,
    RangeOut,
    RangeStatsOut,
)

logger = logging.getLogger("truenorth.api.ranges")

router = APIRouter(prefix="/ranges", tags=["ranges"])


# ── Helpers (same as main.py — will centralise later) ──────────────────
def _audit(db: Session, user: CurrentUser, action: str, resource_type: str, resource_id: str, detail: str = "") -> None:
    from ..models import AuditLog
    db.add(AuditLog(user_id=uuid.UUID(user.id), action=action, resource_type=resource_type, resource_id=resource_id, detail=detail))


def _dispatch_task(task_name: str, *args: Any) -> str | None:
    try:
        from celery import current_app
        result = current_app.send_task(f"worker.tasks.{task_name}", args=args)
        return result.id
    except Exception:
        return None


# ── CRUD ───────────────────────────────────────────────────────────────
@router.post("", response_model=RangeOut, status_code=201)
def create_range(
    body: RangeIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_CREATE)),
) -> Range:
    """Create a new range.  **Permission: range:create**"""
    tmpl = db.query(Template).filter(Template.id == body.template_id).first()
    if not tmpl:
        raise HTTPException(404, "Template not found")
    rng = Range(
        name=body.name,
        template_id=body.template_id,
        tenant_id=uuid.UUID(user.tenant_id),
        state=RangeState.created,
        provisioner_backend=os.getenv("PROVISIONER_BACKEND", "mock"),
    )
    db.add(rng)
    db.commit()
    db.refresh(rng)
    _audit(db, user, "create", "range", str(rng.id))
    db.commit()
    return rng


@router.get("", response_model=list[RangeListOut])
def list_ranges(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> list[Range]:
    """List ranges for the user's tenant.  **Permission: range:read**"""
    return (
        db.query(Range)
        .filter(Range.tenant_id == uuid.UUID(user.tenant_id))
        .order_by(Range.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.get("/stats", response_model=RangeStatsOut)
def get_range_stats(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.STATS_READ)),
) -> RangeStatsOut:
    """Aggregate range statistics.  **Permission: stats:read**"""
    from sqlalchemy import func as sqlfunc

    tenant_id = uuid.UUID(user.tenant_id)
    total = db.query(sqlfunc.count(Range.id)).filter(Range.tenant_id == tenant_id).scalar() or 0
    state_counts = (
        db.query(Range.state, sqlfunc.count(Range.id))
        .filter(Range.tenant_id == tenant_id)
        .group_by(Range.state)
        .all()
    )
    by_state = {s.value: c for s, c in state_counts}
    active_ex = (
        db.query(sqlfunc.count(Exercise.id))
        .filter(Exercise.tenant_id == tenant_id, Exercise.state == ExerciseState.running)
        .scalar() or 0
    )
    return RangeStatsOut(total_ranges=total, by_state=by_state, total_vms=0, active_exercises=active_ex)


@router.get("/{range_id}", response_model=RangeOut)
def get_range(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> Range:
    """Retrieve a single range.  **Permission: range:read**"""
    rng = db.query(Range).filter(Range.id == range_id).first()
    if not rng:
        raise HTTPException(404, "Range not found")
    return rng


@router.delete("/{range_id}", status_code=204)
def delete_range(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_DELETE)),
) -> None:
    """Delete a range record.  **Permission: range:delete**"""
    rng = db.query(Range).filter(Range.id == range_id).first()
    if not rng:
        raise HTTPException(404, "Range not found")
    db.delete(rng)
    db.commit()
    _audit(db, user, "delete", "range", str(range_id))
    db.commit()


# ── Lifecycle Actions ──────────────────────────────────────────────────
@router.post("/{range_id}/provision", response_model=RangeOut)
async def provision_range(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_PROVISION)),
) -> Range:
    """Provision a range (async Celery task).  **Permission: range:provision**"""
    rng = db.query(Range).filter(Range.id == range_id).first()
    if not rng:
        raise HTTPException(404, "Range not found")
    if not rng.state.can_transition_to(RangeState.provisioning):
        raise HTTPException(409, f"Cannot provision range in state {rng.state.value}")
    rng.state = RangeState.provisioning
    db.commit()
    _dispatch_task("provision_range", str(rng.id))
    _audit(db, user, "provision", "range", str(rng.id))
    db.commit()
    db.refresh(rng)
    return rng


@router.post("/{range_id}/destroy", response_model=RangeOut)
async def destroy_range(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_DESTROY)),
) -> Range:
    """Destroy a range (async Celery task).  **Permission: range:destroy**"""
    rng = db.query(Range).filter(Range.id == range_id).first()
    if not rng:
        raise HTTPException(404, "Range not found")
    if not rng.state.can_transition_to(RangeState.destroying):
        raise HTTPException(409, f"Cannot destroy range in state {rng.state.value}")
    rng.state = RangeState.destroying
    db.commit()
    _dispatch_task("destroy_range", str(rng.id))
    _audit(db, user, "destroy", "range", str(rng.id))
    db.commit()
    db.refresh(rng)
    return rng


@router.post("/{range_id}/stop", response_model=RangeOut)
async def stop_range(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_PROVISION)),
) -> Range:
    """Stop a running range.  **Permission: range:provision**"""
    rng = db.query(Range).filter(Range.id == range_id).first()
    if not rng:
        raise HTTPException(404, "Range not found")
    if not rng.state.can_transition_to(RangeState.stopped):
        raise HTTPException(409, f"Cannot stop range in state {rng.state.value}")
    rng.state = RangeState.stopped
    db.commit()
    db.refresh(rng)
    return rng


@router.post("/batch-provision", response_model=BatchProvisionOut, status_code=202)
def batch_provision_ranges(
    body: BatchProvisionIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_BATCH_PROVISION)),
) -> BatchProvisionOut:
    """Batch-provision multiple ranges.  **Permission: range:batch_provision**"""
    range_ids = [str(rid) for rid in body.range_ids]
    ranges_found = db.query(Range).filter(Range.id.in_(body.range_ids)).all()
    if len(ranges_found) != len(body.range_ids):
        raise HTTPException(400, f"Only {len(ranges_found)} of {len(body.range_ids)} ranges found")
    for rng in ranges_found:
        if not rng.state.can_transition_to(RangeState.provisioning):
            raise HTTPException(409, f"Range {rng.id} in state {rng.state.value} cannot be provisioned")
    task = _dispatch_task("batch_provision", range_ids)
    task_id = task if isinstance(task, str) else "mock-batch"
    _audit(db, user, "batch_provision", "range", f"{len(range_ids)} ranges")
    db.commit()
    return BatchProvisionOut(dispatched=len(range_ids), task_id=task_id)