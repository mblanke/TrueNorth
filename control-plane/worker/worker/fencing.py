"""Range tasks act only for the request that was recorded, one execution at a time
(CR1-06 and CR1-11 in docs/review/codereview1.md).

The API, or a lab session, records a request by moving the range into its in-progress
state (``provisioning``, ``destroying``) and only then sends the task. A task may be
delivered twice (a worker lost with late acks, a sender that re-sends) or late, after the
range has moved on. So a task ``claim``s the range: it takes the range's lease (table
``range_leases``), which one execution holds at a time, and the range must still be in
that state.

* State moved on: a late or duplicate copy. ``skipped``; nothing touched.
* State matches, lease held by another execution: that one may be running, or may have
  died with its lease not yet expired. The copy is ``deferred``: re-queued with a
  countdown (``LEASE_RETRY_SECONDS``). When it comes back it finds the state moved on
  (the holder finished) or the lease expired (the holder died) and takes over. It is
  never dropped while the range is still in progress.

The lease is released when the task ends, however it ends, so a retry of a failed attempt
can claim again; it still finds the range in progress, because only the last attempt
records ``failed`` (``last_attempt``). Time limits (celery_app): the soft limit
``SOFT_TIME_LIMIT`` raises ``SoftTimeLimitExceeded`` inside the task. That is final
(``FINAL_ERRORS``: no retry, the task records ``failed``), and the lease is kept
(``keep``), not released, because the hypervisor call may still be running in a thread.
Hypervisor calls go through ``run_async``, which does not wait for those threads after a
failure, so the task's cleanup is not held past the hard limit ``TASK_TIME_LIMIT``. The
hard limit only backs the soft one up (it kills the process) and is below the broker's
visibility timeout, so a running task is not redelivered. ``LEASE_SECONDS`` outlives it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from celery.exceptions import SoftTimeLimitExceeded

# The API owns the table (app/range_leases, migration d2e3f4a5b6c7); tables.py mirrors it.
from .tables import range_leases

logger = logging.getLogger("worker.fencing")

SOFT_TIME_LIMIT = 3300
TASK_TIME_LIMIT = 3500  # below the broker's visibility_timeout (3600)
LEASE_SECONDS = 3600
LEASE_RETRY_SECONDS = 60
CLEANUP_GRACE = 30  # seconds a cancelled hypervisor call gets to close its sessions
LEASE_HELD = "lease-held"  # claim(): the state matches but another execution holds the lease

# Failures that end a task for good on the first attempt: no retry, record failed.
FINAL_ERRORS = (SoftTimeLimitExceeded,)


def last_attempt(task) -> bool:
    """True when a failure now will not be retried, or the task was called directly."""
    return bool(task.request.called_directly) or task.request.retries >= (task.max_retries or 0)


def skipped(action: str, range_id: str, expected: str | None) -> dict:
    """The result of a duplicate or stale delivery: logged, and nothing else."""
    logger.warning("[%s] range %s is no longer %s: duplicate or stale delivery, skipped", action, range_id, expected)
    return {"status": "skipped", "range_id": range_id}


def run_async(coro):
    """``asyncio.run`` for a task's hypervisor call, except on the way out after a failure.

    ``asyncio.run`` waits for every thread the call started (``to_thread``: vSphere clones,
    guest installs) before re-raising, so the soft time limit's cleanup could be held past
    the hard limit, which kills the process with nothing recorded. Here a failure cancels
    the call and gives it ``CLEANUP_GRACE`` seconds to run its own cleanup (``finally``,
    ``async with``: vCenter sessions, HTTP clients), then propagates without waiting for
    those threads. A thread may keep running after that; the range's lease covers it.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(coro)
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())  # finished: nothing to wait for
        return result
    except BaseException:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.wait(pending, timeout=CLEANUP_GRACE))
        raise
    finally:
        asyncio.set_event_loop(None)
        loop.close()  # shuts the executor down without waiting for its threads


# ── the lease ─────────────────────────────────────────────────────────


