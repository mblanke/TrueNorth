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
``LEASE_SECONDS``; it used to be held for an hour, refusing every new provision. A
renewal never shortens a lease, and re-takes this execution's own lease even after it
expired (a database outage longer than the lease), unless another execution took it.
A kept lease (soft limit) is set once to ``KEPT_LEASE_SECONDS`` and not renewed. Expiry
is written and compared in database time (PostgreSQL ``now()``), not the hosts' clocks.

**Abandoned: a tombstone.** An operator's abandon of the range operation does not delete
the lease: it renames it ``abandoned:<holder>`` (app/range_ops/service.py; holders are
``<action>:<token>``), expiring ``LEASE_SECONDS`` later. The range stays blocked while
the tombstone lives, so no new build can collide with VMs, port groups or a teardown the
old execution still has in flight on vCenter. Then:

* a dead worker renews nothing: the tombstone expires and the range is free within
  ``LEASE_SECONDS``;
* a live one is fenced: it starts no further hypervisor call (``run_async`` checks the
  lease first), makes no reservation, and writes no range state (``guarded_range_update``
  locks the range and lease rows and finds the holder renamed). Its heartbeat renews the
  tombstone instead, so the range stays blocked while its call runs to the end (it is not
  cancelled: cancelling stops neither its threads nor vCenter's tasks). A finished build
  sees its ``ready`` refused and tears down what it built (``discard_built``), still under
  the tombstone; a failure is not retried. Then it deletes the tombstone.

**Taken over.** A lease that expired unrenewed and that another execution then claimed
is lost for good: ``LeaseLost``, a ``BaseException`` so that the tasks' ``except
Exception`` paths (write ``failed``, retry) do not run. ``fenced`` returns
``{"status": "lease_lost"}`` and nothing is recorded; what the call built is logged.
"""

from __future__ import annotations

import asyncio
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
from .provisioners.base import ExperimentalProvisionerError
from .range_alloc import AllocationError

# The API owns the table (app/range_leases, migration d2e3f4a5b6c7); tables.py mirrors it.
from .tables import range_leases
from .windows_roles import RoleError

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
# A full VLAN/address pool, Windows Server roles that cannot share a VM, or an experimental
# backend that is switched off: retrying won't help.
FINAL_ERRORS = (SoftTimeLimitExceeded, AllocationError, RoleError, ExperimentalProvisionerError)


class LeaseLost(BaseException):  # noqa: N818 — a fence, not an error the task handles
    """This execution may not act on its range any more (abandoned, or expired and taken).

    A ``BaseException``: the range tasks' ``except Exception`` paths (record ``failed``,
    retry) must not run for an execution that is no longer the range's. ``fenced`` turns
    it into a ``lease_lost`` (or ``abandoned``) result."""


# An abandon renames a lease to this prefix + its holder (app/range_ops/service.py).
ABANDONED = "abandoned:"


def tombstone(holder: str) -> str:
    return ABANDONED + holder


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

    In a fenced task the lease is checked in the database first: an abandoned or lost
    execution starts no hypervisor call (``LeaseLost``). A call already running when its
    operation is abandoned is not cancelled; if it then fails (not by the time limit), its
    threads are waited for while the heartbeat keeps the tombstone, so that the range is
    not handed to a new operation under them.
    """
    lease = _current.get()
    if lease is not None:
        try:
            lease.verify()
        except BaseException:
            coro.close()  # never started
            raise
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
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
        if lease is not None and not isinstance(exc, SoftTimeLimitExceeded) and lease.refresh() == "abandoned":
            loop.run_until_complete(loop.shutdown_default_executor())  # drain, under the tombstone
        raise
    finally:
        asyncio.set_event_loop(None)
        loop.close()  # shuts the executor down without waiting for its threads


# ── the lease ─────────────────────────────────────────────────────────


