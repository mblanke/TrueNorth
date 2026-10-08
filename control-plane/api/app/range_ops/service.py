"""Accept, send and reconcile range operations: provision, destroy, stop, start
(CR1-06 in docs/review/codereview1.md; re-landed from #17 and #18).

* **Accepted means durable.** A 202 means the operation row and the range's new state
  were committed in one transaction. If that commit fails, nothing was accepted. Whether
  the task reached the broker is a separate, later fact.
* **The operation row is the outbox.** ``status = pending`` until a send succeeds. The
  request sends right after its commit; if the broker is down the operation stays
  pending, visibly delayed (``error.code = broker_unavailable``), and
  ``redispatch_pending`` sends it when the broker is back. Delivery can repeat; the worker
  fences duplicates on the range's state and lease (worker/fencing.py).
* **One sender per operation.** The send locks the operation row (SKIP LOCKED), so two API
  processes never send the same operation.
* **One action at a time per range.** Acceptance locks the range row. A second request
  sees the first's state and is refused by the state machine, or with a 409 while an
  operation is still in flight. A destroy supersedes an in-flight operation instead: it
  is the way out of a range whose power task was lost (the worker's destroy waits for
  the range's lease if that task is in fact still running).
* **Idempotency.** The same ``Idempotency-Key`` for the same action returns the original
  operation without doing anything again; for a different action it is a 409.
* **Requested vs observed.** The operation records what was asked. The range's ``state``
  is what the worker observed, and an operation's outcome is reconciled from it
  (``reconcile``), never assumed. An operation with no outcome long after its send is
  flagged (``no_outcome``) but still blocks: ``abandon`` is the operator's way out.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..models import Exercise, ExerciseState, Range, RangeSnapshot, RangeState
from .models import IN_FLIGHT, RangeOperation

logger = logging.getLogger("truenorth.range_ops")


@dataclass(frozen=True)
class Action:
    task: str
    in_progress: RangeState
    outcomes: dict[RangeState, str]  # observed range state -> operation status


ACTIONS: dict[str, Action] = {
    "provision": Action(
        "provision_range", RangeState.provisioning, {RangeState.ready: "succeeded", RangeState.failed: "failed"}
    ),
    "destroy": Action(
        "destroy_range", RangeState.destroying, {RangeState.destroyed: "succeeded", RangeState.failed: "failed"}
    ),
    "stop": Action("stop_range", RangeState.stopping, {RangeState.stopped: "succeeded", RangeState.failed: "failed"}),
    "start": Action("start_range", RangeState.starting, {RangeState.running: "succeeded", RangeState.failed: "failed"}),
}

BROKER_UNAVAILABLE = {
    "code": "broker_unavailable",
    "message": "The task queue is not reachable; the operation is recorded and will be sent when it is.",
}


class RangeOperationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    range_id: uuid.UUID
    action: str
    generation: int
    status: str
    idempotency_key: str | None = None
    task_id: str | None = None
    dispatch_attempts: int
    error: dict | None = None
    created_at: datetime | None = None
    dispatched_at: datetime | None = None
    finished_at: datetime | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes for timezone columns; they were written as UTC."""
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _request_hash(action: str) -> str:
    return hashlib.sha256(json.dumps({"action": action}, sort_keys=True).encode()).hexdigest()


def _user_uuid(user: CurrentUser) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(user.id))
    except ValueError:
        return None


def _locked_range(db: Session, range_id: uuid.UUID, user: CurrentUser) -> Range:
    """The caller's range, row-locked until commit (PostgreSQL; SQLite serialises writes)."""
    rng = (
        db.query(Range)
        .filter(Range.id == range_id, Range.tenant_id == uuid.UUID(user.tenant_id))
        .with_for_update()
        .populate_existing()
        .first()
    )
    if not rng:
        raise HTTPException(404, "Range not found")
    return rng


def recorded_vms(rng: Range) -> int:
    """How many VMs the range's last build recorded (provisioner_output["vms"])."""
    try:
        out = json.loads(rng.provisioner_output or "{}")
    except ValueError:
        return 0
    vms = out.get("vms") if isinstance(out, dict) else None
    return len(vms) if isinstance(vms, list) else 0


