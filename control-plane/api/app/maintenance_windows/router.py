"""Maintenance windows and emergency teardown (ADR 0013 §7, range certification S7).

=================================================  =====================================
Endpoint                                           Permission
=================================================  =====================================
GET    /maintenance-windows                        schedule:read (staff)
GET    /maintenance-windows/active                 range:read (the Ranges/Schedule banner)
GET    /maintenance-windows/{id}                   schedule:read
POST   /maintenance-windows                        range:admin (+ platform admin: platform-wide)
PUT    /maintenance-windows/{id}                   range:admin (+ platform admin: platform-wide)
DELETE /maintenance-windows/{id}                   range:admin (+ platform admin: platform-wide)
POST   /ranges/{range_id}/emergency-teardown       range:admin, with a reason
=================================================  =====================================

A tenant's windows cover its own ranges; platform-wide ones (``tenant_id`` null) cover
every tenant's. Every caller sees its tenant's windows and the platform-wide ones; another
tenant's windows are not found (404).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..models import AuditLog, Range, RangeState
from ..range_certification import service as certification
from ..range_ops import service as ops
from ..rbac import Permission, require_permission
from ..schemas import RangeOut
from ..tenancy import tenant_uuid
from . import policy, service
from .models import MaintenanceWindow
from .schemas import EmergencyTeardownIn, MaintenanceWindowIn, MaintenanceWindowListOut, MaintenanceWindowOut

logger = logging.getLogger("truenorth.api.maintenance_windows")

router = APIRouter(prefix="/maintenance-windows", tags=["maintenance-windows"])
emergency_router = APIRouter(prefix="/ranges", tags=["ranges"])

_READ = require_permission(Permission.SCHEDULE_READ)
_ADMIN = require_permission(Permission.RANGE_ADMIN)


@router.get("", response_model=MaintenanceWindowListOut, summary="List maintenance windows")
def list_windows(
    include_past: bool = Query(False, description="Also windows that have ended"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_READ),
) -> MaintenanceWindowListOut:
    """The caller's tenant's windows and the platform-wide ones, soonest first."""
    q = service.visible(db, user)
    if not include_past:
        q = q.filter(MaintenanceWindow.ends_at > service._now())
    total = q.count()
    rows = q.order_by(MaintenanceWindow.starts_at, MaintenanceWindow.id).offset(offset).limit(limit).all()
    mine = service.own_range_ids(db, user, rows)
    return MaintenanceWindowListOut(items=[service.to_out(db, user, w, mine) for w in rows], total=total)


@router.get("/active", response_model=list[MaintenanceWindowOut], summary="Maintenance windows in effect now")
def active_windows(
    range_id: uuid.UUID | None = Query(None, description="Only the windows that cover this range"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> list[MaintenanceWindowOut]:
    """For the banner on the Ranges and Schedule pages: what is refused now, why and until when."""
    now = service._now()
    if range_id is not None:
        rng = db.query(Range).filter(Range.id == range_id, Range.tenant_id == tenant_uuid(user)).first()
        if rng is None:
            raise HTTPException(404, "Range not found")
        rows = service.covering_range(db, rng, now)
    else:
        rows = [
            w for w in service.visible(db, user)
            .filter(MaintenanceWindow.starts_at <= now, MaintenanceWindow.ends_at > now)
            .order_by(MaintenanceWindow.ends_at)
            .limit(2 * service.MAX_LIVE_WINDOWS)  # the tenant's and the platform's, each capped
            .all()
            if service._in_effect(w, now)
        ]
    mine = service.own_range_ids(db, user, rows)
    return [service.to_out(db, user, w, mine) for w in rows]


@router.get("/{window_id}", response_model=MaintenanceWindowOut, summary="Get a maintenance window")
def get_window(
    window_id: uuid.UUID = Path(...), db: Session = Depends(get_db), user: CurrentUser = Depends(_READ)
) -> MaintenanceWindowOut:
    w = service.get_visible(db, user, window_id)
    return service.to_out(db, user, w, service.own_range_ids(db, user, [w]))


@router.post("", response_model=MaintenanceWindowOut, status_code=201, summary="Create a maintenance window")
def create_window(
    body: MaintenanceWindowIn, db: Session = Depends(get_db), user: CurrentUser = Depends(_ADMIN)
) -> MaintenanceWindowOut:
    """Inside it, provision, reset/restore and normal destroy of the ranges it covers are
    refused (409 with its reason and end time); automated cleanup and emergency teardown
    still run. ``platform_wide`` needs a platform administrator. Audit-logged."""
    w = service.create(db, user, body)
    db.commit()
    db.refresh(w)
    return service.to_out(db, user, w, service.own_range_ids(db, user, [w]))


@router.put("/{window_id}", response_model=MaintenanceWindowOut, summary="Change a maintenance window")
def update_window(
    body: MaintenanceWindowIn,
    window_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_ADMIN),
) -> MaintenanceWindowOut:
    """Audit-logged, with what it was. A window's tenant (or platform-wide) is fixed."""
    w = service.update(db, user, service.get_editable(db, user, window_id), body)
    db.commit()
    db.refresh(w)
    return service.to_out(db, user, w, service.own_range_ids(db, user, [w]))


