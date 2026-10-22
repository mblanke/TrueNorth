"""Reading a range's inventory checks, and an administrator confirming unidentified items
(ADR 0013 §5, range certification S5, issue #243).

**The confirm rule.** A teardown deletes only what the range recorded at clone. Anything
else that carries the range's annotation, ``tn-range`` tag or port group owner mark is
listed as *unidentified* and left alone. An administrator (``range:admin``) may confirm
such items, one by one, by typing each listed managed-object id back. The server checks
every item: it must be in the listed set of the range's latest teardown report, and the
typed id must equal it exactly (no trimming, no case folding). Nothing is ever deleted by
a mark alone, nor by an id that was not listed. Each confirmation is audit-logged with its
reason and written to the range's certification record as an override; the worker then
re-checks each item against vCenter (moid *and* instance UUID, or port group key and owner
mark) before deleting it (worker/range_teardown.py).

Confirming is cleanup after a teardown (the range is ``destroyed`` or ``failed``, nothing
in flight), done by an administrator with a reason, like an emergency teardown, so a
maintenance window does not refuse it (ADR 0013 §7; the teardown bypass rules are
otherwise unchanged).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..celery_client import dispatch
from ..models import AuditLog, Range, RangeState
from ..range_certification import service as certification
from ..range_ops import service as ops
from ..range_ops.models import IN_FLIGHT, RangeOperation
from ..rbac import Permission, user_has_permission
from .models import RangeInventoryCheck
from .schemas import (
    ConfirmUnidentifiedIn,
    DriftCheckOut,
    InventoryItemOut,
    RangeInventoryChecksOut,
    TeardownCheckOut,
)

logger = logging.getLogger("truenorth.range_inventory")

TASK = "delete_confirmed_teardown_items"  # worker/range_teardown.py
CONFIRMABLE_STATES = (RangeState.destroyed, RangeState.failed)
DELETABLE_KINDS = ("vm", "portgroup")


def check(db: Session, rng: Range, kind: str) -> RangeInventoryCheck | None:
    # tenant-safe: ``rng`` was resolved in the caller's tenant; a check row belongs to its range.
    return (db.query(RangeInventoryCheck)
            .filter(RangeInventoryCheck.range_id == rng.id, RangeInventoryCheck.kind == kind).first())


def _items(raw) -> list[dict]:
    return [i for i in (raw if isinstance(raw, list) else []) if isinstance(i, dict)]


def listed(row: RangeInventoryCheck | None) -> dict[str, dict]:
    """The unidentified items of a teardown report, by moid."""
    report = row.report if row is not None and isinstance(row.report, dict) else {}
    return {str(i["moid"]): i for i in _items(report.get("unidentified")) if i.get("moid")}


def confirm_blocked(db: Session, rng: Range, row: RangeInventoryCheck | None) -> str | None:
    """Why unidentified items cannot be confirmed now, or None."""
    if rng.state not in CONFIRMABLE_STATES:
        return (f"The range is {rng.state.value}; unidentified items are confirmed after its teardown "
                "(destroyed, or failed)")
    busy = (db.query(RangeOperation)
            .filter(RangeOperation.range_id == rng.id, RangeOperation.status.in_(IN_FLIGHT)).first())
    if busy is not None:
        return f"A {busy.action} of this range is still in progress"
    if (holder := ops.worker_acting(db, rng.id)) is not None:
        return f"A worker task is acting on the range ({holder.split(':')[0]}); try again when it ends"
    if not listed(row):
        return "The range's last teardown lists no unidentified items"
    return None


def _teardown_out(row: RangeInventoryCheck) -> TeardownCheckOut:
    r = row.report if isinstance(row.report, dict) else {}
    return TeardownCheckOut(
        status=row.status, summary=row.summary, checked_at=row.checked_at, changed_at=row.changed_at,
        verified_at=r.get("verified_at"), legacy=bool(r.get("legacy")), gaps=[str(g) for g in r.get("gaps") or []],
        deleted=_items(r.get("deleted")), remaining=_items(r.get("remaining")),
        unidentified=_items(r.get("unidentified")), not_checked=[str(n) for n in r.get("not_checked") or []],
        confirmed_deleted=_items(r.get("confirmed_deleted")), confirm_skipped=_items(r.get("confirm_skipped")))


def _drift_out(row: RangeInventoryCheck) -> DriftCheckOut:
    r = row.report if isinstance(row.report, dict) else {}
    return DriftCheckOut(
        status=row.status, summary=row.summary, checked_at=row.checked_at, changed_at=row.changed_at,
        findings=_items(r.get("findings")), checked={k: int(v) for k, v in (r.get("checked") or {}).items()},
        gaps=[str(g) for g in r.get("gaps") or []], error=r.get("error"))


def view(db: Session, rng: Range, user: CurrentUser) -> RangeInventoryChecksOut:
    teardown, drift = check(db, rng, "teardown"), check(db, rng, "drift")
    admin = user_has_permission(user, Permission.RANGE_ADMIN)
    why = confirm_blocked(db, rng, teardown) if admin else "Only an administrator (range:admin) confirms items"
    return RangeInventoryChecksOut(
        range_id=rng.id, teardown=_teardown_out(teardown) if teardown else None,
        drift=_drift_out(drift) if drift else None, can_confirm=why is None, confirm_blocked=why)


def confirm(db: Session, rng: Range, user: CurrentUser, body: ConfirmUnidentifiedIn) -> list[dict]:
    """Check every confirmation against the listed set (module docstring), audit it and
    record it as an override. Returns the accepted items. Does not commit."""
    ops.reconcile(db, rng)  # an operation the worker finished, settled first
    row = check(db, rng, "teardown")
    if why := confirm_blocked(db, rng, row):
        raise HTTPException(409, why)
    known = listed(row)
    problems, seen, accepted = [], set(), []
    for item in body.items:
        if item.moid in seen:
            problems.append(f"{item.moid}: listed twice")
        elif item.confirm_moid != item.moid:
            problems.append(f"{item.moid}: the typed id {item.confirm_moid!r} does not match it exactly")
        elif item.moid not in known:
            problems.append(f"{item.moid}: not an unidentified item of this range's last teardown")
        elif known[item.moid].get("kind") not in DELETABLE_KINDS:
            problems.append(f"{item.moid}: a {known[item.moid].get('kind')} is not deleted by TrueNorth; remove it by hand")
        else:
            accepted.append(known[item.moid])
        seen.add(item.moid)
    if problems:
        raise HTTPException(422, {"message": "Nothing was confirmed", "problems": problems})
    summary = ", ".join(f"{i.get('kind')} {i['moid']} ({i.get('name') or '-'})" for i in accepted)
    db.add(AuditLog(user_id=_uuid(user.id), tenant_id=rng.tenant_id, action="confirm_unidentified_teardown_items",
                    resource_type="range", resource_id=str(rng.id),
                    detail=f"confirmed by typed moid: {summary}; reason: {body.reason}"[:2000]))
    certification.record_override(db, rng, "override", "confirm_unidentified_teardown_items", user, body.reason, {
        "items": [{k: i.get(k) for k in ("kind", "moid", "name", "instance_uuid", "key", "marks")} for i in accepted],
    })
    return accepted


def send(rng: Range, user: CurrentUser, accepted: list[dict]) -> str | None:
    """Queue the worker's re-check and delete of the accepted items (None: broker down)."""
    task_id = dispatch(TASK, str(rng.id), [str(i["moid"]) for i in accepted], str(user.id))
    if task_id is None:
        logger.error("range %s: confirmed unidentified items not queued (broker down)", rng.id)
    return task_id


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def item_out(item: dict) -> InventoryItemOut:
    return InventoryItemOut.model_validate({k: v for k, v in item.items() if k in InventoryItemOut.model_fields})
