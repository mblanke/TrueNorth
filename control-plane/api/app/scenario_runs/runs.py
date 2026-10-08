"""Issue the run id and lease a scenario run task is dispatched with (``ExerciseRun``).

``begin`` is for start and replay (a new run from event 0); ``resume`` keeps the run and
hands it to a new task that continues after the last event the previous one claimed.
Neither commits: the caller commits with the exercise's state change, then dispatches.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from .models import ExerciseRun, InjectRecord


def _token() -> str:
    return str(uuid.uuid4())


def begin(db: Session, exercise_id: uuid.UUID) -> str:
    """A new run of the exercise's timeline from its first event; returns the lease."""
    lease = _token()
    row = db.get(ExerciseRun, exercise_id)
    if row is None:
        db.add(ExerciseRun(exercise_id=exercise_id, run_id=_token(), lease=lease, next_seq=0, resume_from=0))
    else:
        row.run_id, row.lease, row.next_seq, row.resume_from = _token(), lease, 0, 0
    db.flush()
    return lease


def resume(db: Session, exercise_id: uuid.UUID, started_at: datetime | None) -> str:
    """Hand the exercise's run to a new task; returns its lease.

    One statement swaps the lease and sets ``resume_from`` to the claim cursor, so the
    earlier task can claim nothing after it and the new one starts after everything it
    claimed. An exercise started before ``exercise_runs`` existed has no row: its run is the
    one that recorded timeline events since it started (its Celery task id), and the new
    task skips what that run recorded.
    """
    lease = _token()
    swapped = db.execute(
        update(ExerciseRun)
        .where(ExerciseRun.exercise_id == exercise_id)
        .values(lease=lease, resume_from=ExerciseRun.next_seq)
        .execution_options(synchronize_session=False)
    ).rowcount
    if not swapped:
        q = db.query(InjectRecord.run_id).filter(
            InjectRecord.exercise_id == exercise_id,
            InjectRecord.source == "timeline",
            InjectRecord.run_id.isnot(None),
        )
        if started_at is not None:
            q = q.filter(InjectRecord.created_at >= started_at)
        last = q.order_by(InjectRecord.created_at.desc()).first()
        run_id = last[0] if last else _token()
        db.add(ExerciseRun(exercise_id=exercise_id, run_id=run_id, lease=lease, next_seq=0, resume_from=0))
    db.flush()
    return lease