@router.delete("/{window_id}", status_code=204, response_class=Response, summary="Delete a maintenance window")
def delete_window(
    window_id: uuid.UUID = Path(...), db: Session = Depends(get_db), user: CurrentUser = Depends(_ADMIN)
) -> None:
    """Ends it at once. Audit-logged, with what it was."""
    service.delete(db, user, service.get_editable(db, user, window_id))
    db.commit()


# ── Emergency teardown ─────────────────────────────────────────────────────
_EMERGENCY_RESPONSES: dict = {
    202: {
        "description": "Accepted: the teardown is recorded and runs even inside a maintenance window.",
        "headers": {"Operation-Id": {"description": "The operation's id", "schema": {"type": "string"}}},
    },
    409: {"description": "Not allowed in the range's state, or the range is a lab session's"},
}


@emergency_router.post(
    "/{range_id}/emergency-teardown",
    response_model=RangeOut,
    status_code=202,
    responses=_EMERGENCY_RESPONSES,
    summary="Tear a range down now, inside a maintenance window if need be (range:admin)",
)
def emergency_teardown(
    body: EmergencyTeardownIn,
    response: Response,
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(_ADMIN),
) -> Range:
    """DANGEROUS. A destroy that a maintenance window does not refuse, for a range that must
    come down now. ``confirm_range_id`` repeats the range's id; ``reason`` is mandatory. It
    is audit-logged (``emergency_teardown``, with the reason and any window it overrode) and
    written to the range's certification record as an override. The worker's re-check lets
    it through (``range_operations.window_bypass = emergency``). Otherwise exactly a destroy:
    same state rules, and it supersedes an operation in flight.
    **Permission: range:admin** (admin only)"""
    if body.confirm_range_id != range_id:
        raise HTTPException(422, "confirm_range_id must repeat the range's id")
    rng = db.query(Range).filter(Range.id == range_id, Range.tenant_id == tenant_uuid(user)).first()
    if rng is None:
        raise HTTPException(404, "Range not found")
    from ..lab_sessions.service import lab_range_ids

    if rng.id in lab_range_ids(db, [rng.id]):
        raise HTTPException(409, "This range belongs to a student's lab session; end the lab session (that always runs)")
    overridden = service.covering_range(db, rng)
    try:
        # One teardown is one destroy operation: a destroy already in flight (a person's, held
        # by the window, say) takes the emergency bypass and is sent now; otherwise a new one.
        op = None
        if rng.state == RangeState.destroying:
            rng = ops._locked_range(db, range_id, user)
            op = ops.run_destroy_with_bypass(db, rng, policy.BYPASS_EMERGENCY, force=True)
        if op is not None:
            created = True
        else:
            op, rng, created = ops.accept(db, range_id, user, "destroy", window_bypass=policy.BYPASS_EMERGENCY)
        if created:
            window = f"overrode maintenance window {service.label(overridden[0])} ({overridden[0].reason})" \
                if overridden else "no maintenance window in effect"
            db.add(AuditLog(
                user_id=uuid.UUID(str(user.id)), tenant_id=tenant_uuid(user), action="emergency_teardown",
                resource_type="range", resource_id=str(rng.id),
                detail=f"operation {op.id} generation {op.generation}; {window}; reason: {body.reason}"[:2000],
            ))
            record_override(db, rng, user, body.reason, {
                "operation_id": str(op.id),
                "maintenance_window_id": str(overridden[0].id) if overridden else None,
            })
            db.commit()
    except HTTPException:
        raise  # refused before anything was written
    except Exception:
        db.rollback()
        raise
    if created and op.status in ("pending", "held"):
        ops.dispatch(db, op)
    logger.warning("range %s: emergency teardown by %s: %s", rng.id, user.id, body.reason)
    response.headers["Operation-Id"] = str(op.id)
    db.refresh(rng)
    return rng


def record_override(db: Session, rng: Range, user: CurrentUser, reason: str, detail: dict) -> object | None:
    """Write the emergency teardown into the range's newest certification record (ADR 0013
    §6, ``range_certification.service.record_override``) as an ``override`` entry, with who,
    why, the operation and the window it overrode. A range with no record (never built since
    S6) gets none; the audit entry is the trace then. It never blocks the teardown:
    ``record_override`` writes in a savepoint and never raises."""
    return certification.record_override(db, rng, "override", "emergency_teardown", user, reason, detail)
