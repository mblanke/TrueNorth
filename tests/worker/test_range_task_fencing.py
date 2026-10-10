"""Range tasks act only for the request that was recorded, one execution at a time
(worker/fencing.py; CR1-06 and CR1-11 in docs/review/codereview1.md).

On main a provision accepted a range already in ``provisioning``, so a second copy of the
task (redelivered after a worker loss with late acks, or sent twice) arriving while the
first was still building built the range a second time: two sets of VMs, the second's
output overwriting the first's and orphaning its VMs. A lab's end, reset and expiry send
the same tasks. Re-landed from #18 and #39, with what their five review rounds found.

These run the real tasks against SQLite (and one against PostgreSQL), with the
hypervisor stubbed.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

os.environ.setdefault("MOCK_PROVISION_DELAY", "0")
os.environ.setdefault("MOCK_FAILURE_RATE", "0")

import sqlalchemy as sa  # noqa: E402
from celery.exceptions import SoftTimeLimitExceeded  # noqa: E402
from worker import fencing, tasks  # noqa: E402
from worker.provisioners.results import DestroyResult, ProvisionResult  # noqa: E402

OUTPUT = '{"vms": [{"name": "r-dc01", "vm_id": "vm-1"}]}'


@pytest.fixture(autouse=True)
def _requeue_recorded(requeued):
    """Every re-queue (fencing.defer) is recorded (tests/worker/conftest.py); none is sent."""


def _range(db, state: str, output: str | None = None) -> str:
    rid, tid = str(uuid.uuid4()), str(uuid.uuid4())
    with db.begin() as conn:
        conn.execute(sa.text("INSERT INTO templates VALUES (:id, 'id: t\n')"), {"id": tid})
        conn.execute(
            sa.text(
                "INSERT INTO ranges (id, template_id, state, provisioner_backend, provisioner_output) "
                "VALUES (:id, :t, :s, 'mock', :o)"
            ),
            {"id": rid, "t": tid, "s": state, "o": output},
        )
    return rid


def _state(db, rid: str) -> tuple:
    with db.connect() as conn:
        return tuple(conn.execute(sa.text("SELECT state, error_message FROM ranges WHERE id = :id"), {"id": rid}).one())


def _leases(db, rid: str) -> int:
    with db.connect() as conn:
        return conn.execute(sa.text("SELECT count(*) FROM range_leases WHERE range_id = :i"), {"i": rid}).scalar()


class SlowBackend:
    """A hypervisor call that blocks until released, and counts how often it was made."""

    def __init__(self):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    async def _hold(self):
        self.calls += 1
        self.started.set()
        await asyncio.to_thread(self.release.wait, 10)

    async def provision(self, range_id, template, allocations):
        await self._hold()
        return ProvisionResult(status="ok", vms=[{"name": "x", "vm_id": "vm-9"}])

    async def destroy(self, range_id, output):
        await self._hold()
        return DestroyResult(status="ok", resources_removed=1)


# ── state: only the recorded request ────────────────────────────────────


def test_a_recorded_provision_runs_and_reaches_ready(db):
    rid = _range(db, "provisioning")
    assert tasks.provision_range.run(rid)["status"] == "ready"
    assert _state(db, rid)[0] == "ready" and _leases(db, rid) == 0


@pytest.mark.parametrize("state", ["ready", "destroying", "destroyed", "created", "failed"])
def test_a_duplicate_or_stale_provision_changes_nothing(db, state):
    rid = _range(db, state)
    with patch.object(tasks, "_get_backend") as backend:
        assert tasks.provision_range.run(rid)["status"] == "skipped"
    backend.assert_not_called()
    assert _state(db, rid)[0] == state and _leases(db, rid) == 0


@pytest.mark.parametrize("state", ["destroyed", "ready", "provisioning", "failed"])
def test_a_duplicate_or_stale_destroy_changes_nothing(db, state):
    rid = _range(db, state, output=OUTPUT)
    with patch.object(tasks, "_get_backend") as backend:
        assert tasks.destroy_range.run(rid)["status"] == "skipped"
    backend.assert_not_called()
    assert _state(db, rid)[0] == state


def test_a_failed_attempt_that_will_be_retried_leaves_the_range_in_progress(db):
    rid = _range(db, "provisioning")
    with (
        patch.object(tasks, "_get_backend", side_effect=RuntimeError("vCenter busy")),
        patch.object(tasks, "_last_attempt", return_value=False),
        pytest.raises(RuntimeError),
    ):
        tasks.provision_range.run(rid)
    assert _state(db, rid) == ("provisioning", None), "the retry must still find the range in provisioning"
    assert tasks.provision_range.run(rid)["status"] == "ready", "and then it proceeds: the lease was released"


def test_the_last_failed_attempt_records_failed(db):
    rid = _range(db, "provisioning")
    with (
        patch.object(tasks, "_get_backend", side_effect=RuntimeError("vCenter refused the clone")),
        pytest.raises(RuntimeError),
    ):
        tasks.provision_range.run(rid)  # called directly: no retry follows
    assert _state(db, rid) == ("failed", "vCenter refused the clone") and _leases(db, rid) == 0


def test_a_destroy_reaches_destroyed_and_a_redelivery_is_skipped(db):
    rid = _range(db, "destroying", output=OUTPUT)
    with patch.object(tasks, "_get_backend", return_value=SlowBackend()) as backend:
        backend.return_value.release.set()
        assert tasks.destroy_range.run(rid)["status"] == "destroyed"
        assert tasks.destroy_range.run(rid)["status"] == "skipped"
    assert _state(db, rid)[0] == "destroyed"


# ── the lease: one execution at a time ──────────────────────────────────


@pytest.mark.parametrize(
    ("task", "state", "output"), [("provision_range", "provisioning", None), ("destroy_range", "destroying", OUTPUT)]
)
def test_a_second_copy_while_the_first_runs_does_nothing(db, task, state, output, requeued):
    rid = _range(db, state, output)
    backend = SlowBackend()
    results: dict = {}
    with patch.object(tasks, "_get_backend", return_value=backend):
        first = threading.Thread(target=lambda: results.update(first=getattr(tasks, task).run(rid)))
        first.start()
        assert backend.started.wait(10), "the first copy never reached the hypervisor"
        results["second"] = getattr(tasks, task).run(rid)  # arrives while the first runs
        backend.release.set()
        first.join(20)
    assert backend.calls == 1, f"{backend.calls} {task} calls reached the hypervisor"
    # Not dropped: re-queued, to find the range finished (skip) or the lease expired (take over).
    assert results["second"]["status"] == "deferred" and [c["task"] for c in requeued] == [f"worker.tasks.{task}"]
    assert requeued[0]["countdown"] == fencing.LEASE_RETRY_SECONDS
    assert results["first"]["status"] != "skipped"
    assert getattr(tasks, task).run(rid)["status"] == "skipped", "after the first finished, a late copy does nothing"


def test_a_destroy_waits_for_a_build_still_running_then_tears_down_what_it_built(db, requeued):
    """A lab ended while it was still being built: the destroy must not run alongside the
    build, and the build, finding the range moved to destroying, discards its VMs."""
    rid = _range(db, "provisioning")
    backend = SlowBackend()
    results: dict = {}
    with patch.object(tasks, "_get_backend", return_value=backend):
        build = threading.Thread(target=lambda: results.update(build=tasks.provision_range.run(rid)))
        build.start()
        assert backend.started.wait(10)
        with db.begin() as conn:  # the lab ends: its sender records the teardown
            conn.execute(sa.text("UPDATE ranges SET state = 'destroying' WHERE id = :i"), {"i": rid})
        assert tasks.destroy_range.run(rid)["status"] == "deferred"
        backend.release.set()
        build.join(20)
        assert results["build"]["status"] == "discarded"
        assert tasks.destroy_range.run(rid)["status"] == "destroyed"  # the re-queued copy, later


def test_a_lease_left_by_a_dead_worker_expires(db):
    rid = _range(db, "provisioning")
    with db.begin() as conn:
        conn.execute(
            fencing.range_leases.insert().values(
                range_id=rid, holder="dead-worker", expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
    assert tasks.provision_range.run(rid)["status"] == "ready"


def test_a_restore_takes_the_range_lease(db, requeued):
    """A restore is never run alongside a build, a teardown or another restore."""
    rid = _range(db, "ready", OUTPUT)
    with db.begin() as conn:
        conn.execute(
            fencing.range_leases.insert().values(
                range_id=rid, holder="other", expires_at=datetime.now(UTC) + timedelta(seconds=60)
            )
        )
    with patch.object(tasks, "_get_backend") as backend:
        result = tasks.restore_snapshot.run(rid, str(uuid.uuid4()))
    backend.assert_not_called()
    assert result["status"] == "deferred" and requeued[0]["task"] == "worker.tasks.restore_snapshot"


# ── time limits ─────────────────────────────────────────────────────────


def test_the_task_time_limit_ends_a_task_before_the_broker_redelivers_it():
    from worker.celery_app import app

    visibility = app.conf.broker_transport_options["visibility_timeout"]
    for task in (tasks.provision_range, tasks.destroy_range, tasks.snapshot_range, tasks.restore_snapshot):
        assert task.soft_time_limit < task.time_limit < visibility, task.name
        # After the soft limit nothing renews the lease: the kept lease outlives the hard limit.
        assert task.time_limit - task.soft_time_limit < fencing.KEPT_LEASE_SECONDS, task.name
    # A running task renews its lease several times per lease: a dead worker's expires in minutes.
    assert fencing.LEASE_SECONDS <= 300 and fencing.LEASE_HEARTBEAT_SECONDS * 3 <= fencing.LEASE_SECONDS
    # Only range tasks: a health check over every range must not be cut off at 55 minutes.
    assert tasks.health_check_ranges.soft_time_limit is None


def test_the_soft_limit_is_final_not_retried():
    from worker.provisioners import ExperimentalProvisionerError
    from worker.range_alloc import AllocationError
    from worker.windows_roles import RoleError

    # and a full VLAN/address pool (worker/range_alloc.py): retrying does not empty it;
    # and Windows Server roles that cannot share a VM: retrying does not separate them;
    # and a switched-off experimental backend: retrying does not switch it on
    assert tasks.ReliableTask.dont_autoretry_for == (
        SoftTimeLimitExceeded, AllocationError, RoleError, ExperimentalProvisionerError)


class _CutOffCall:
    """A hypervisor call, running in its thread, that the soft time limit cuts off.

    Celery raises ``SoftTimeLimitExceeded`` from SIGALRM in the task's (main) thread. These
    tests used a 0.3 s wall-clock timer for it, which raced the task's own set-up: on a
    loaded runner the claim took longer, the signal landed before the task body's ``try``,
    and the range was left as it was (CI: ``'ready' == 'failed'``). Here the call itself
    sends the signal to the main thread once it is running and the task is blocked
    waiting for it in the event loop's ``select``, as a task is when its hypervisor call
    is what takes the time. So the limit lands while the call is in flight, every time,
    whatever the load. The thread then blocks until the test releases it (``finished``
    stays clear until then)."""

    def __init__(self):
        self.release = threading.Event()
        self.finished = threading.Event()

    @staticmethod
    def _task_waits_on_the_call(main: int) -> bool:
        frame = sys._current_frames().get(main)
        return frame is not None and frame.f_code.co_name == "select" and frame.f_code.co_filename.endswith("selectors.py")

    def _thread(self) -> None:
        main = threading.main_thread().ident
        for _ in range(10_000):  # a barrier, not a timing guess: up to 10 s for the task to reach select
            if self._task_waits_on_the_call(main):
                break
            time.sleep(0.001)
        else:
            raise AssertionError("the task never waited on its hypervisor call")
        signal.pthread_kill(main, signal.SIGALRM)
        self.release.wait(10)
        self.finished.set()

    async def run(self, *a, **k) -> None:
        await asyncio.to_thread(self._thread)


@contextmanager
def _soft_limit():
    """Celery's soft-limit handler, installed for the block; yields the call it cuts off,
    whose thread is released on the way out."""

    def soft_limit(signum, frame):
        raise SoftTimeLimitExceeded()

    previous = signal.signal(signal.SIGALRM, soft_limit)
    call = _CutOffCall()
    try:
        yield call
    finally:
        signal.signal(signal.SIGALRM, previous)
        call.release.set()


def test_after_the_soft_limit_the_calls_cleanup_runs_without_waiting_for_its_thread():
    """asyncio.run waited for the hypervisor's threads before re-raising, holding the
    task's cleanup past the hard limit; abandoning the coroutine skipped its finally."""
    cleaned = []

    with _soft_limit() as stuck:

        async def call():
            try:
                await stuck.run()
            finally:
                cleaned.append("finally")

        with pytest.raises(SoftTimeLimitExceeded):
            fencing.run_async(call())
        assert not stuck.finished.is_set(), "it waited for the hypervisor thread"
    assert cleaned == ["finally"]


