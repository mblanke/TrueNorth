"""TrueNorth Range — Ranges router.

Handles range CRUD, provision/destroy/stop/start lifecycle actions, batch
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
POST   /ranges/{range_id}/topology RANGE_UPDATE + TEMPLATE_CREATE
DELETE /ranges/{range_id}          RANGE_DELETE
POST   /ranges/{range_id}/provision  RANGE_PROVISION
POST   /ranges/{range_id}/destroy    RANGE_DESTROY
POST   /ranges/{range_id}/stop       RANGE_PROVISION
POST   /ranges/{range_id}/start      RANGE_PROVISION
POST   /ranges/batch-provision       RANGE_BATCH_PROVISION
=================================  ==========================
"""

from __future__ import annotations

import contextlib
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import object_store, range_ops
from ..auth import CurrentUser
from ..db import get_db
from ..models import (
    EventState,
    Exercise,
    ExerciseState,
    Range,
    RangeDocument,
    RangeSnapshot,
    RangeState,
    ScheduledEvent,
    Template,
)
from ..models_range_ops import RangeOperation
from ..rbac import Permission, require_permission
from ..schemas import (
    BatchProvisionIn,
    BatchProvisionOut,
    RangeDocumentOut,
    RangeIn,
    RangeListOut,
    RangeOut,
    RangeStatsOut,
    RangeUpdate,
    SnapshotIn,
    SnapshotOut,
)
from ..tenancy import get_owned, owned_or_404

logger = logging.getLogger("truenorth.api.ranges")

router = APIRouter(prefix="/ranges", tags=["ranges"])


# ── Helpers (same as main.py — will centralise later) ──────────────────
def _audit(db: Session, user: CurrentUser, action: str, resource_type: str, resource_id: str, detail: str = "") -> None:
    from ..models import AuditLog

    db.add(
        AuditLog(
            user_id=uuid.UUID(user.id),
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=detail,
        )
    )


def _dispatch_task(task_name: str, *args: Any) -> str | None:
    from ..celery_client import dispatch

    return dispatch(task_name, *args)


# ── CRUD ───────────────────────────────────────────────────────────────
def _tenant_range(db: Session, range_id: uuid.UUID, user: CurrentUser) -> Range:
    """Fetch a range scoped to the caller's tenant, or 404.

    Every by-id lookup must go through here. `list_ranges` filtered on tenant_id but
    the by-id handlers did not, so any tenant could read, modify, provision or DESTROY
    another tenant's range given its UUID.

    404 rather than 403 on a foreign id: "this exists but is not yours" is itself
    disclosure, and it lets a caller enumerate ids across tenants.
    """
    rng = db.query(Range).filter(Range.id == range_id, Range.tenant_id == uuid.UUID(user.tenant_id)).first()
    if not rng:
        raise HTTPException(404, "Range not found")
    return rng


# Snapshot states in which a worker task still owns the row.
_SNAPSHOT_BUSY = ("creating", "restoring")

# A range's record may go only once nothing still needs it:
#  - its VMs are gone, because it was never provisioned or has been destroyed.
#    Deleting the row of a live (or failed) range loses the only handle on VMs that
#    keep running on the hypervisor; /destroy works from `failed` too;
#  - no exercise ran on it. Exercises are training records (scores, AARs), and they
#    keep their range, so a range that hosted one stays as part of that history;
#  - no draft, scheduled or active event still reserves it.
# Snapshot rows go with it: on a destroyed range their hypervisor copies went with
# the VMs. So do its documents, rows and stored bytes.
_DELETABLE_RANGE_STATES = (RangeState.created, RangeState.destroyed)
_RESERVING_EVENT_STATES = (EventState.draft, EventState.scheduled, EventState.active)


RESTORE_STALE_AFTER = timedelta(hours=2)  # well past the worker's hard time limit (~1 h)