ABANDONED = "abandoned:"  # worker/fencing.py's tombstone prefix
# The longest a live but hung worker can keep its tombstone: it renews it until the soft
# time limit (worker/fencing.py SOFT_TIME_LIMIT, 55 min), then keeps it unrenewed for
# KEPT_LEASE_SECONDS (1 h). Force-release (``force_release_tombstone``) is the way out.
TOMBSTONE_WORST_CASE = "about 1 h 55 min (a hung worker: its 55 min time limit, then 1 h kept)"


def lease_seconds() -> float:
    """worker/fencing.py's LEASE_SECONDS (same variable): how long an abandoned
    operation's tombstone blocks the range unless its worker, still alive, renews it."""
    return float(os.getenv("RANGE_LEASE_SECONDS", "180") or 180)


def _db_later(db: Session, seconds: float):
    """Now plus ``seconds`` in database time (PostgreSQL), as the worker writes lease
    expiry; SQLite (tests) has no interval arithmetic, so there the process clock."""
    if db.get_bind().dialect.name == "postgresql":
        return func.now() + timedelta(seconds=seconds)
    return _now() + timedelta(seconds=seconds)


def worker_acting(db: Session, range_id: uuid.UUID) -> str | None:
    """The holder of the range's unexpired lease (worker/fencing.py), if a worker task is
    acting on it or an abandoned one may still be finishing (``abandoned:...``)."""
    from ..range_leases import RangeLease

    row = (
        db.query(RangeLease.holder)
        .filter(RangeLease.range_id == range_id, RangeLease.expires_at > func.now())  # database time
        .first()
    )
    return row[0] if row else None


def refuse_while_in_flight(db: Session, rng: Range, what: str) -> None:
    """409 while an operation of the range is in flight (outcomes reconciled first). For
    actions that are not operations (a restore): a restore over a stop that has not been
    read yet stranded the stop, whose outcome state the restore then overwrote."""
    reconcile(db, rng)
    busy = (
        db.query(RangeOperation).filter(RangeOperation.range_id == rng.id, RangeOperation.status.in_(IN_FLIGHT)).first()
    )
    if busy:
        raise HTTPException(409, f"Cannot {what}: a {busy.action} of this range is still in progress")


def _check(db: Session, rng: Range, action: str) -> None:
    reconcile(db, rng)
    if not rng.state.can_transition_to(ACTIONS[action].in_progress):
        raise HTTPException(409, f"Cannot {action} range in state {rng.state.value}")
    vms = recorded_vms(rng)
    if action in ("stop", "start"):
        if not vms:
            raise HTTPException(409, f"Cannot {action}: the range has no VMs recorded")
        # Never alongside a snapshot being taken or restored: the snapshot task holds no
        # lease, and a stop over a half-done revert would hide that the revert failed.
        busy = (
            db.query(RangeSnapshot.snapshot_state)
            .filter(RangeSnapshot.range_id == rng.id, RangeSnapshot.snapshot_state.in_(("creating", "restoring")))
            .first()
        )
        if busy:
            doing = "taken" if busy[0] == "creating" else "restored"
            raise HTTPException(409, f"Cannot {action}: a snapshot of this range is being {doing}")
    if action == "stop" and (
        db.query(Exercise.id).filter(Exercise.range_id == rng.id, Exercise.state == ExerciseState.running).first()
    ):
        raise HTTPException(409, "Cannot stop: an exercise is running on this range")
    if action == "provision" and vms:
        raise HTTPException(409, f"The range still has {vms} VMs from an earlier build; destroy it first")
    in_flight = db.query(RangeOperation).filter(RangeOperation.range_id == rng.id, RangeOperation.status.in_(IN_FLIGHT))
    if action != "destroy" and (holder := worker_acting(db, rng.id)):
        # An abandoned or superseded task may still be running: its result would be taken
        # for the new operation's, and the new task skipped (worker/fencing.py).
        if holder.startswith(ABANDONED):
            why = (
                "the abandoned operation's worker may still be finishing on the hypervisor. If it is dead this "
                f"clears within {lease_seconds():.0f} s; if alive, when its work ends, at worst "
                f"{TOMBSTONE_WORST_CASE}; an admin can force-release it"
            )
        else:
            why = (
                f"a dead worker's lease expires within {lease_seconds():.0f} s; abandoning its operation hands "
                "the range back once the worker, if alive, has stopped"
            )
        raise HTTPException(409, f"Cannot {action}: a worker is still acting on this range; try again later ({why}).")
    if action == "destroy":
        for op in in_flight:
            op.status, op.finished_at = "superseded", _now()
            op.error = {"code": "superseded", "message": "A destroy of the range replaced it"}
        return
    busy_op = in_flight.first()
    if busy_op:
        raise HTTPException(409, f"A {busy_op.action} of this range is still in progress (operation {busy_op.id})")