def test_after_the_soft_limit_the_range_is_failed_and_stays_leased(db, requeued):
    """The hypervisor call can outlive the task in a thread; releasing the lease would let
    a new destroy run alongside it. The lease is kept (until it expires) instead."""
    rid = _range(db, "destroying", OUTPUT)
    with _soft_limit() as stuck:

        class StuckBackend:
            async def destroy(self, range_id, output):
                await stuck.run()

        with (
            patch.object(tasks, "_get_backend", return_value=StuckBackend()),
            patch.object(tasks, "_last_attempt", return_value=False),  # final because of the limit, not the count
            pytest.raises(SoftTimeLimitExceeded),
        ):
            tasks.destroy_range.run(rid)
        assert not stuck.finished.is_set(), "the destroy is still running in its thread"
    assert _state(db, rid)[0] == "failed", "final at once: the soft limit is not retried"
    assert _leases(db, rid) == 1, "the lease was released while the destroy may still be running"
    with db.begin() as conn:  # someone asks for the teardown again
        conn.execute(sa.text("UPDATE ranges SET state = 'destroying' WHERE id = :i"), {"i": rid})
    backend = SlowBackend()
    with patch.object(tasks, "_get_backend", return_value=backend):
        assert tasks.destroy_range.run(rid)["status"] == "deferred"
    assert backend.calls == 0