def _refuse_while_restoring(db: Session, range_id: uuid.UUID) -> None:
    """409 while any snapshot of the range is being restored.

    A restore does not move the range out of `ready`, so without this a second
    restore or a new snapshot could run over the VMs mid-revert. A restore cannot run
    longer than the worker's hard time limit, so a `restoring` row untouched for
    RESTORE_STALE_AFTER belongs to a worker that died, and no longer blocks the range.
    """
    # tenant-safe: callers pass a range_id they already resolved through _tenant_range().
    busy = (
        db.query(RangeSnapshot.id)
        .filter(
            RangeSnapshot.range_id == range_id,
            RangeSnapshot.snapshot_state == "restoring",
            RangeSnapshot.updated_at > datetime.now(UTC) - RESTORE_STALE_AFTER,
        )
        .first()
    )
    if busy:
        raise HTTPException(409, "A restore of this range is in progress")


@router.post("", response_model=RangeOut, status_code=201)
def create_range(
    body: RangeIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_CREATE)),
) -> Range:
    """Create a new range.  **Permission: range:create**"""
    tmpl = get_owned(db, Template, body.template_id, user, not_found="Template not found")
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
        db.query(Range.state, sqlfunc.count(Range.id)).filter(Range.tenant_id == tenant_id).group_by(Range.state).all()
    )
    by_state = {s.value: c for s, c in state_counts}
    active_ex = (
        db.query(sqlfunc.count(Exercise.id))
        .filter(Exercise.tenant_id == tenant_id, Exercise.state == ExerciseState.running)
        .scalar()
        or 0
    )
    return RangeStatsOut(total_ranges=total, by_state=by_state, total_vms=0, active_exercises=active_ex)


@router.get("/{range_id}", response_model=RangeOut)
def get_range(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> Range:
    """Retrieve a single range.  **Permission: range:read**"""
    rng = _tenant_range(db, range_id, user)
    return rng


@router.put("/{range_id}", response_model=RangeOut)
def update_range(
    body: RangeUpdate,
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_UPDATE)),
) -> Range:
    """Update a range.  **Permission: range:update**"""
    rng = _tenant_range(db, range_id, user)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(rng, field, value)
    db.commit()
    db.refresh(rng)
    _audit(db, user, "update", "range", str(rng.id))
    db.commit()
    return rng


@router.delete("/{range_id}", status_code=204, response_class=Response)
def delete_range(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_DELETE)),
):
    """Delete a range record.  **Permission: range:delete**

    409, with the reason, while something still depends on the range (see
    `_DELETABLE_RANGE_STATES`). This used to let the database refuse instead, so a
    range with an exercise or a scheduled event came back as a 500.
    """
    rng = _tenant_range(db, range_id, user)
    if rng.state not in _DELETABLE_RANGE_STATES:
        raise HTTPException(
            409, f"Range is {rng.state.value}; destroy it first so its VMs are torn down, then delete it"
        )
    # tenant-safe: rng came from _tenant_range(), so what references it is the caller's.
    exercises = db.query(Exercise.id).filter(Exercise.range_id == range_id).count()
    if exercises:
        raise HTTPException(409, f"Range has {exercises} exercise(s) on record and is kept as part of their history")
    # tenant-safe: as above.
    events = db.query(ScheduledEvent.id).filter(
        ScheduledEvent.range_id == range_id, ScheduledEvent.state.in_(_RESERVING_EVENT_STATES)
    )
    if reserved := events.count():
        raise HTTPException(409, f"Range is reserved by {reserved} scheduled event(s); cancel them first")
    # Completed and cancelled events are history: they keep their row, without the range.
    db.query(ScheduledEvent).filter(ScheduledEvent.range_id == range_id).update(
        {ScheduledEvent.range_id: None}, synchronize_session=False
    )

    # Snapshots and documents cascade with the range (Range.snapshots/.documents).
    # The documents' stored bytes do not, so note them for removal after the commit.
    stored = [doc.minio_key for doc in rng.documents if doc.minio_key]
    db.delete(rng)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # A reference added after this was written; still not a 500.
        logger.exception("Range %s is referenced by a table delete_range does not handle", range_id)
        raise HTTPException(409, "Range is still referenced by other records and cannot be deleted") from None
    for key in stored:
        with contextlib.suppress(Exception):
            object_store.delete_object(key, bucket=RANGE_BUCKET)
    _audit(db, user, "delete", "range", str(range_id))
    db.commit()


