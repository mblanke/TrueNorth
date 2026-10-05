"""Accept, dispatch and reconcile range operations (provision, destroy).

The contract (codereview1 S3a):

* **Accepted means durable.** A 202 means the operation row and the range's new state
  were committed in one transaction. If that commit fails, nothing was accepted and the
  caller gets an error. Whether the task reached the broker is a separate, later fact.
* **The operation row is the outbox.** ``status = pending`` until a dispatch succeeds.
  The request dispatches right after commit; if the broker is down the operation stays
  pending, visibly delayed (``error.code = broker_unavailable``), and
  ``redispatch_pending`` sends it when the broker is back. Delivery can repeat, so the
  worker must tolerate a duplicate (S3b fences it on the range's state).
* **One action at a time per range.** Acceptance locks the range row. Two concurrent
  requests serialise; the second sees the first's state and is refused by the state
  machine, or, if an earlier operation is still in flight, with a 409.
* **Idempotency.** The same ``Idempotency-Key`` with the same request returns the
  original operation without doing anything again. The same key with a different
  request is a 409.
* **Requested vs observed.** The operation records what was asked. The range's
  ``state`` is what the worker observed; an operation's outcome is reconciled from it
  (``reconcile``), never assumed.
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
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from .auth import CurrentUser
from .models import Range, RangeState
from .models_range_ops import IN_FLIGHT, RangeOperation

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


def request_hash(action: str, body: dict | None = None) -> str:
    return hashlib.sha256(json.dumps({"action": action, "body": body or {}}, sort_keys=True).encode()).hexdigest()


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


def accept(
    db: Session, range_id: uuid.UUID, user: CurrentUser, action: str, idempotency_key: str | None = None
) -> tuple[RangeOperation, Range, bool]:
    """Record ``action`` on the range and move it to the in-progress state. Does not commit.

    Returns (operation, range, created). ``created`` is False when an Idempotency-Key
    replay returned the original operation; the caller then changes nothing.
    """
    spec = ACTIONS[action]
    rng = _locked_range(db, range_id, user)
    digest = request_hash(action)
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
    reconcile(db, rng)
    if not rng.state.can_transition_to(spec.in_progress):
        raise HTTPException(409, f"Cannot {action} range in state {rng.state.value}")
    busy = (
        db.query(RangeOperation).filter(RangeOperation.range_id == rng.id, RangeOperation.status.in_(IN_FLIGHT)).first()
    )
    if busy:
        raise HTTPException(409, f"A {busy.action} of this range is still in progress (operation {busy.id})")
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
    )
    db.add(op)
    rng.state = spec.in_progress
    rng.error_message = None
    return op, rng, True


def _user_uuid(user: CurrentUser) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(user.id))
    except ValueError:
        return None


def dispatch(db: Session, op: RangeOperation) -> bool:
    """Send a pending operation's task, after its acceptance has committed. Commits.

    The status changes only from ``pending`` (a conditional UPDATE), so a concurrent
    redispatch or a superseding request is never overwritten.
    """
    from .celery_client import dispatch as send

    task_id = send(ACTIONS[op.action].task, str(op.range_id))
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
    """Send operations still pending (the broker was down). Returns how many were sent.

    ``min_age`` leaves a just-accepted operation to its own request. On PostgreSQL rows
    another API process is already sending are skipped (SKIP LOCKED).
    """
    pending = (
        db.query(RangeOperation)
        .filter(RangeOperation.status == "pending", RangeOperation.created_at <= _now() - min_age)
        .order_by(RangeOperation.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .all()
    )
    sent = 0
    for op in pending:
        sent += dispatch(db, op)
    db.commit()
    return sent


def reconcile(db: Session, rng: Range) -> None:
    """Settle dispatched operations from the range's observed state. Does not commit."""
    for op in db.query(RangeOperation).filter(RangeOperation.range_id == rng.id, RangeOperation.status == "dispatched"):
        outcome = ACTIONS[op.action].outcomes.get(rng.state) if op.action in ACTIONS else None
        if outcome is None:
            continue
        op.status = outcome
        op.finished_at = _now()
        if outcome == "failed":
            op.error = {"code": "range_failed", "message": (rng.error_message or "The worker reported a failure")[:500]}
    # Production sessions do not autoflush: write the outcomes now, so the in-flight
    # check that follows (accept) sees them, not the stale 'dispatched' rows.
    db.flush()


async def redispatch_loop(session_factory, interval: float) -> None:
    """Background loop in the API process: re-send pending operations every ``interval``."""
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_redispatch_once, session_factory)
        except Exception:  # never let the loop die; the next pass retries
            logger.exception("range operation redispatch pass failed")


def _redispatch_once(session_factory) -> None:
    db = session_factory()
    try:
        sent = redispatch_pending(db)
        if sent:
            logger.info("redispatched %d pending range operation(s)", sent)
    finally:
        db.close()


def redispatch_interval() -> float:
    return float(os.getenv("RANGE_OP_REDISPATCH_SECONDS", "30") or 0)