def _postgres(db) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def _db_time(db, seconds: float = 0):
    """Now, plus ``seconds``, in database time: every worker and the API compare one clock.
    SQLite (the tests) has no interval arithmetic; there it is this process's clock."""
    if _postgres(db):
        return sa.func.now() + timedelta(seconds=seconds) if seconds else sa.func.now()
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _claim_lease(db, range_id: str, holder: str) -> bool:
    """Take the range's lease for ``holder`` unless another unexpired holder has it (an
    expired one, a tombstone included, is taken over)."""
    if _postgres(db):
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    expires = _db_time(db, LEASE_SECONDS)
    stmt = insert(range_leases).values(range_id=range_id, holder=holder, expires_at=expires)
    stmt = stmt.on_conflict_do_update(
        index_elements=[range_leases.c.range_id],
        set_={"holder": holder, "expires_at": expires},
        where=range_leases.c.expires_at < _db_time(db),
    )
    # RETURNING, not rowcount: psycopg reports -1 for an upsert.
    return db.execute(stmt.returning(range_leases.c.holder)).first() is not None


def _release_lease(db, range_id: str, holder: str) -> None:
    """Delete ``holder``'s lease, or its tombstone (abandoned)."""
    db.execute(
        sa.delete(range_leases).where(
            range_leases.c.range_id == range_id, range_leases.c.holder.in_([holder, tombstone(holder)])
        )
    )


def _extend_lease(db, range_id: str, holder: str, seconds: int | None = None) -> bool:
    """Make ``holder``'s lease last at least ``seconds`` (``LEASE_SECONDS``) from now.
    False when ``holder`` does not have it.

    Never shortens it (a renewal racing ``keep``). Re-takes ``holder``'s own lease even
    after it expired: a database outage longer than the lease must not cost a healthy
    task its range. Safe because nothing hands the range on while it is in progress
    except a takeover of the lease (the holder changes) or an abandon (renamed)."""
    later = _db_time(db, seconds or LEASE_SECONDS)
    greater = sa.func.greatest if _postgres(db) else sa.func.max  # SQLite's two-argument max()
    stmt = (
        sa.update(range_leases)
        .where(range_leases.c.range_id == range_id, range_leases.c.holder == holder)
        .values(expires_at=greater(range_leases.c.expires_at, later))
    )
    return db.execute(stmt).rowcount > 0


def _lease_holder(db, range_id: str, lock: bool = False) -> str | None:
    """Who holds the range's lease, expired or not; ``lock`` row-locks it until commit."""
    stmt = sa.select(range_leases.c.holder).where(range_leases.c.range_id == range_id)
    row = db.execute(stmt.with_for_update() if lock else stmt).first()
    return row[0] if row else None


