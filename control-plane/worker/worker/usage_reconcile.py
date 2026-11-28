# Author: Marc-Andre Blanke
# Created: 2026-11-28T03:36:45Z
"""The usage ledger's safety net (ADR 0021). Beat runs it hourly (celery_app.py).

Every change of a range's state appends a ``range_state_events`` row where it is made (the
worker's ``update_range_state``, the API's flush hook). A change made some other way (a
Core UPDATE, a hand edit, a soft delete) leaves the range's last event saying something
else. This compares each range with its last event and, where they differ, appends the
missing change with ``source`` reconcile, stamped when it was noticed: the report then
knows that part of the history is approximate.

A range never seen before (one older than the ledger) gets its first event here, unless it
holds nothing (created, destroyed): its earlier history is derived from its timestamps.
A soft-deleted range counts as destroyed.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa

from . import db_ops
from .base_tasks import ReliableTask
from .celery_app import app
from .tables import range_state_events, ranges
from .task_plumbing import db_session as _db_session

logger = logging.getLogger(__name__)

SOURCE = "reconcile"
_HOLDS_NOTHING = ("created", "destroyed")


def _effective(state: str | None, deleted_at) -> str | None:
    return "destroyed" if deleted_at is not None else state


def _last_states(db) -> dict[str, str]:
    """Each range's last recorded state: the latest event's ``to_state``."""
    ranked = sa.select(
        range_state_events.c.range_id,
        range_state_events.c.to_state,
        sa.func.row_number()
        .over(partition_by=range_state_events.c.range_id, order_by=(range_state_events.c.at.desc(),))
        .label("n"),
    ).subquery()
    rows = db.execute(sa.select(ranked.c.range_id, ranked.c.to_state).where(ranked.c.n == 1)).fetchall()
    return {str(r[0]): r[1] for r in rows}


def run_reconcile(db) -> dict:
    """Append a reconcile event for every range whose state its ledger does not show."""
    last = _last_states(db)
    rows = db.execute(
        sa.select(ranges.c.id, ranges.c.tenant_id, sa.cast(ranges.c.state, sa.Text), ranges.c.deleted_at)
    ).fetchall()
    appended = 0
    for rid, tenant, state, deleted_at in rows:
        now_state = _effective(state, deleted_at)
        before = last.get(str(rid))
        if now_state is None or before == now_state or (before is None and now_state in _HOLDS_NOTHING):
            continue
        # Lock the range and look again: a change made since the read above wrote its own event.
        row = db.execute(
            sa.select(sa.cast(ranges.c.state, sa.Text), ranges.c.deleted_at).where(ranges.c.id == rid).with_for_update()
        ).first()
        if row is None or _effective(row[0], row[1]) != now_state:
            continue
        db_ops.record_state_event(db, rid, tenant, before, now_state, source=SOURCE)
        appended += 1
    if appended:
        logger.info("usage: %d range state change(s) were missing from the ledger; recorded now", appended)
    return {"checked": len(rows), "appended": appended}


@app.task(name="worker.usage_reconcile.reconcile_usage_ledger", base=ReliableTask, bind=True)
def reconcile_usage_ledger(self) -> dict:
    with _db_session() as db:
        return run_reconcile(db)
