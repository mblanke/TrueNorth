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
visibility timeout, so a running task is not redelivered.

**Heartbeat.** The lease is short (``LEASE_SECONDS``, minutes) and a thread renews it
every ``LEASE_HEARTBEAT_SECONDS`` while the task runs, however long it runs. A worker
that dies (killed, OOM, host lost) stops renewing, and its lease expires within
``LEASE_SECONDS``; it used to be held for an hour, refusing every new provision. A kept
lease (soft limit) is set once to ``KEPT_LEASE_SECONDS`` and not renewed.

**Losing the lease.** An operator's abandon of the range operation deletes the lease of
that operation's action (app/range_ops/service.py: holders are ``<action>:<token>``), and
a lease the heartbeat could not renew for ``LEASE_SECONDS`` may be taken by another
execution. Either way the execution no longer holds it, and from then on it does nothing:

* the heartbeat that finds the lease gone cancels the task's hypervisor call (``run_async``);
* ``run_async`` re-checks the lease in the database before each hypervisor call;
* every range state write of a fenced task (``guarded_range_update``) is conditional, in
  the same UPDATE, on the lease still being this execution's and unexpired.

Each raises ``LeaseLost``, a ``BaseException`` so that the tasks' ``except Exception``
failure paths (write ``failed``, retry) do not run; ``fenced`` returns
``{"status": "lease_lost"}``. Work already done on the hypervisor is not undone: it is
logged, and the abandon told the operator to check the hypervisor.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import logging
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from celery.exceptions import SoftTimeLimitExceeded

from . import db_ops
from .range_alloc import AllocationError

# The API owns the table (app/range_leases, migration d2e3f4a5b6c7); tables.py mirrors it.
from .tables import range_leases

logger = logging.getLogger("worker.fencing")