class _Lease:
    """The lease a running fenced task holds, and the heartbeat that renews it.

    The heartbeat is a daemon thread: it dies with the process, so a dead worker's lease
    expires ``LEASE_SECONDS`` after its last renewal. Once the operation is abandoned it
    renews the tombstone instead (``current``), until the task ends; once the lease is
    taken over it stops."""

    def __init__(self, session_factory, range_id: str, holder: str):
        self.session_factory, self.range_id, self.holder = session_factory, range_id, holder
        self.current = holder  # what the heartbeat renews: the lease, or its tombstone
        self.abandoned = threading.Event()  # renamed to tombstone(holder) by an abandon
        self.lost = threading.Event()  # taken over by another execution, or gone
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._beat, name=f"lease-{range_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Stop renewing. A renewal still in flight cannot shorten a ``keep`` (never
        shortens) nor revive a released lease (no row)."""
        self._stopped.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def _beat(self) -> None:
        while not self._stopped.wait(LEASE_HEARTBEAT_SECONDS):
            try:
                with self.session_factory() as db:
                    if _extend_lease(db, self.range_id, self.current):
                        continue
                    if self.observe(_lease_holder(db, self.range_id)) == "abandoned":
                        _extend_lease(db, self.range_id, self.current)
            except Exception:  # noqa: BLE001 — a database blip: renewal re-takes the lease when it is back
                logger.warning("could not renew the lease on range %s", self.range_id, exc_info=True)
                continue
            if self.lost.is_set():
                return

    def observe(self, holder_now: str | None) -> str:
        """Classify the lease's current holder: ``held``, ``abandoned`` (our tombstone; from
        now on the heartbeat renews that) or ``lost``."""
        if holder_now == self.holder and not self.abandoned.is_set():
            return "held"
        if holder_now == tombstone(self.holder):
            if not self.abandoned.is_set():
                logger.warning(
                    "range %s: the operation was abandoned. This execution starts nothing more and writes "
                    "nothing; it keeps the range blocked until the work it has in flight ends.", self.range_id,
                )
            self.current = holder_now
            self.abandoned.set()
            return "abandoned"
        if not self.lost.is_set():
            logger.error("range %s: this execution's lease was taken over (now %r); it stops acting on the range",
                         self.range_id, holder_now)
        self.lost.set()
        return "lost"

    def refresh(self) -> str:
        """``observe`` the database now; ``unknown`` when it cannot be read."""
        try:
            with self.session_factory() as db:
                return self.observe(_lease_holder(db, self.range_id))
        except Exception:  # noqa: BLE001
            logger.warning("could not read the lease on range %s", self.range_id, exc_info=True)
            return "unknown"

    def verify(self) -> None:
        """Raise ``LeaseLost`` unless this execution still holds the lease (database check)."""
        with self.session_factory() as db:
            status = self.observe(_lease_holder(db, self.range_id))
        if status != "held":
            raise LeaseLost(f"range {self.range_id}: {status}; no hypervisor call is started")

    def keep(self) -> None:
        keep(self.session_factory, self.range_id, self.current)

    def release(self) -> None:
        release(self.session_factory, self.range_id, self.holder)


def ensure_held(db, range_id: str) -> None:
    """Inside a fenced task, for its own range: raise ``LeaseLost`` unless the task still
    holds the lease, read in ``db``'s transaction. A no-op anywhere else."""
    lease = _current.get()
    if lease is None or lease.range_id != str(range_id):
        return
    status = lease.observe(_lease_holder(db, range_id))
    if status != "held":
        raise LeaseLost(f"range {range_id}: {status}; nothing reserved")


def guarded_range_update(db, range_id: str, new_state: str, **kwargs) -> int:
    """``db_ops.update_range_state``, fenced: inside a fenced task, for its own range, it
    writes only while the task holds the range's lease.

    The range row, then the lease row, are locked first (in the order the API's accept
    and abandon lock them: no deadlock), so neither an abandon nor a takeover can land
    between the check and the write. Abandoned: nothing is written and 0 returned, as
    for a range whose state moved on (a finished build then discards what it built,
    still under the tombstone). Taken over: nothing is written, ``LeaseLost``. Outside a
    fenced task (the claim's own state check, untasked callers) it is the plain update."""
    lease = _current.get()
    if lease is None or lease.range_id != str(range_id):
        return db_ops.update_range_state(db, range_id, new_state, **kwargs)
    db_ops.lock_range(db, range_id)
    status = lease.observe(_lease_holder(db, range_id, lock=True))
    if status == "held":
        return db_ops.update_range_state(db, range_id, new_state, **kwargs)
    if status == "abandoned":
        logger.warning("range %s: its operation was abandoned; not recording %r", range_id, new_state)
        if kwargs.get("output"):  # a finished build: discarded next (discard_built), shown here in case that fails
            logger.warning("range %s: built after the abandon, to be discarded: %s", range_id, kwargs["output"][:2000])
        return 0
    if kwargs.get("output"):
        logger.error("range %s: built but not recorded, check the hypervisor: %s", range_id, kwargs["output"][:2000])
    raise LeaseLost(f"range {range_id}: not writing {new_state!r}, the lease was taken over")