def accept(
    db: Session, range_id: uuid.UUID, user: CurrentUser, action: str, idempotency_key: str | None = None
) -> tuple[RangeOperation, Range, bool]:
    """Record ``action`` on the range and move it to the in-progress state. Does not commit.

    Returns (operation, range, created). ``created`` is False when an Idempotency-Key
    replay returned the original operation; the caller then changes nothing.
    """
    spec = ACTIONS[action]
    rng = _locked_range(db, range_id, user)
    digest = _request_hash(action)
    if idempotency_key:
        existing = (
            db.query(RangeOperation)
            .filter(RangeOperation.range_id == rng.id, RangeOperation.idempotency_key == idempotency_key)
            .first()
        )
        if existing:
            if existing.request_hash != digest:
                raise HTTPException(409, "This Idempotency-Key was already used for a different request")
            return existing, rng, False
    _check(db, rng, action)
    if action == "provision":
        _reserve_for_provision(db, rng)
    generation = (
        db.query(func.max(RangeOperation.generation)).filter(RangeOperation.range_id == rng.id).scalar() or 0
    ) + 1
    op = RangeOperation(
        id=uuid.uuid4(),
        tenant_id=rng.tenant_id,
        range_id=rng.id,
        action=action,
        generation=generation,
        idempotency_key=idempotency_key,
        request_hash=digest,
        requested_by=_user_uuid(user),
        status="pending",
        dispatch_attempts=0,
    )
    db.add(op)
    rng.state = spec.in_progress
    rng.error_message = None
    db.flush()
    return op, rng, True


def _reserve_for_provision(db: Session, rng: Range) -> None:
    """Shared-network addresses the worker will build with, held from acceptance on.

    Today only noise agents' management NICs (app/noise/mgmt.py). In the acceptance
    transaction, so a refused or failed acceptance holds nothing.
    """
    from ..network_inventory import PoolExhaustedError
    from ..noise import mgmt as noise_mgmt

    try:
        noise_mgmt.reserve(db, rng, noise_mgmt.range_template(rng))
    except PoolExhaustedError as exc:
        raise HTTPException(409, f"Cannot provision: {exc}") from exc


def _task_args(db: Session, op: RangeOperation) -> tuple:
    """What the operation's task is sent: the range id, and for a provision the reserved
    noise management addresses when it holds any (``noise_mgmt``, provision_range's
    optional second argument; worker/contracts.py)."""
    if op.action == "provision":
        from ..noise import mgmt as noise_mgmt

        if held := noise_mgmt.reserved(db, op.range_id):
            return (str(op.range_id), held)
    return (str(op.range_id),)