# ── Diagram (range designer persistence) ───────────────────────────────
@router.get("/{range_id}/diagram")
def get_range_diagram(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> dict:
    """Retrieve persisted JointJS diagram for a range.  **Permission: range:read**"""
    rng = _tenant_range(db, range_id, user)
    return {"range_id": str(rng.id), "diagram_json": rng.diagram_json or {"cells": []}}


@router.put("/{range_id}/diagram")
def save_range_diagram(
    body: dict,
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_UPDATE)),
) -> dict:
    """Persist JointJS diagram JSON for a range.  **Permission: range:update**

    Accepts the raw output of ``joint.dia.Graph.toJSON()``.
    """
    rng = _tenant_range(db, range_id, user)
    if not isinstance(body, dict):
        raise HTTPException(400, "Diagram body must be a JSON object")
    rng.diagram_json = body
    db.commit()
    db.refresh(rng)
    _audit(db, user, "update", "range.diagram", str(rng.id))
    db.commit()
    return {"range_id": str(rng.id), "diagram_json": rng.diagram_json}


# States in which the range has no VMs, so its topology may be replaced. Changing the
# template under a built range would leave the record describing machines that are
# not the ones running. `failed` qualifies only if nothing was ever built: a failed
# destroy also lands there, with its VMs still up.
_TOPOLOGY_EDITABLE = (RangeState.created, RangeState.destroyed, RangeState.failed)
# Top-level template keys the designer owns; everything else is carried over.
_DESIGNER_KEYS = frozenset({"name", "version", "nodes", "network", "assets", "source"})


def _topology_editable(rng: Range) -> bool:
    if rng.state == RangeState.failed:
        return not (rng.provisioner_output or "").strip()
    return rng.state in _TOPOLOGY_EDITABLE