def record_leftover(session_factory, range_id: str, result: dict) -> dict:
    """Record on the range what a failed ``discard_built`` left on the hypervisor: its
    VMs, port groups, mirrors and uplink go into ``provisioner_output`` (so the API's
    "the range still has N VMs; destroy it first" refuses a new build over them, and a
    destroy tears them down) with a warning. The range's state is not touched. Returns
    the task's result without the bulky ``leftover``."""
    leftover = result["leftover"]
    warning = (
        f"A build of this range that was no longer wanted (abandoned, or torn down meanwhile) could not be "
        f"discarded: {'; '.join(result.get('errors') or []) or 'no detail'}. Its {len(leftover.get('vms') or [])} "
        "VMs are recorded here; destroy the range before building it again."
    )
    try:
        with session_factory() as db:
            db_ops.merge_range_output(db, range_id, {**leftover, "warnings": [warning]})
    except Exception:  # noqa: BLE001 — the error log of discard_built still lists them
        logger.error("range %s: could not record the undiscarded build: %s", range_id, leftover, exc_info=True)
    return {k: v for k, v in result.items() if k != "leftover"}


def snapshot_back_to_ready(range_id: str, snapshot_id: str, *args, **kwargs) -> None:
    """``on_lost`` for restore_snapshot: a fenced-out restore gives its snapshot back
    (``restoring`` -> ``ready``), as a final failure does, so it is not stuck."""
    from .tasks import _update_snapshot_state  # tasks imports this module

    _update_snapshot_state(snapshot_id, "ready", only_from=("restoring",))


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
    """Give the range's lease back (the task ended, or will be retried), or its tombstone
    (the task's operation was abandoned, and its work has ended)."""
    try:
        with session_factory() as db:
            _release_lease(db, range_id, holder)
    except Exception:  # noqa: BLE001 — an unreleased lease only delays a takeover until it expires
        logger.warning("could not release the lease on range %s", range_id, exc_info=True)


def fenced(action: str, state: str | None, on_lost=None):
    """Decorate a bound range task ``fn(task, range_id, ...)``: ``claim`` first (``skipped``
    or ``defer`` when it cannot); renew the lease by heartbeat while it runs; when it ends,
    ``release`` the lease (or its tombstone), except after a soft time limit, when the call
    may still run in a thread and the lease is ``keep``-ed.

    An execution fenced out (``LeaseLost``), or one that fails after its operation was
    abandoned, ends with ``lease_lost`` / ``abandoned``: not retried, nothing recorded.
    ``on_lost(range_id, *args, **kwargs)`` then gives back what the task had taken (a
    restore's snapshot)."""

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
            except SoftTimeLimitExceeded:
                lease.stop()
                lease.keep()  # the lease, or the tombstone: the call may still be running
                raise
            except (Exception, LeaseLost) as exc:
                lease.stop()
                fenced_out = isinstance(exc, LeaseLost) or lease.abandoned.is_set() or lease.lost.is_set()
                if not (fenced_out or lease.refresh() in ("abandoned", "lost")):
                    lease.release()
                    raise
                lease.release()  # the tombstone: run_async drained the call's threads first
                outcome = "abandoned" if lease.abandoned.is_set() else "lease_lost"
                logger.error("[%s] range %s: %s (%s); not retried, nothing recorded", action, range_id, outcome, exc)
                if on_lost is not None:
                    try:
                        on_lost(range_id, *args, **kwargs)
                    except Exception:  # noqa: BLE001
                        logger.warning("[%s] range %s: on_lost failed", action, range_id, exc_info=True)
                return {"status": outcome, "range_id": range_id}
            except BaseException:
                lease.stop()
                lease.release()
                raise
            finally:
                lease.stop()
                _current.reset(token)
            if isinstance(result, dict) and result.get("leftover"):  # discard_built could not tear it down
                result = record_leftover(_db_session, range_id, result)
            lease.release()  # only now: the leftover is on the range before anyone can build over it
            if lease.abandoned.is_set():
                logger.warning("[%s] range %s: finished after its operation was abandoned: %s", action, range_id, result)
            return result

        return run

    return wrap
