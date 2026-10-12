"""Whether a range's lease can be force-released, and the proof (R0-B recovery controls).

A lease tombstone (``abandoned:<action>:<token>``, app/range_ops/service.py fence_lease)
means an abandoned operation's worker *may* still be finishing on the hypervisor. Until
2026-10-12 an admin could delete one at any time, so a hung worker's clones, port groups
or teardown could run on beside the next build, and a destroy queued behind the tombstone
could start at once beside it. The plan of record (R0-B, "no lease is recycled while
resources may still be live") asks for proof instead. What the API can prove:

* no operation of the range is in flight (pending or dispatched) and no snapshot is being
  taken or restored: nothing queued behind the tombstone would start beside it;
* the abandoned worker has stopped renewing: a live worker's heartbeat
  (worker/fencing.py, every ``RANGE_LEASE_HEARTBEAT_SECONDS``) pushes the tombstone's
  expiry past the one the abandon wrote, and a worker past its soft time limit pushes it
  an hour out (``keep``). So: expiry not moved since the abandon, and at least two
  heartbeats elapsed since it. Then the process that held the lease is gone.

Anything short of that is refused with 409 and the quarantine guidance: the range stays
blocked, the tombstone lapses on its own, a destroy can be queued behind it.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..models import Range, RangeSnapshot
from .models import IN_FLIGHT, RangeOperation
from .service import ABANDONED, TOMBSTONE_WORST_CASE, _aware, _now, lease_seconds, reconcile

LeaseKind = Literal["none", "task", "tombstone"]


class LeaseStatusOut(BaseModel):
    """GET /ranges/{id}/lease: who blocks the range, and whether force-release is proven safe."""

    range_id: uuid.UUID
    kind: LeaseKind
    holder: str | None = None
    expires_at: datetime | None = None
    # An unexpired lease refuses every new operation except a destroy, which waits for it.
    blocking: bool = False
    in_flight_operation_id: uuid.UUID | None = None
    in_flight_action: str | None = None
    abandoned_at: datetime | None = None
    force_release_allowed: bool = False
    reason: str
    guidance: str = ""


def heartbeat_seconds() -> float:
    """worker/fencing.py's LEASE_HEARTBEAT_SECONDS (same variable, same default)."""
    default = max(int(lease_seconds()) // 6, 1)
    try:
        return float(os.getenv("RANGE_LEASE_HEARTBEAT_SECONDS", str(default)) or default)
    except ValueError:
        return float(default)


def quarantine_guidance() -> str:
    return (
        "Quarantine the range instead: leave it blocked. The tombstone lapses on its own "
        f"{lease_seconds():.0f} s after the abandoned worker's last renewal; a hung worker holds it at worst "
        f"{TOMBSTONE_WORST_CASE}. Meanwhile check vCenter's recent tasks for this range's VMs and port groups. "
        "A destroy can be queued now: it waits for the lease, then tears down what the range records. If the "
        "range cannot be destroyed, a platform administrator can abandon it, and its VMs and port groups are "
        "then removed by hand."
    )


def _abandoned_operation(db: Session, rng: Range, holder: str) -> RangeOperation | None:
    """The most recent abandoned operation of the tombstone's action (holder ``abandoned:<action>:...``)."""
    rest = holder[len(ABANDONED):]
    action = rest.split(":", 1)[0] if ":" in rest else None
    q = db.query(RangeOperation).filter(RangeOperation.range_id == rng.id, RangeOperation.status == "failed")
    if action:
        q = q.filter(RangeOperation.action == action)
    for op in q.order_by(RangeOperation.finished_at.desc()).limit(20):
        if isinstance(op.error, dict) and op.error.get("code") == "abandoned" and op.finished_at:
            return op
    return None


def assess(db: Session, rng: Range, *, lock: bool = False) -> tuple[LeaseStatusOut, object | None]:
    """The range's lease and whether force-releasing it is proven safe. Reconciles the
    range's operations first (does not commit). Returns the status and the lease row."""
    from ..range_leases import RangeLease

    reconcile(db, rng)
    q = db.query(RangeLease).filter(RangeLease.range_id == rng.id)
    lease = (q.with_for_update().populate_existing() if lock else q).first()
    busy_op = (
        db.query(RangeOperation)
        .filter(RangeOperation.range_id == rng.id, RangeOperation.status.in_(IN_FLIGHT))
        .order_by(RangeOperation.generation.desc())
        .first()
    )
    base = {
        "range_id": rng.id,
        "in_flight_operation_id": busy_op.id if busy_op else None,
        "in_flight_action": busy_op.action if busy_op else None,
    }
    if lease is None:
        return LeaseStatusOut(**base, kind="none", reason="No worker holds this range: there is nothing to release."), None

    now = _now()
    expires = _aware(lease.expires_at)
    expired = expires <= now
    base |= {"holder": lease.holder, "expires_at": expires, "blocking": not expired}
    if not lease.holder.startswith(ABANDONED):
        return LeaseStatusOut(
            **base,
            kind="task",
            reason=(
                "This lease is held by a task that is still the range's, not an abandoned operation's tombstone. "
                f"Abandon its operation first; a dead worker's lease expires on its own within {lease_seconds():.0f} s."
            ),
        ), lease

    def refused(reason: str, **extra) -> tuple[LeaseStatusOut, object]:
        return LeaseStatusOut(**base, **extra, kind="tombstone", reason=reason, guidance=quarantine_guidance()), lease

    if busy_op:
        return refused(
            f"A {busy_op.action} of this range is in flight (operation {busy_op.id}). It waits for this tombstone; "
            "releasing it would start that work beside the abandoned worker's."
        )
    snap = (
        db.query(RangeSnapshot.snapshot_state)
        .filter(RangeSnapshot.range_id == rng.id, RangeSnapshot.snapshot_state.in_(("creating", "restoring")))
        .first()
    )
    if snap:
        return refused(f"A snapshot of this range is {snap[0]}; it would run beside the abandoned worker's work.")
    op = _abandoned_operation(db, rng, lease.holder)
    if op is None:
        return refused(
            "No abandoned operation of this range matches the tombstone, so there is no record of when it was "
            "written and no proof that its worker has stopped."
        )
    abandoned_at = _aware(op.finished_at)
    beat = heartbeat_seconds()
    written = abandoned_at + timedelta(seconds=lease_seconds())
    if expires > written + timedelta(seconds=beat / 2):
        return refused(
            "The abandoned worker is alive: it renewed the tombstone after the abandon (it now runs to "
            f"{expires.isoformat(timespec='seconds')}), so its hypervisor work may still be running.",
            abandoned_at=abandoned_at,
        )
    if expired:
        return LeaseStatusOut(
            **base, kind="tombstone", abandoned_at=abandoned_at, force_release_allowed=True,
            reason="The tombstone has lapsed without renewal; it no longer blocks the range, and releasing it only "
            "tidies up.",
        ), lease
    wait = abandoned_at + timedelta(seconds=2 * beat) - now
    if wait > timedelta(0):
        return refused(
            f"Too soon to tell whether the abandoned worker is alive: a live one renews every {beat:.0f} s. "
            f"Check again in {wait.total_seconds():.0f} s.",
            abandoned_at=abandoned_at,
        )
    return LeaseStatusOut(
        **base, kind="tombstone", abandoned_at=abandoned_at, force_release_allowed=True,
        reason=(
            f"The abandoned worker has not renewed the tombstone in {(now - abandoned_at).total_seconds():.0f} s "
            f"(a live one renews every {beat:.0f} s): its process has stopped, and nothing is queued behind it."
        ),
    ), lease