@router.post("/{range_id}/topology")
def save_range_topology(
    body: dict | None = None,
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_UPDATE, Permission.TEMPLATE_CREATE)),
) -> dict:
    """Make the designer diagram the topology this range provisions.

    **Permission: range:update + template:create**

    Converts the diagram (``body.diagram_json``, else the saved one) into a template
    and points the range at it. The range's own template (created here earlier for
    this range and used by no other range) is updated in place; any other template,
    shared, public, or one a second range also uses, is copied, never modified.
    A copy keeps the source template's other keys and its PO links, so the
    curriculum spine survives. 409 once the range has VMs, checked atomically with
    the switch so a concurrent provision cannot slip in between.
    """
    import yaml as pyyaml

    from .. import range_topology
    from ..models import RangeObjectiveMap

    rng = _tenant_range(db, range_id, user)
    if not _topology_editable(rng):
        raise HTTPException(409, f"Range is {rng.state.value} with VMs; destroy it before changing its topology")
    diagram = body.get("diagram_json") if isinstance(body, dict) else None
    if diagram is None:
        diagram = rng.diagram_json
    if not isinstance(diagram, dict):
        raise HTTPException(422, "No diagram to save: draw or load one first")

    try:
        out = range_topology.diagram_to_template(diagram, rng.name, range_id=str(rng.id))
    except range_topology.TopologyError as exc:
        raise HTTPException(422, str(exc)) from exc
    template = out["template"]
    if not template["nodes"]:
        raise HTTPException(422, {"message": "Diagram has no VMs to provision", "warnings": out["warnings"]})

    # The template the range points at now. Read-only here (keys and PO links are
    # copied from it); it is written only if it passes the ownership test below.
    # tenant-safe: the range is tenant-scoped above; a range may point at its own
    # tenant's template or a shared (public / NULL-tenant) one, never another tenant's.
    current = (
        db.query(Template)
        .filter(
            Template.id == rng.template_id,
            (Template.tenant_id == rng.tenant_id) | Template.tenant_id.is_(None) | Template.is_public.is_(True),
        )
        .first()
    )
    parsed: dict = {}
    if current is not None:
        try:
            loaded = pyyaml.safe_load(current.yaml or "")
            parsed = loaded if isinstance(loaded, dict) else {}
        except pyyaml.YAMLError:
            parsed = {}
    for key, value in parsed.items():
        if key not in _DESIGNER_KEYS:
            template.setdefault(key, value)
    if isinstance(parsed.get("network"), dict):
        for key, value in parsed["network"].items():
            if key != "vlans":
                template["network"].setdefault(key, value)
    text = pyyaml.safe_dump(template, sort_keys=False)

    # Claim the range first: a conditional UPDATE that only matches while the range is
    # still editable. On Postgres it holds the row lock until commit, so a concurrent
    # provision waits and then builds the new topology; if the provision got there
    # first, nothing matches and nothing has been written.
    editable_now = (Range.state.in_([RangeState.created, RangeState.destroyed])) | (
        (Range.state == RangeState.failed)
        & (Range.provisioner_output.is_(None) | (Range.provisioner_output == ""))
    )
    claimed = (
        db.query(Range)
        .filter(Range.id == rng.id, Range.tenant_id == rng.tenant_id, editable_now)
        .update({Range.diagram_json: diagram}, synchronize_session=False)
    )
    if claimed != 1:
        raise HTTPException(409, "Range changed state while saving; reload and try again")

    src = parsed.get("source") if isinstance(parsed.get("source"), dict) else {}
    owned = (
        current is not None
        and current.tenant_id == rng.tenant_id
        and not current.is_public
        and src.get("range_id") == str(rng.id)
        and db.query(Range).filter(Range.template_id == current.id, Range.id != rng.id).count() == 0
    )

    if owned:
        current.yaml = text
        tmpl = current
    else:
        tmpl = Template(name=f"{rng.name} (designer)", version="1.0", yaml=text,
                        tenant_id=rng.tenant_id, is_public=False)
        db.add(tmpl)
        db.flush()
        if current is not None:
            for m in db.query(RangeObjectiveMap).filter(RangeObjectiveMap.template_id == current.id).all():
                db.add(RangeObjectiveMap(template_id=tmpl.id, po_id=m.po_id, source=m.source, tenant_id=m.tenant_id))

    db.query(Range).filter(Range.id == rng.id, Range.tenant_id == rng.tenant_id).update(
        {Range.template_id: tmpl.id}, synchronize_session=False
    )
    _audit(db, user, "update", "range.topology", str(rng.id),
           detail=f"template={tmpl.id} nodes={len(template['nodes'])} created={not owned}")
    db.commit()
    return {
        "range_id": str(rng.id),
        "template_id": str(tmpl.id),
        "created": not owned,
        "node_count": len(template["nodes"]),
        "vlan_count": len(template["network"]["vlans"]),
        "warnings": out["warnings"],
        "template": template,
    }


# ── Lifecycle Actions ──────────────────────────────────────────────────
_OPERATION_RESPONSES: dict = {
    202: {"description": "Accepted: the operation is durably recorded (Operation-Id / Location headers). "
                         "It may still be waiting for the task queue; see the operation's status."},
    409: {"description": "Not allowed in the range's state, another operation is in flight, "
                         "or the Idempotency-Key was used for a different request"},
}


def _range_operation(
    action: str, range_id: uuid.UUID, idempotency_key: str | None, db: Session, user: CurrentUser,
    response: Response,
) -> Range:
    """Accept a provision/destroy/stop/start: operation + state change in one commit, then dispatch.

    202 means accepted, not done: the body is the range as it is now (``provisioning`` /
    ``destroying``), and the operation (headers) carries the request's progress. A
    failed commit accepts nothing. Broker downtime leaves the operation pending and
    visibly delayed; it is re-sent when the broker is back (app/range_ops.py).
    """
    try:
        op, rng, created = range_ops.accept(db, range_id, user, action, idempotency_key)
        if created:
            _audit(db, user, action, "range", str(rng.id), f"operation {op.id} generation {op.generation}")
            db.commit()
    except HTTPException:
        raise  # refused before anything was written; closing the session releases the row lock
    except Exception:
        db.rollback()  # nothing is accepted unless the operation and the state change both commit
        raise
    if created:
        range_ops.dispatch(db, op)
    response.headers["Operation-Id"] = str(op.id)
    response.headers["Location"] = f"/ranges/{rng.id}/operations/{op.id}"
    db.refresh(rng)
    return rng


