"""Range tasks act only for the operation the API recorded, one execution at a time.

The API records an operation by moving the range into its in-progress state
(``provisioning``, ``destroying``, ``stopping``, ``starting``) in the same transaction as
the operation row (control-plane/api/app/range_ops.py). A task may be delivered twice
(worker loss with late acks, or the API's outbox re-sending) or late, after the range has
moved on. So a task ``claim``s the range: the range must still be in that state, *and* the
task takes the range's lease (table ``range_leases``), which one execution holds at a time.

* State moved on: a late or duplicate copy. ``skipped``; nothing touched.
* State matches, lease held by another execution: that one may be running, or may have
  died with its lease not yet expired. The copy is ``deferred``: re-queued with a
  countdown (``LEASE_RETRY_SECONDS``). When it comes back it either finds the state moved
  on (the holder finished) or the lease expired (the holder died) and takes over. It is
  never dropped while the range is still in progress.

The lease is released when the task ends, however it ends (``release``), so a retry of a
failed attempt can claim again; it still finds the range in progress, because only the
last attempt records ``failed`` (reliable._last_attempt). Time limits (celery_app): the
soft limit ``SOFT_TIME_LIMIT`` raises ``SoftTimeLimitExceeded`` inside the task, which is
final (``FINAL_ERRORS``: no retry; each range task records ``failed``, a restore gives its
snapshot back; cleanup and ``release`` run). Hypervisor calls go through ``run_async``,
which does not wait for their threads after a failure, so that cleanup is not held past
the hard limit ``TASK_TIME_LIMIT``. The hard limit only backs the soft one up (it kills
the process, nothing runs after it) and is below the broker's visibility timeout, so a
running task is not redelivered. ``LEASE_SECONDS`` outlives the hard limit.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid

from celery.exceptions import SoftTimeLimitExceeded

logger = logging.getLogger("worker.fencing")

SOFT_TIME_LIMIT = 3300
TASK_TIME_LIMIT = 3500  # below the broker's visibility_timeout (3600)
LEASE_SECONDS = 3600
LEASE_RETRY_SECONDS = 60
LEASE_HELD = "lease-held"  # claim(): the state matches but another execution holds the lease


class PermanentError(RuntimeError):
    """A failure no retry can fix (a full pool, a range with nothing to power): the task
    records ``failed`` on the first attempt instead of retrying (tasks.ReliableTask)."""


# Failures that end a task for good on the first attempt: no retry, record failed.
FINAL_ERRORS = (PermanentError, SoftTimeLimitExceeded)


def skipped(action: str, range_id: str, expected: str) -> dict:
    """The result of a duplicate or stale delivery: logged, and nothing else."""
    logger.warning("[%s] range %s is no longer %s: duplicate or stale delivery, skipped", action, range_id, expected)
    return {"status": "skipped", "range_id": range_id, "reason": f"range is no longer {expected}"}


def run_async(coro):
    """``asyncio.run`` for a task's hypervisor call, except on the way out after a failure.

    ``asyncio.run`` waits for every thread the call started (``to_thread``: vSphere clones,
    guest installs) before re-raising, so the soft time limit's cleanup could be held past
    the hard limit, which kills the process with nothing recorded. Here a failure leaves
    those threads behind (the executor is shut down without waiting) and propagates at once.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(coro)
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())  # finished: nothing to wait for
        return result
    finally:
        asyncio.set_event_loop(None)
        loop.close()  # after a failure: shuts the executor down without waiting for its threads


def claim(session_factory, range_id: str, state: str) -> str | None:
    """Claim ``range_id`` for this execution. Returns the holder token to ``release`` with;
    ``LEASE_HELD`` when the range is still ``state`` but another execution holds its lease
    (``defer``); None when the range is no longer ``state`` (``skipped``)."""
    from . import db_ops

    holder = uuid.uuid4().hex
    with session_factory() as db:
        leased = db_ops.claim_lease(db, range_id, holder, LEASE_SECONDS)
        in_state = db_ops.update_range_state(db, range_id, state, only_from=(state,))
        if leased and in_state:
            return holder
        if leased:
            db_ops.release_lease(db, range_id, holder)
    return LEASE_HELD if in_state else None


def defer(task, action: str, range_id: str, state: str, *args, **kwargs) -> dict:
    """Re-queue this delivery to try again in ``LEASE_RETRY_SECONDS`` (the lease is held)."""
    logger.warning(
        "[%s] range %s is %s but another execution holds its lease; trying again in %ss",
        action, range_id, state, LEASE_RETRY_SECONDS,
    )
    task.apply_async(args=(range_id, *args), kwargs=kwargs, countdown=LEASE_RETRY_SECONDS)
    return {"status": "deferred", "range_id": range_id, "reason": "another execution holds the range's lease"}


def release(session_factory, range_id: str, holder: str) -> None:
    """Give the range's lease back (the task ended, or will be retried)."""
    from . import db_ops

    try:
        with session_factory() as db:
            db_ops.release_lease(db, range_id, holder)
    except Exception:  # an unreleased lease only delays a takeover until it expires
        logger.warning("could not release the lease on range %s", range_id, exc_info=True)


def fenced(action: str, state: str):
    """Decorate a bound range task ``fn(task, range_id, ...)``: ``claim`` first (``skipped``
    or ``defer`` when it cannot), ``release`` when it ends, however it ends."""

    def wrap(fn):
        @functools.wraps(fn)
        def run(task, range_id: str, *args, **kwargs):
            from .tasks import _db_session  # tasks imports this module

            holder = claim(_db_session, range_id, state)
            if holder is None:
                return skipped(action, range_id, state)
            if holder == LEASE_HELD:
                return defer(task, action, range_id, state, *args, **kwargs)
            try:
                return fn(task, range_id, *args, **kwargs)
            finally:
                release(_db_session, range_id, holder)

        return run

    return wrap
