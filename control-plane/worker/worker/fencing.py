"""Range tasks act only for the operation the API recorded, one execution at a time.

The API records an operation by moving the range into its in-progress state
(``provisioning``, ``destroying``, ``stopping``, ``starting``) in the same transaction as
the operation row (control-plane/api/app/range_ops.py). A task may be delivered twice
(worker loss with late acks, or the API's outbox re-sending) or late, after the range has
moved on. So a task ``claim``s the range: the range must still be in that state, *and* the
task takes the range's lease (table ``range_leases``), which only one execution holds at a
time. A late copy finds the state moved on; a copy arriving while another runs finds the
lease held. Either does nothing and touches no hypervisor. Outcomes are written only from
the in-progress state.

The lease is released when the task ends (``release``), so a retry of a failed attempt can
claim again; it still finds the range in progress, because only the last attempt records
``failed`` (reliable._last_attempt). A lease left by a worker that died expires after
``LEASE_SECONDS``, longer than any task may run (``TASK_TIME_LIMIT``, which is shorter
than the broker's visibility timeout, so a running task is never redelivered).
"""

from __future__ import annotations

import functools
import logging
import uuid

logger = logging.getLogger("worker.fencing")

# A task is stopped after TASK_TIME_LIMIT (celery_app), before the broker would redeliver
# it (visibility_timeout 3600); vSphere's own provision budget is below it (3300).
TASK_TIME_LIMIT = 3500
LEASE_SECONDS = 3600


class PermanentError(RuntimeError):
    """A failure no retry can fix (a full pool, a range with nothing to power): the task
    records ``failed`` on the first attempt instead of retrying (tasks.ReliableTask)."""


def skipped(action: str, range_id: str, expected: str) -> dict:
    """The result of a duplicate or stale delivery: logged, and nothing else."""
    logger.warning("[%s] range %s is no longer %s: duplicate or stale delivery, skipped", action, range_id, expected)
    return {"status": "skipped", "range_id": range_id, "reason": f"range is no longer {expected}"}


def claim(session_factory, range_id: str, state: str) -> str | None:
    """Claim ``range_id`` for this execution if it is still ``state`` and nobody else holds
    its lease. Returns the holder token to ``release`` with, or None (do nothing)."""
    from . import db_ops

    holder = uuid.uuid4().hex
    with session_factory() as db:
        if not db_ops.claim_lease(db, range_id, holder, LEASE_SECONDS):
            return None
        if not db_ops.update_range_state(db, range_id, state, only_from=(state,)):
            db_ops.release_lease(db, range_id, holder)
            return None
    return holder


def release(session_factory, range_id: str, holder: str) -> None:
    """Give the range's lease back (the task ended, or will be retried)."""
    from . import db_ops

    try:
        with session_factory() as db:
            db_ops.release_lease(db, range_id, holder)
    except Exception:  # an unreleased lease only delays a takeover until it expires
        logger.warning("could not release the lease on range %s", range_id, exc_info=True)


def fenced(action: str, state: str):
    """Decorate a bound range task ``fn(task, range_id, ...)``: ``claim`` first (or return
    ``skipped``), ``release`` when it ends, however it ends."""

    def wrap(fn):
        @functools.wraps(fn)
        def run(task, range_id: str, *args, **kwargs):
            from .tasks import _db_session  # tasks imports this module

            holder = claim(_db_session, range_id, state)
            if holder is None:
                return skipped(action, range_id, state)
            try:
                return fn(task, range_id, *args, **kwargs)
            finally:
                release(_db_session, range_id, holder)

        return run

    return wrap