@router.post("/{range_id}/provision", response_model=RangeOut, status_code=202, responses=_OPERATION_RESPONSES)
async def provision_range(
    response: Response,
    range_id: uuid.UUID = Path(...),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=255),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_PROVISION)),
) -> Range:
    """Provision a range (async worker task).  **Permission: range:provision**"""
    return _range_operation("provision", range_id, idempotency_key, db, user, response)


@router.post("/{range_id}/destroy", response_model=RangeOut, status_code=202, responses=_OPERATION_RESPONSES)
async def destroy_range(
    response: Response,
    range_id: uuid.UUID = Path(...),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=255),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_DESTROY)),
) -> Range:
    """Destroy a range (async worker task).  **Permission: range:destroy**"""
    return _range_operation("destroy", range_id, idempotency_key, db, user, response)


@router.get("/{range_id}/operations", response_model=list[range_ops.RangeOperationOut])
def list_range_operations(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> list[RangeOperation]:
    """The range's operations, newest first, with outcomes reconciled from its state."""
    rng = _tenant_range(db, range_id, user)
    range_ops.reconcile(db, rng)
    db.commit()
    return (
        db.query(RangeOperation)
        .filter(RangeOperation.range_id == rng.id)
        .order_by(RangeOperation.generation.desc())
        .limit(100)
        .all()
    )


@router.get("/{range_id}/operations/{operation_id}", response_model=range_ops.RangeOperationOut)
def get_range_operation(
    range_id: uuid.UUID = Path(...),
    operation_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> RangeOperation:
    rng = _tenant_range(db, range_id, user)
    range_ops.reconcile(db, rng)
    db.commit()
    op = (
        db.query(RangeOperation)
        .filter(
            RangeOperation.id == operation_id,
            RangeOperation.range_id == rng.id,
            RangeOperation.tenant_id == rng.tenant_id,
        )
        .first()
    )
    if not op:
        raise HTTPException(404, "Operation not found")
    return op


@router.post("/{range_id}/operations/{operation_id}/abandon", response_model=range_ops.RangeOperationOut)
def abandon_range_operation(
    range_id: uuid.UUID = Path(...),
    operation_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_DESTROY)),
) -> RangeOperation:
    """Give up on an in-flight operation that will not finish (lost task, dead worker).

    Check the hypervisor first: the API cannot see whether work is still running there.
    The range goes to ``failed``, from where it can be destroyed or provisioned again.
    **Permission: range:destroy**
    """
    rng = _tenant_range(db, range_id, user)
    op = (
        db.query(RangeOperation)
        .filter(
            RangeOperation.id == operation_id,
            RangeOperation.range_id == rng.id,
            RangeOperation.tenant_id == rng.tenant_id,
        )
        .with_for_update()
        .first()
    )
    if not op:
        raise HTTPException(404, "Operation not found")
    range_ops.abandon(db, rng, op, user)
    _audit(db, user, "abandon_operation", "range", str(rng.id), f"operation {op.id}")
    db.commit()
    return op


class NetworkReservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    domain: str
    kind: str
    value: str
    holder: str


@router.get("/{range_id}/network-reservations", response_model=list[NetworkReservationOut])
def list_network_reservations(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> list:
    """Addresses and VLANs this range holds on shared networks (app/network_inventory.py)."""
    rng = _tenant_range(db, range_id, user)
    from ..models_network import NetworkReservation

    return (
        db.query(NetworkReservation)
        .filter(NetworkReservation.range_id == rng.id, NetworkReservation.tenant_id == rng.tenant_id)
        .order_by(NetworkReservation.kind, NetworkReservation.holder)
        .all()
    )


@router.post("/{range_id}/stop", response_model=RangeOut, status_code=202, responses=_OPERATION_RESPONSES)
async def stop_range(
    response: Response,
    range_id: uuid.UUID = Path(...),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=255),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_PROVISION)),
) -> Range:
    """Power off a range's VMs (an operation; the range is ``stopping`` until the worker
    reports ``stopped``).  **Permission: range:provision**"""
    _refuse_while_restoring(db, _tenant_range(db, range_id, user).id)
    return _range_operation("stop", range_id, idempotency_key, db, user, response)