def dispatch(db: Session, op: RangeOperation) -> bool:
    """Send a pending operation's task, after its acceptance has committed. Commits.

    One sender per operation: the operation's row is locked (PostgreSQL; SKIP LOCKED)
    from before the send until the status is written, so another API process sending
    the same operation skips it, and one that comes later finds it no longer pending.
    Returns False when this call did not send it.
    """
    from ..celery_client import dispatch as send

    held = (
        db.query(RangeOperation.id)
        .filter(RangeOperation.id == op.id, RangeOperation.status == "pending")
        .with_for_update(skip_locked=True)
        .first()
    )
    if held is None:  # being sent by someone else, or already sent
        db.commit()
        return False
    task_id = send(ACTIONS[op.action].task, *_task_args(db, op))
    values: dict = {"dispatch_attempts": RangeOperation.dispatch_attempts + 1}
    if task_id:
        values.update(status="dispatched", task_id=task_id, dispatched_at=_now(), error=None)
    else:
        values.update(error=BROKER_UNAVAILABLE)
    db.execute(
        update(RangeOperation)
        .where(RangeOperation.id == op.id, RangeOperation.status == "pending")
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.refresh(op)
    return bool(task_id)


def redispatch_pending(db: Session, *, min_age: timedelta = timedelta(seconds=15), limit: int = 50) -> int:
    """Send operations still pending (the broker was down, or the process died between
    the commit and the send). Returns how many were sent. ``min_age`` leaves a
    just-accepted operation to its own request."""
    pending = (
        db.query(RangeOperation)
        .filter(RangeOperation.status == "pending", RangeOperation.created_at <= _now() - min_age)
        .order_by(RangeOperation.created_at)
        .limit(limit)
        .all()
    )
    db.commit()
    return sum(dispatch(db, op) for op in pending)


def stale_after() -> timedelta:
    return timedelta(seconds=float(os.getenv("RANGE_OP_STALE_AFTER_SECONDS", str(6 * 3600))))


def reconcile(db: Session, rng: Range) -> None:
    """Settle dispatched operations from the range's observed state. Does not commit.

    No outcome long after the send (the worker died, the task was lost) is reported as
    ``error.code = no_outcome``, but the operation stays in flight: a quiet task is not
    proof that nothing is still happening on the hypervisor. ``abandon`` is the explicit,
    human way out.
    """
    for op in db.query(RangeOperation).filter(RangeOperation.range_id == rng.id, RangeOperation.status == "dispatched"):
        outcome = ACTIONS[op.action].outcomes.get(rng.state) if op.action in ACTIONS else None
        if outcome is None:
            sent = _aware(op.dispatched_at)
            if sent and _now() - sent > stale_after() and not op.error:
                op.error = {
                    "code": "no_outcome",
                    "message": f"No result from the worker {stale_after()} after it was sent. The task may have "
                    "been lost; check the hypervisor, then abandon the operation if nothing is running.",
                }
            continue
        op.status, op.finished_at = outcome, _now()
        if op.action == "destroy" and outcome == "succeeded":
            from ..network_inventory import release_range

            release_range(db, rng.id)  # its addresses and VLANs are free for other ranges
        if outcome == "failed":
            op.error = {"code": "range_failed", "message": (rng.error_message or "The worker reported a failure")[:500]}
    # Sessions here do not autoflush: write the outcomes now, so the in-flight check that
    # follows (accept) sees them, not the stale 'dispatched' rows.
    db.flush()


def fence_lease(db: Session, range_id: uuid.UUID, action: str, *, legacy: bool = False) -> bool:
    """Fence out the task holding the range's lease for ``action``: rename the lease to a
    tombstone, ``abandoned:<holder>``, expiring ``lease_seconds()`` from now. Does not
    commit. Returns whether there was such a lease.

    Not deleted: the task may still be running, with vCenter work in flight that a new
    build would collide with (VM names, port groups) or a teardown that would remove a new
    build's port groups. The tombstone keeps the range blocked; the task, if alive, is
    fenced (it starts nothing more and writes nothing, worker/fencing.py) and renews the
    tombstone until its work ends, then deletes it. A dead one renews nothing, and the
    range is free within ``lease_seconds()``.

    Only ``<action>:...`` holders: another action's lease (a restore, a superseded build
    under a destroy) is left alone. ``legacy`` also fences a holder without an action
    (written before holders carried one); the caller passes it only when the abandoned
    operation is the range's only one in flight, so that lease can only be its task's."""
    from sqlalchemy import literal, or_

    from ..range_leases import RangeLease

    mine = RangeLease.holder.like(f"{action}:%")
    if legacy:
        mine = or_(mine, ~RangeLease.holder.like("%:%"))
    renamed = (
        db.query(RangeLease)
        .filter(RangeLease.range_id == range_id, mine)
        .update(
            {
                RangeLease.holder: literal(ABANDONED).concat(RangeLease.holder),
                RangeLease.expires_at: _db_later(db, lease_seconds()),
            },
            synchronize_session=False,
        )
    )
    return bool(renamed)


def abandon(db: Session, rng: Range, op: RangeOperation, user: CurrentUser) -> bool:
    """An operator's decision that an in-flight operation will not finish. Does not commit.

    The range goes to ``failed`` (from where it can be destroyed or provisioned again) and
    the operation records who gave up on it. In the same transaction the lease held by the
    operation's task becomes a short tombstone (``fence_lease``): a dead worker's range
    is free within ``lease_seconds()`` instead of the lease's full life, and a worker that
    is in fact alive is fenced out and keeps the range blocked only until its in-flight
    hypervisor work ends. Not automatic: the API cannot see whether the hypervisor is still
    working. Returns whether a lease was fenced.
    """
    reconcile(db, rng)  # it may have finished since anyone looked
    if op.status not in IN_FLIGHT:
        raise HTTPException(409, f"Operation is already {op.status}")
    only_op = (
        db.query(RangeOperation.id)
        .filter(RangeOperation.range_id == rng.id, RangeOperation.status.in_(IN_FLIGHT), RangeOperation.id != op.id)
        .first()
        is None
    )
    released = fence_lease(db, rng.id, op.action, legacy=only_op)
    op.status, op.finished_at = "failed", _now()
    message = f"Abandoned by {user.email or user.id}"
    if released:
        message += (
            f"; the worker's lease on the range was fenced: the range is blocked for up to {lease_seconds():.0f} s, "
            f"or while that worker is still finishing (at worst {TOMBSTONE_WORST_CASE})"
        )
    op.error = {"code": "abandoned", "message": message}
    if rng.state == ACTIONS[op.action].in_progress:
        rng.state = RangeState.failed
        rng.error_message = f"{op.action} abandoned by an operator; check the hypervisor for the VMs' real state"
    return released


# ── force-releasing an abandoned operation's tombstone ───────────────


FORCE_RELEASE_WARNING = (
    "Force-released an abandoned operation's lease tombstone. If that operation's worker is in fact still "
    "running, its in-flight hypervisor work (clones, port groups, a teardown) continues beside whatever runs on "
    "this range next, and what it builds is no longer discarded: check vCenter's recent tasks for this range "
    "and destroy it before building it again."
)


class ForceReleaseIn(BaseModel):
    """A deliberate act: the caller repeats the range's id and says why."""

    confirm_range_id: uuid.UUID
    reason: str = Field(min_length=10, max_length=500)


class ForceReleaseOut(BaseModel):
    range_id: uuid.UUID
    released_holder: str
    was_expired: bool
    warning: str


def force_release_tombstone(db: Session, rng: Range) -> tuple[str, bool]:
    """Delete the range's lease if it is an abandoned operation's tombstone. Does not
    commit; the caller has row-locked the range (the lock order of accept and abandon).
    Returns (holder, whether it had expired already).

    Only a tombstone: a live lease belongs to a task that is still the range's (abandon
    its operation first; a dead worker's lease expires on its own). The tombstone of a
    hung worker can otherwise block the range for ``TOMBSTONE_WORST_CASE``."""
    from ..range_leases import RangeLease

    lease = (
        db.query(RangeLease).filter(RangeLease.range_id == rng.id).with_for_update().populate_existing().first()
    )
    if lease is None:
        raise HTTPException(404, "This range has no lease to release")
    if not lease.holder.startswith(ABANDONED):
        raise HTTPException(
            409,
            "This lease is held by a task that is still the range's, not an abandoned operation's tombstone. "
            "Abandon its operation first; a dead worker's lease expires on its own.",
        )
    expired = _aware(lease.expires_at) <= _now()
    holder = lease.holder
    db.delete(lease)
    db.flush()
    logger.warning("range %s: tombstone %s force-released", rng.id, holder)
    return holder, expired


# ── the API process's re-send loop ────────────────────────────────────


def redispatch_interval() -> float:
    return float(os.getenv("RANGE_OP_REDISPATCH_SECONDS", "30") or 0)


def _redispatch_once(session_factory) -> None:
    db = session_factory()
    try:
        min_age = timedelta(seconds=float(os.getenv("RANGE_OP_REDISPATCH_MIN_AGE_SECONDS", "15") or 0))
        if sent := redispatch_pending(db, min_age=min_age):
            logger.info("re-sent %d pending range operation(s)", sent)
    finally:
        db.close()


async def redispatch_loop(session_factory, interval: float) -> None:
    """Re-send pending operations every ``interval`` seconds, in every API process (one
    sender per operation, ``dispatch``)."""
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_redispatch_once, session_factory)
        except Exception:  # noqa: BLE001 — never let the loop die; the next pass retries
            logger.exception("range operation re-send pass failed")
