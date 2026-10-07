"""Starting a range build or teardown on the scheduler's behalf (ADR 0004 §5).

The ranges router and the scheduler share one path for this: ``app/range_ops`` (CR1-06).
An operation row and the range's new state commit together, then the task is sent; a
broker that is down leaves the operation pending and the API's re-send loop sends it
later. The same guards apply as for a person: the state machine, one operation in
flight per range, no rebuild over recorded VMs, no start while a worker still holds the
range's lease. A lab session's range is driven only by its session and is refused here,
as the ranges router refuses it. The scheduler uses the ``*_for_booking`` functions and
never touches the ``ranges`` table itself.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .auth import CurrentUser
from .models import Range, RangeState, UserRole

logger = logging.getLogger("truenorth.api.range_lifecycle")

# A range in these states is already built: a booking uses it as it is.
UP_STATES = frozenset({RangeState.ready, RangeState.running, RangeState.stopped, RangeState.provisioning})


class RangeTransitionError(Exception):
    """The range's state (or an operation in flight) does not allow the requested move."""


def _scheduler_identity(rng: Range) -> CurrentUser:
    """Who the operation is recorded as requested by: the scheduler, in the range's own
    tenant (``range_ops`` scopes its row lock to the requester's tenant)."""
    return CurrentUser(
        id="scheduler",
        email="scheduler@truenorth.local",
        display_name="Scheduler",
        role=UserRole.admin,
        tenant_id=str(rng.tenant_id),
        keycloak_id="scheduler",
    )


def is_lab_range(db: Session, range_id: uuid.UUID) -> bool:
    from .lab_sessions.service import lab_range_ids

    return range_id in lab_range_ids(db, [range_id])


def _begin(db: Session, rng: Range, action: str, before_commit: Callable[[], None] | None = None) -> str | None:
    """Accept ``action`` through range_ops, run ``before_commit`` in the same transaction,
    commit, then send. Returns the task id (None when the broker is down: the operation
    stays pending and is re-sent)."""
    from .range_ops import service as ops

    if is_lab_range(db, rng.id):
        raise RangeTransitionError("This range belongs to a student's lab session; manage it from the lab session")
    try:
        op, _, created = ops.accept(db, rng.id, _scheduler_identity(rng), action)
    except HTTPException as exc:  # refused before anything was written
        raise RangeTransitionError(str(exc.detail)) from None
    if before_commit:
        before_commit()
    db.commit()
    if created:
        ops.dispatch(db, op)
    return op.task_id


def begin_provision(db: Session, rng: Range, before_commit: Callable[[], None] | None = None) -> str | None:
    """Accept and send a build of the range (``provisioning`` until the worker reports)."""
    return _begin(db, rng, "provision", before_commit)


def begin_destroy(db: Session, rng: Range) -> str | None:
    """Accept and send a teardown of the range. Supersedes an operation in flight."""
    return _begin(db, rng, "destroy")


# -- For the scheduler ----------------------------------------------------------
def create_for_booking(db: Session, *, tenant_id: uuid.UUID, template_id: uuid.UUID, name: str) -> Range:
    """A new range from a booking's template, in the booking's tenant, ready to build.
    As POST /ranges does. Does not commit."""
    rng = Range(
        name=name[:255],
        template_id=template_id,
        tenant_id=tenant_id,
        state=RangeState.created,
        provisioner_backend=os.getenv("PROVISIONER_BACKEND", "mock"),
    )
    db.add(rng)
    db.flush()
    return rng


def provision_for_booking(
    db: Session, range_id: uuid.UUID, before_build: Callable[[], None] | None = None
) -> tuple[bool, str]:
    """Bring a booked range up. Returns (built_by_us, what happened).

    A range that is already up is used as it is and reported as not built by us, so the
    scheduler will not tear it down afterwards. ``before_build`` runs after the build is
    accepted and before it is committed, so the caller's record of owning it commits in
    the same transaction: a crash after the commit cannot leave a built range nobody
    owns, and a refused build records no ownership.
    """
    rng = db.get(Range, range_id)
    if rng is None or rng.deleted_at is not None:
        return False, "range no longer exists"
    if rng.state in UP_STATES:
        return False, f"range already {rng.state.value}"
    if not rng.state.can_transition_to(RangeState.provisioning):
        return False, f"Cannot provision range in state {rng.state.value}"
    try:
        task = begin_provision(db, rng, before_commit=before_build)
    except RangeTransitionError as exc:
        return False, str(exc)
    return True, "provisioning dispatched" if task else "provisioning accepted; broker unavailable, will be re-sent"


def destroy_for_booking(db: Session, range_id: uuid.UUID) -> tuple[bool, str]:
    """Take down a range a booking built. Returns (settled, what happened).

    Settled means there is nothing left to tear down: dispatched now, already going or
    gone. A range still being built cannot be destroyed yet; the caller retries later.
    """
    rng = db.get(Range, range_id)
    if rng is None or rng.deleted_at is not None:
        return True, "range no longer exists"
    if rng.state in (RangeState.destroying, RangeState.destroyed):
        return True, f"range already {rng.state.value}"
    try:
        task = begin_destroy(db, rng)
    except RangeTransitionError as exc:
        return False, f"{exc}; will retry"
    return True, "teardown dispatched" if task else "teardown accepted; broker unavailable, will be re-sent"