@router.post("/{range_id}/start", response_model=RangeOut, status_code=202, responses=_OPERATION_RESPONSES)
async def start_range(
    response: Response,
    range_id: uuid.UUID = Path(...),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=255),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_PROVISION)),
) -> Range:
    """Power a stopped range's VMs back on (an operation; the range is ``starting`` until
    the worker reports ``running``).  **Permission: range:provision**"""
    _refuse_while_restoring(db, _tenant_range(db, range_id, user).id)
    return _range_operation("start", range_id, idempotency_key, db, user, response)


@router.post("/batch-provision", response_model=BatchProvisionOut, status_code=202)
def batch_provision_ranges(
    body: BatchProvisionIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_BATCH_PROVISION)),
) -> BatchProvisionOut:
    """Batch-provision multiple ranges.  **Permission: range:batch_provision**

    Each range gets its own provision operation (app/range_ops.py), all accepted in one
    transaction: one refusal (not the caller's, wrong state, already busy) accepts none.
    Ranges are locked in id order so two overlapping batches cannot deadlock. The
    worker only provisions a range the API moved to ``provisioning``, so the old
    single ``batch_provision`` task, which did not, is no longer sent. ``task_id`` now
    carries the operations' ids, comma-separated.
    """
    # owned_or_404 refuses partial results: a batch must not silently act on the
    # subset the caller happens to own.
    owned_or_404(db, Range, body.range_ids, user)
    try:
        ordered = sorted(set(body.range_ids), key=str)
        for rid in ordered:  # every range passes before any operation is written
            range_ops.check(db, rid, user, "provision")
        ops = [range_ops.accept(db, rid, user, "provision")[0] for rid in ordered]
        _audit(db, user, "batch_provision", "range", f"{len(ops)} ranges")
        db.commit()
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        raise
    for op in ops:
        range_ops.dispatch(db, op)
    return BatchProvisionOut(dispatched=len(ops), task_id=",".join(str(op.id) for op in ops))


# ── Snapshots ──────────────────────────────────────────────────────────


@router.get("/{range_id}/snapshots", response_model=list[SnapshotOut])
def list_snapshots(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> list[SnapshotOut]:
    rng = _tenant_range(db, range_id, user)
    return (
        db.query(RangeSnapshot)
        .filter(RangeSnapshot.range_id == range_id)
        .order_by(RangeSnapshot.created_at.desc())
        .all()
    )


@router.post("/{range_id}/snapshots", response_model=SnapshotOut, status_code=status.HTTP_202_ACCEPTED)
def create_snapshot(
    payload: SnapshotIn,
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_PROVISION)),
) -> SnapshotOut:
    """Create a snapshot of the current range state."""
    rng = _tenant_range(db, range_id, user)
    if rng.state not in (RangeState.ready, RangeState.stopped):
        raise HTTPException(409, f"Cannot snapshot range in state '{rng.state.value}'")
    _refuse_while_restoring(db, range_id)

    snap = RangeSnapshot(
        range_id=range_id,
        name=payload.name,
        description=payload.description,
        range_state_at_snapshot=rng.state.value,
        tenant_id=rng.tenant_id,
        snapshot_state="creating",
    )
    db.add(snap)
    db.flush()
    _dispatch_task("snapshot_range", str(range_id), str(snap.id))
    _audit(db, user, "snapshot_create", "range_snapshot", str(snap.id), f"Snapshot of range {range_id}")
    db.commit()
    db.refresh(snap)
    return snap


