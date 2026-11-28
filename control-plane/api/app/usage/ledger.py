# Author: Marc-Andre Blanke
# Created: 2026-11-28T03:36:45Z
"""The range state-event ledger (ADR 0021): what a range held, from when to when.

Every change of ``ranges.state`` appends a ``RangeStateEvent`` in the same transaction:

- the worker, in ``worker/db_ops.py`` ``update_range_state`` (every worker write, fenced
  or not, goes through it);
- the API, here: a ``before_flush`` hook sees every ``Range`` whose ``state`` the ORM is
  about to write, wherever in the API that was set. A Core ``UPDATE`` of ``ranges.state``
  is invisible to it; the one that exists (``range_lifecycle.discard_unbuilt``,
  created -> destroyed) moves between two states that hold nothing, and the hourly
  reconciler catches any other.

What a state costs (``charge``): COMPUTE pays vCPU, RAM, VMs and disk; DISK pays disk only;
NOTHING pays nothing. ``failed`` keeps what was charged before it (a stop or start that
failed leaves the VMs as they were), except after a build, which leaves nothing built.
"""

from __future__ import annotations

import uuid

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from ..models import Range, RangeState
from .models import RangeStateEvent

COMPUTE, DISK, NOTHING = "compute", "disk", "nothing"

_COMPUTE = {"provisioning", "ready", "running", "stopping", "starting", "destroying"}
_DISK = {"stopped"}

SOURCE_API, SOURCE_WORKER, SOURCE_RECONCILE = "api", "worker", "reconcile"


def _value(state) -> str | None:
    if state is None:
        return None
    return state.value if isinstance(state, RangeState) else str(state)


def charge(state: str | None, before_state: str | None = None, before_charge: str = NOTHING) -> str:
    """What a range in ``state`` pays for. For ``failed``: what it paid before
    (``before_charge``), unless it failed out of a build, which leaves nothing built."""
    if state in _COMPUTE:
        return COMPUTE
    if state in _DISK:
        return DISK
    if state == "failed" and before_state not in (None, "created", "provisioning"):
        return before_charge
    return NOTHING


@event.listens_for(Session, "before_flush")
def _record(session: Session, _ctx, _instances) -> None:
    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, Range):
            continue
        hist = inspect(obj).attrs.state.history
        if not hist.added:
            continue
        new = _value(hist.added[0])
        old = _value(hist.deleted[0]) if hist.deleted else None
        if new is None or new == old or (old is None and new == "created"):
            continue
        if obj.id is None:  # a new range: give it its id now, so the event can name it
            obj.id = uuid.uuid4()
        session.add(
            RangeStateEvent(tenant_id=obj.tenant_id, range_id=obj.id, from_state=old, to_state=new, source=SOURCE_API)
        )