# ── PostgreSQL ──────────────────────────────────────────────────────────


def test_on_postgres_a_second_copy_while_the_first_runs_does_nothing(postgres_engine, monkeypatch, requeued):
    """The same, on the migrated schema (range_leases from the migration, the upsert's
    RETURNING as psycopg reports it, native UUID ids)."""
    from app import models as m
    from sqlalchemy.orm import Session, sessionmaker

    factory = sessionmaker(bind=postgres_engine, class_=Session, expire_on_commit=False)

    @contextmanager
    def session():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    with factory() as s:
        tenant = m.Tenant(name="t", slug=f"t-{uuid.uuid4().hex[:6]}")
        s.add(tenant)
        s.flush()
        tmpl = m.Template(name="t", yaml="id: t\n", tenant_id=tenant.id)
        s.add(tmpl)
        s.flush()
        rng = m.Range(name="r", template_id=tmpl.id, tenant_id=tenant.id, state=m.RangeState.provisioning)
        s.add(rng)
        s.commit()
        rid = str(rng.id)
    monkeypatch.setattr(tasks, "_db_session", session)
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    backend = SlowBackend()
    results: dict = {}
    with patch.object(tasks, "_get_backend", return_value=backend):
        first = threading.Thread(target=lambda: results.update(first=tasks.provision_range.run(rid)))
        first.start()
        assert backend.started.wait(10), "the first copy never reached the hypervisor"
        results["second"] = tasks.provision_range.run(rid)
        backend.release.set()
        first.join(20)
    assert backend.calls == 1 and results["second"]["status"] == "deferred"
    assert results["first"]["status"] == "ready"
    with factory() as s:
        assert s.get(m.Range, rng.id).state == m.RangeState.ready
        assert s.execute(sa.text("select count(*) from range_leases")).scalar() == 0