@router.post(
    "/{range_id}/snapshots/{snapshot_id}/restore", response_model=RangeOut, status_code=status.HTTP_202_ACCEPTED
)
def restore_snapshot(
    range_id: uuid.UUID = Path(...),
    snapshot_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_PROVISION)),
) -> RangeOut:
    """Restore a range from a snapshot."""
    rng = _tenant_range(db, range_id, user)
    if rng.state not in (RangeState.ready, RangeState.stopped, RangeState.failed):
        raise HTTPException(409, f"Cannot restore range in state '{rng.state.value}'")
    _refuse_while_restoring(db, range_id)

    # tenant-safe: _tenant_range() above already 404s unless `range_id` belongs to the
    # caller, so filtering snapshots by that same range_id is transitively scoped.
    snap = (
        db.query(RangeSnapshot)
        .filter(
            RangeSnapshot.id == snapshot_id,
            RangeSnapshot.range_id == range_id,
            RangeSnapshot.snapshot_state == "ready",
        )
        .first()
    )
    if not snap:
        raise HTTPException(404, "Snapshot not found or not ready")

    snap.snapshot_state = "restoring"
    _dispatch_task("restore_snapshot", str(range_id), str(snapshot_id))
    _audit(db, user, "snapshot_restore", "range_snapshot", str(snapshot_id), f"Restoring range {range_id}")
    db.commit()
    db.refresh(rng)
    return rng


@router.delete("/{range_id}/snapshots/{snapshot_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_snapshot(
    range_id: uuid.UUID = Path(...),
    snapshot_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_DESTROY)),
):
    # The comment below was here without the call it describes, so any tenant could
    # delete any other tenant's snapshot given the two ids. That only flipped a row
    # while the worker's delete was broken; now it removes the hypervisor copy.
    _tenant_range(db, range_id, user)
    # tenant-safe: _tenant_range() above already 404s unless `range_id` belongs to the
    # caller, so filtering snapshots by that same range_id is transitively scoped.
    snap = (
        db.query(RangeSnapshot)
        .filter(
            RangeSnapshot.id == snapshot_id,
            RangeSnapshot.range_id == range_id,
        )
        .first()
    )
    if not snap:
        raise HTTPException(404, "Snapshot not found")
    if snap.snapshot_state in _SNAPSHOT_BUSY:
        # The worker is still writing this row; deleting now let it come back `ready`
        # with nothing on the hypervisor, or left the hypervisor copy orphaned.
        raise HTTPException(409, f"Snapshot is {snap.snapshot_state}; delete it once that finishes")
    _dispatch_task("delete_snapshot", str(range_id), str(snapshot_id))
    snap.snapshot_state = "deleted"
    _audit(db, user, "snapshot_delete", "range_snapshot", str(snapshot_id))
    db.commit()


# ── Description & documents ──────────────────────────────────────────────
#
# A range needed somewhere to say what it actually is — its purpose, the ROE, how
# it is meant to be used. `Range.description` holds that as markdown, editable in
# the designer or imported from a text file; `RangeDocument` keeps supporting
# files (briefing packs, PDFs) with their original bytes intact.

RANGE_BUCKET = "ranges"
DESCRIPTION_SUFFIXES = (".md", ".markdown", ".txt", ".text", ".rst")
MAX_DOC_BYTES = 25 * 1024 * 1024
MAX_DESCRIPTION_CHARS = 200_000


@router.post("/{range_id}/description/import", response_model=RangeOut)
async def import_description(
    file: UploadFile,
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_UPDATE)),
) -> Range:
    """Replace the range description with the contents of a text file.

    Text in, text on the range — the file is not retained, so the description
    stays editable and searchable afterwards. Use the documents endpoints when
    the original file itself needs to be kept.
    """
    rng = _tenant_range(db, range_id, user)
    filename = file.filename or "upload"
    if not filename.lower().endswith(DESCRIPTION_SUFFIXES):
        raise HTTPException(415, f"Expected a text or markdown file, got: {filename}")
    raw = await file.read()
    if not raw:
        raise HTTPException(422, f"{filename} is empty")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, f"{filename} is not valid UTF-8 text") from exc
    if len(text) > MAX_DESCRIPTION_CHARS:
        raise HTTPException(413, "Description exceeds 200,000 characters")

    rng.description = text
    db.commit()
    db.refresh(rng)
    _audit(db, user, "description_import", "range", str(rng.id))
    db.commit()
    return rng