def _claim_lease(db, range_id: str, holder: str) -> bool:
    """Take the range's lease for ``holder`` unless another unexpired holder has it."""
    now = datetime.now(UTC)
    values = {"range_id": range_id, "holder": holder, "expires_at": now + timedelta(seconds=LEASE_SECONDS)}
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    stmt = insert(range_leases).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[range_leases.c.range_id],
        set_={"holder": holder, "expires_at": values["expires_at"]},
        where=range_leases.c.expires_at < now,
    )
    # RETURNING, not rowcount: psycopg reports -1 for an upsert.
    return db.execute(stmt.returning(range_leases.c.holder)).first() is not None


def _release_lease(db, range_id: str, holder: str) -> None:
    db.execute(sa.delete(range_leases).where(range_leases.c.range_id == range_id, range_leases.c.holder == holder))


def _extend_lease(db, range_id: str, holder: str) -> None:
    db.execute(
        sa.update(range_leases)
        .where(range_leases.c.range_id == range_id, range_leases.c.holder == holder)
        .values(expires_at=datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS))
    )


def claim(session_factory, in_state, range_id: str, state: str | None) -> str | None:
    """Claim ``range_id`` for this execution. Returns the holder token to ``release`` with;
    ``LEASE_HELD`` when the range is still ``state`` but another execution holds its lease
    (``defer``); None when the range is no longer ``state`` (``skipped``).

    ``in_state(range_id, state)`` says whether the range is in ``state``; a ``state`` of
    None means the lease alone (a restore, which has no in-progress range state)."""
    holder = uuid.uuid4().hex
    try:
        with session_factory() as db:
            leased = _claim_lease(db, range_id, holder)
    except sa.exc.IntegrityError:  # the range row is gone (deleted): a late copy
        return None
    try:
        matches = state is None or bool(in_state(range_id, state))
    except BaseException:  # never leave the lease behind on a failed check
        if leased:
            release(session_factory, range_id, holder)
        raise
    if leased and matches:
        return holder
    if leased:
        release(session_factory, range_id, holder)
    return LEASE_HELD if matches else None


def defer(task, action: str, range_id: str, *args, **kwargs) -> dict:
    """Re-queue this delivery to try again in ``LEASE_RETRY_SECONDS`` (the lease is held)."""
    logger.warning(
        "[%s] another execution holds the lease on range %s; trying again in %ss", action, range_id, LEASE_RETRY_SECONDS
    )
    task.apply_async(args=(range_id, *args), kwargs=kwargs, countdown=LEASE_RETRY_SECONDS)
    return {"status": "deferred", "range_id": range_id}


def keep(session_factory, range_id: str, holder: str) -> None:
    """After a soft time limit: the hypervisor call may still be running in a thread, so
    hold the lease for another ``LEASE_SECONDS`` instead of releasing it."""
    try:
        with session_factory() as db:
            _extend_lease(db, range_id, holder)
    except Exception:  # noqa: BLE001 — the lease still expires on its own
        logger.warning("could not extend the lease on range %s", range_id, exc_info=True)


def release(session_factory, range_id: str, holder: str) -> None:
    """Give the range's lease back (the task ended, or will be retried)."""
    try:
        with session_factory() as db:
            _release_lease(db, range_id, holder)
    except Exception:  # noqa: BLE001 — an unreleased lease only delays a takeover until it expires
        logger.warning("could not release the lease on range %s", range_id, exc_info=True)


def fenced(action: str, state: str | None):
    """Decorate a bound range task ``fn(task, range_id, ...)``: ``claim`` first (``skipped``
    or ``defer`` when it cannot); when it ends, ``release`` the lease, except after a soft
    time limit, when the call may still run in a thread and the lease is ``keep``-ed."""

    def wrap(fn):
        @functools.wraps(fn)
        def run(task, range_id: str, *args, **kwargs):
            from .tasks import _db_session, _in_state  # tasks imports this module

            holder = claim(_db_session, _in_state, range_id, state)
            if holder is None:
                return skipped(action, range_id, state)
            if holder == LEASE_HELD:
                return defer(task, action, range_id, *args, **kwargs)
            try:
                result = fn(task, range_id, *args, **kwargs)
            except SoftTimeLimitExceeded:
                keep(_db_session, range_id, holder)
                raise
            except BaseException:
                release(_db_session, range_id, holder)
                raise
            release(_db_session, range_id, holder)
            return result

        return run

    return wrap
