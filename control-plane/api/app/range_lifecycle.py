"""Starting a range build or teardown: the one path the ranges router and the scheduler share.

Both move the range's state first (guarded by ``RangeState.can_transition_to``), commit
so the worker reads the new state, then dispatch the contracted worker task. The
scheduler uses the ``*_for_booking`` functions and never touches the ``ranges`` table
itself (ADR 0004 §5).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from .celery_client import dispatch
from .models import Range, RangeState

logger = logging.getLogger("truenorth.api.range_lifecycle")

# A range in these states is already built: a booking uses it as it is.
UP_STATES = frozenset({RangeState.ready, RangeState.running, RangeState.stopped, RangeState.provisioning})


class RangeTransitionError(Exception):
    """The range's state does not allow the requested move."""


def begin_provision(db: Session, rng: Range) -> str | None:
    """Mark the range provisioning, commit, and dispatch the build. Returns the task id
    (None when the broker is down; the range stays provisioning, as before)."""
    if not rng.state.can_transition_to(RangeState.provisioning):
        raise RangeTransitionError(f"Cannot provision range in state {rng.state.value}")
    rng.state = RangeState.provisioning
    db.commit()
    return dispatch("provision_range", str(rng.id))


def begin_destroy(db: Session, rng: Range) -> str | None:
    """Mark the range destroying, commit, and dispatch the teardown."""
    if not rng.state.can_transition_to(RangeState.destroying):
        raise RangeTransitionError(f"Cannot destroy range in state {rng.state.value}")
    rng.state = RangeState.destroying
    db.commit()
    return dispatch("destroy_range", str(rng.id))


# -- For the scheduler ----------------------------------------------------------
def provision_for_booking(db: Session, range_id: uuid.UUID) -> tuple[bool, str]:
    """Bring a booked range up. Returns (built_by_us, what happened).

    A range that is already up is used as it is and reported as not built by us, so the
    scheduler will not tear it down afterwards.
    """
    rng = db.get(Range, range_id)
    if rng is None or rng.deleted_at is not None:
        return False, "range no longer exists"
    if rng.state in UP_STATES:
        return False, f"range already {rng.state.value}"
    try:
        task = begin_provision(db, rng)
    except RangeTransitionError as exc:
        return False, str(exc)
    return True, "provisioning dispatched" if task else "provisioning marked; broker unavailable"


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
    return True, "teardown dispatched" if task else "teardown marked; broker unavailable"