def _tenant_document(
    db: Session, range_id: uuid.UUID, document_id: uuid.UUID, user: CurrentUser
) -> RangeDocument:
    """Fetch an attachment scoped to both its range and the caller's tenant, or 404.

    The range check alone would be enough (callers reach here through
    `_tenant_range`), but the tenant predicate is stated outright so the scoping
    is visible at the query rather than inferred from a caller two frames up.
    """
    doc = (
        db.query(RangeDocument)
        .filter(
            RangeDocument.id == document_id,
            RangeDocument.range_id == range_id,
            RangeDocument.tenant_id == uuid.UUID(user.tenant_id),
        )
        .first()
    )
    if not doc:
        raise HTTPException(404, "Document not found")
    return doc


@router.get("/{range_id}/documents", response_model=list[RangeDocumentOut])
def list_range_documents(
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> list[RangeDocument]:
    """Supporting documents attached to a range."""
    _tenant_range(db, range_id, user)
    return (
        db.query(RangeDocument)
        .filter(RangeDocument.range_id == range_id)
        .order_by(RangeDocument.created_at.desc())
        .all()
    )


@router.post("/{range_id}/documents", response_model=list[RangeDocumentOut], status_code=201)
async def upload_range_documents(
    files: list[UploadFile],
    range_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_UPDATE)),
) -> list[RangeDocument]:
    """Attach one or more supporting files to a range."""
    rng = _tenant_range(db, range_id, user)
    created: list[RangeDocument] = []
    for file in files:
        filename = file.filename or "upload"
        data = await file.read()
        if not data:
            raise HTTPException(422, f"{filename} is empty")
        if len(data) > MAX_DOC_BYTES:
            raise HTTPException(413, f"{filename} exceeds the 25 MB limit")

        doc = RangeDocument(
            range_id=rng.id,
            filename=filename,
            mime_type=file.content_type or "application/octet-stream",
            size_bytes=len(data),
            tenant_id=uuid.UUID(user.tenant_id),
        )
        db.add(doc)
        db.flush()
        doc.minio_key = f"{rng.id}/{doc.id}/{filename}"
        object_store.put_object(doc.minio_key, data, doc.mime_type, bucket=RANGE_BUCKET)
        created.append(doc)

    db.commit()
    for doc in created:
        db.refresh(doc)
    _audit(db, user, "document_upload", "range", str(rng.id))
    db.commit()
    return created


@router.get("/{range_id}/documents/{document_id}")
def download_range_document(
    range_id: uuid.UUID = Path(...),
    document_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_READ)),
) -> Response:
    """Download an attached document in its original form."""
    _tenant_range(db, range_id, user)
    doc = _tenant_document(db, range_id, document_id, user)
    try:
        data = object_store.get_object(doc.minio_key, bucket=RANGE_BUCKET)
    except Exception as exc:  # noqa: BLE001 — object store faults are a 502, not a crash
        logger.error("Could not read %s from object storage: %s", doc.minio_key, exc)
        raise HTTPException(502, "Document storage is unavailable") from exc
    return Response(
        content=data,
        media_type=doc.mime_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{doc.filename}"'},
    )


@router.delete("/{range_id}/documents/{document_id}", status_code=204, response_class=Response)
def delete_range_document(
    range_id: uuid.UUID = Path(...),
    document_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.RANGE_UPDATE)),
):
    """Detach a document and remove its stored bytes."""
    _tenant_range(db, range_id, user)
    doc = _tenant_document(db, range_id, document_id, user)
    with contextlib.suppress(Exception):
        # A missing object must not block detaching the row it points at.
        object_store.delete_object(doc.minio_key, bucket=RANGE_BUCKET)
    db.delete(doc)
    _audit(db, user, "document_delete", "range", str(range_id))
    db.commit()