SOFT_TIME_LIMIT = 3300
TASK_TIME_LIMIT = 3500  # below the broker's visibility_timeout (3600)
# How long a lease lasts without renewal: how long a dead worker can block its range.
LEASE_SECONDS = int(os.getenv("RANGE_LEASE_SECONDS", "180"))
# How often a running task renews it: several renewals per lease, so a database blip
# shorter than LEASE_SECONDS does not cost a live task its lease.
LEASE_HEARTBEAT_SECONDS = float(os.getenv("RANGE_LEASE_HEARTBEAT_SECONDS", str(max(LEASE_SECONDS // 6, 1))))
# After a soft time limit (``keep``): the hypervisor call may still run in a thread, with
# no heartbeat. Longer than the hard limit's margin and any single hypervisor call.
KEPT_LEASE_SECONDS = 3600
LEASE_RETRY_SECONDS = 60
CLEANUP_GRACE = 30  # seconds a cancelled hypervisor call gets to close its sessions
LEASE_HELD = "lease-held"  # claim(): the state matches but another execution holds the lease

# Failures that end a task for good on the first attempt: no retry, record failed.
FINAL_ERRORS = (SoftTimeLimitExceeded, AllocationError)  # a full VLAN/address pool: retrying won't help


class LeaseLost(BaseException):  # noqa: N818 — a fence, not an error the task handles
    """This execution no longer holds its range's lease (abandoned, or expired and taken).

    A ``BaseException``: the range tasks' ``except Exception`` paths (record ``failed``,
    retry) must not run for an execution that is no longer the range's. ``fenced`` turns
    it into a ``lease_lost`` result."""


# The lease of the fenced task running in this context (set by ``fenced``).
_current: contextvars.ContextVar[_Lease | None] = contextvars.ContextVar("range_lease", default=None)


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

    In a fenced task the lease is checked in the database first (``LeaseLost``: the call
    is not made), and a heartbeat that finds the lease gone cancels the call the same way.
    """
    lease = _current.get()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    if lease is not None:
        lease.loop = loop  # before the check: a loss found after it cancels the call
    try:
        if lease is not None:
            try:
                lease.verify()
            except BaseException:
                coro.close()  # never started
                raise
        result = loop.run_until_complete(coro)
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())  # finished: nothing to wait for
        return result
    except BaseException as exc:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.wait(pending, timeout=CLEANUP_GRACE))
        if lease is not None and lease.lost.is_set() and not isinstance(exc, LeaseLost):
            raise LeaseLost(f"range {lease.range_id}: lease lost during the hypervisor call") from exc
        raise
    finally:
        if lease is not None:
            lease.loop = None
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


def _extend_lease(db, range_id: str, holder: str, seconds: int | None = None) -> bool:
    """Push the lease's expiry ``seconds`` (``LEASE_SECONDS``) from now, if ``holder``
    still has it unexpired. False when it does not: released (abandoned), or expired and
    possibly taken. An expired lease is not revived: in the meantime the API may have
    taken the range as free (app/range_ops/service.py, worker_acting)."""
    now = datetime.now(UTC)
    return (
        db.execute(
            sa.update(range_leases)
            .where(
                range_leases.c.range_id == range_id,
                range_leases.c.holder == holder,
                range_leases.c.expires_at > now,
            )
            .values(expires_at=now + timedelta(seconds=seconds or LEASE_SECONDS))
        ).rowcount
        > 0
    )


def _holds(db, range_id: str, holder: str) -> bool:
    """Whether ``holder`` has the range's lease, unexpired."""
    stmt = sa.select(range_leases.c.range_id).where(
        range_leases.c.range_id == range_id,
        range_leases.c.holder == holder,
        range_leases.c.expires_at > datetime.now(UTC),
    )
    return db.execute(stmt).first() is not None


class _Lease:
    """The lease a running fenced task holds, and the heartbeat that renews it.

    The heartbeat is a daemon thread: it dies with the process, so a dead worker's lease
    expires ``LEASE_SECONDS`` after its last renewal. It stops on the first renewal that
    finds the lease no longer this execution's (``lose``)."""

    def __init__(self, session_factory, range_id: str, holder: str):
        self.session_factory, self.range_id, self.holder = session_factory, range_id, holder
        self.lost = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None  # the hypervisor call's (run_async)
        self._stopped = threading.Event()
        self._lock = threading.Lock()  # no renewal after stop(): keep() must have the last word
        self._thread = threading.Thread(target=self._beat, name=f"lease-{range_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stopped.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def _beat(self) -> None:
        while not self._stopped.wait(LEASE_HEARTBEAT_SECONDS):
            with self._lock:
                if self._stopped.is_set():
                    return
                try:
                    with self.session_factory() as db:
                        ours = _extend_lease(db, self.range_id, self.holder)
                except Exception:  # noqa: BLE001 — a database blip: the lease has LEASE_SECONDS to spare
                    logger.warning("could not renew the lease on range %s", self.range_id, exc_info=True)
                    continue
            if not ours:
                self.lose("the heartbeat found it gone")
                return

    def lose(self, why: str) -> None:
        """Record that this execution no longer holds the lease, and cancel its hypervisor
        call if one is running (``run_async`` then raises ``LeaseLost``)."""
        if not self.lost.is_set():
            logger.error(
                "range %s: this execution lost its lease (%s): abandoned, or expired and taken over. "
                "It stops acting on the range.", self.range_id, why,
            )
        self.lost.set()
        loop = self.loop
        if loop is not None:
            with contextlib.suppress(RuntimeError):  # the call finished and its loop closed meanwhile
                loop.call_soon_threadsafe(_cancel_all, loop)

    def verify(self) -> None:
        """Raise ``LeaseLost`` unless this execution still holds the lease (database check)."""
        if not self.lost.is_set():
            with self.session_factory() as db:
                if _holds(db, self.range_id, self.holder):
                    return
            self.lose("checked before a hypervisor call")
        raise LeaseLost(f"range {self.range_id}: this execution no longer holds its lease")


def _cancel_all(loop) -> None:
    for t in asyncio.all_tasks(loop):
        t.cancel()


def ensure_held(db, range_id: str) -> None:
    """Inside a fenced task, for its own range: raise ``LeaseLost`` unless the task still
    holds the lease, read in ``db``'s transaction. A no-op anywhere else."""
    lease = _current.get()
    if lease is None or lease.range_id != str(range_id):
        return
    if lease.lost.is_set() or not _holds(db, range_id, lease.holder):
        lease.lose("checked before a database side effect")
        raise LeaseLost(f"range {range_id}: this execution no longer holds its lease")


def guarded_range_update(db, range_id: str, new_state: str, **kwargs) -> int:
    """``db_ops.update_range_state``, fenced: inside a fenced task, for its own range, the
    UPDATE also requires that the task still holds the range's lease, unexpired. When it
    does not, nothing is written and ``LeaseLost`` is raised. Outside one (the claim's
    state check, untasked callers) it is the plain update."""
    lease = _current.get()
    if lease is None or lease.range_id != str(range_id):
        return db_ops.update_range_state(db, range_id, new_state, **kwargs)
    written = db_ops.update_range_state(db, range_id, new_state, lease_holder=lease.holder, **kwargs)
    if written or _holds(db, range_id, lease.holder):
        return written  # 0 with the lease held: the state moved on (only_from), as before
    lease.lose(f"refused to write state {new_state!r}")
    if kwargs.get("output"):
        logger.error("range %s: built but not recorded, check the hypervisor: %s", range_id, kwargs["output"][:2000])
    raise LeaseLost(f"range {range_id}: not writing {new_state!r}, this execution no longer holds its lease")


def claim(session_factory, in_state, range_id: str, state: str | None, action: str = "range") -> str | None:
    """Claim ``range_id`` for this execution. Returns the holder token to ``release`` with;
    ``LEASE_HELD`` when the range is still ``state`` but another execution holds its lease
    (``defer``); None when the range is no longer ``state`` (``skipped``).

    ``in_state(range_id, state)`` says whether the range is in ``state``; a ``state`` of
    None means the lease alone (a restore, which has no in-progress range state).

    The holder is ``<action>:<random token>``: one execution (not the Celery task id,
    which a redelivered copy shares), and the action, by which an abandon of the range's
    operation finds the lease to release (app/range_ops/service.py)."""
    holder = f"{action}:{uuid.uuid4().hex}"
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
    hold the lease for ``KEPT_LEASE_SECONDS`` instead of releasing it (no heartbeat now)."""
    try:
        with session_factory() as db:
            _extend_lease(db, range_id, holder, KEPT_LEASE_SECONDS)
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
    or ``defer`` when it cannot); renew the lease by heartbeat while it runs; when it ends,
    ``release`` the lease, except after a soft time limit, when the call may still run in a
    thread and the lease is ``keep``-ed. An execution that lost the lease (``LeaseLost``)
    ends with ``lease_lost``: nothing recorded, no retry."""

    def wrap(fn):
        @functools.wraps(fn)
        def run(task, range_id: str, *args, **kwargs):
            from .tasks import _db_session, _in_state  # tasks imports this module

            holder = claim(_db_session, _in_state, range_id, state, action)
            if holder is None:
                return skipped(action, range_id, state)
            if holder == LEASE_HELD:
                return defer(task, action, range_id, *args, **kwargs)
            lease = _Lease(_db_session, str(range_id), holder)
            token = _current.set(lease)
            lease.start()
            try:
                result = fn(task, range_id, *args, **kwargs)
            except LeaseLost as exc:
                lease.stop()
                release(_db_session, range_id, holder)  # only if still ours (expired, not taken)
                logger.error("[%s] range %s: %s; stopped, nothing recorded", action, range_id, exc)
                return {"status": "lease_lost", "range_id": range_id}
            except SoftTimeLimitExceeded:
                lease.stop()
                keep(_db_session, range_id, holder)
                raise
            except BaseException:
                lease.stop()
                release(_db_session, range_id, holder)
                raise
            finally:
                lease.stop()
                _current.reset(token)
            release(_db_session, range_id, holder)
            return result

        return run

    return wrap