# ── From the adversarial review of 2ab83f0 ──────────────────────────────


def test_a_failed_state_check_gives_the_lease_back(db):
    rid = _range(db, "provisioning")
    with patch.object(tasks, "_in_state", side_effect=RuntimeError("database went away")), pytest.raises(RuntimeError):
        tasks.provision_range.run(rid)
    assert _leases(db, rid) == 0, "a lab would wait out the whole lease for its build"
    assert tasks.provision_range.run(rid)["status"] == "ready"


def test_a_late_copy_for_a_deleted_range_is_skipped_not_retried(db):
    """range_leases.range_id references ranges.id: on PostgreSQL the lease insert for a
    deleted range raises IntegrityError, which ReliableTask would retry three times."""
    rid = _range(db, "destroyed")

    def gone(*a, **k):
        raise sa.exc.IntegrityError("INSERT INTO range_leases", {}, Exception("violates foreign key"))

    with patch.object(fencing, "_claim_lease", gone), patch.object(tasks, "_get_backend") as backend:
        assert tasks.destroy_range.run(rid)["status"] == "skipped"
    backend.assert_not_called()


def test_a_restore_cut_off_by_the_soft_limit_marks_the_range_failed(db, monkeypatch):
    """The revert may still be running in a thread: the range is not as it was."""
    rid = _range(db, "ready", OUTPUT)
    sid = str(uuid.uuid4())
    with db.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO range_snapshots (id, range_id, snapshot_state, snapshot_data, range_state_at_snapshot) "
                "VALUES (:i, :r, 'restoring', '{\"snapshot_name\": \"tnx\"}', 'ready')"
            ),
            {"i": sid, "r": rid},
        )

    with _soft_limit() as stuck:

        class StuckBackend:
            async def restore(self, *a, **k):
                await stuck.run()

        with (
            patch.object(tasks, "_get_backend", return_value=StuckBackend()),
            patch.object(tasks, "_last_attempt", return_value=False),
            pytest.raises(SoftTimeLimitExceeded),
        ):
            tasks.restore_snapshot.run(rid, sid)
    assert _state(db, rid)[0] == "failed" and _leases(db, rid) == 1
