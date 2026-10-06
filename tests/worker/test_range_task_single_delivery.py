"""Two overlapping deliveries of one range task: only one reaches the hypervisor.

Independent review of the candidate (2026-10-05): the worker's claim was a conditional
update from the in-progress state to itself, so a second copy arriving while the first
was still running passed it too. Two builds ran; the second's output overwrote the
first's and orphaned its VMs. A copy arrives that way when a build outlives the broker's
visibility timeout (acks_late) or when two API processes re-send one operation.

Now a task also takes a lease on the range (worker/fencing.py): one holder at a time,
released when the task ends (so a retry after a failed attempt can claim it), and
expiring, so a copy redelivered after its worker died can take over.

Written to run on the code before the fix too: the lease table is created only if the
worker knows it.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

os.environ.setdefault("MOCK_PROVISION_DELAY", "0")
os.environ.setdefault("MOCK_FAILURE_RATE", "0")

import sqlalchemy as sa  # noqa: E402
from worker import tables, tasks  # noqa: E402
from worker.provisioners.base import DestroyResult, ProvisionResult, StopResult  # noqa: E402

OUTPUT = '{"vms": [{"name": "r-dc01", "vm_id": "vm-1"}]}'


@pytest.fixture(autouse=True)
def requeued(monkeypatch):
    """Every re-queue (fencing.defer) is recorded here; none reaches a broker."""
    from celery.app.task import Task

    calls: list[dict] = []
    monkeypatch.setattr(Task, "apply_async", lambda self, *a, **k: calls.append({"task": self.name, **k}))
    return calls


def _sqlite_now(dbapi_conn, _record):
    if type(dbapi_conn).__module__.startswith("sqlite3"):
        dbapi_conn.create_function("NOW", 0, lambda: datetime.now(UTC).isoformat())


@pytest.fixture
def db(tmp_path, monkeypatch):
    sa.event.listen(sa.engine.Engine, "connect", _sqlite_now)
    url = f"sqlite:///{tmp_path / 'worker.db'}"
    engine = sa.create_engine(url, connect_args={"timeout": 30})
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE templates (id TEXT PRIMARY KEY, yaml TEXT)"))
        conn.execute(
            sa.text(
                "CREATE TABLE ranges (id TEXT PRIMARY KEY, template_id TEXT, state TEXT, provisioner_backend TEXT, "
                "provisioner_output TEXT, error_message TEXT, updated_at TIMESTAMP)"
            )
        )
    tables.network_reservations.create(engine)
    tables.range_snapshots.create(engine)
    if hasattr(tables, "range_leases"):
        tables.range_leases.create(engine)
    monkeypatch.setattr(tasks, "DATABASE_URL", url)
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    yield engine
    sa.event.remove(sa.engine.Engine, "connect", _sqlite_now)


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
        return ProvisionResult(status="ok", vms=[{"name": "x", "vm_id": "vm-9"}], networks=[], duration_seconds=0)

    async def destroy(self, range_id, output):
        await self._hold()
        return DestroyResult(status="ok", resources_removed=1, duration_seconds=0, errors=[])

    async def stop(self, range_id, output):
        await self._hold()
        return StopResult(status="ok", vms_stopped=1, duration_seconds=0, errors=[])


@pytest.mark.parametrize(
    ("task", "state", "output"),
    [
        ("provision_range", "provisioning", None),
        ("destroy_range", "destroying", OUTPUT),
        ("stop_range", "stopping", OUTPUT),
    ],
)
def test_a_second_copy_while_the_first_runs_does_nothing(db, task, state, output, requeued):
    rid = _range(db, state, output)
    backend = SlowBackend()
    results: dict = {}
    with (
        patch.object(tasks, "_get_backend", return_value=backend),
        patch.object(tasks, "reserve_for_build", return_value={}),
    ):
        first = threading.Thread(target=lambda: results.update(first=getattr(tasks, task).run(rid)))
        first.start()
        assert backend.started.wait(10), "the first copy never reached the hypervisor"
        results["second"] = getattr(tasks, task).run(rid)  # arrives while the first runs
        backend.release.set()
        first.join(20)
    assert backend.calls == 1, f"{backend.calls} {task} calls reached the hypervisor"
    # Not dropped: re-queued, to find the range finished (skip) or the lease expired (take over).
    assert results["second"]["status"] == "deferred" and [c["task"] for c in requeued] == [f"worker.tasks.{task}"]
    assert results["first"]["status"] != "skipped"
    assert getattr(tasks, task).run(rid)["status"] == "skipped", "after the first finished, a late copy does nothing"


def test_a_retry_after_a_failed_attempt_still_runs(db):
    rid = _range(db, "provisioning")
    with (
        patch.object(tasks, "_get_backend", side_effect=RuntimeError("vCenter busy")),
        patch.object(tasks, "_last_attempt", return_value=False),
        pytest.raises(RuntimeError),
    ):
        tasks.provision_range.run(rid)
    assert tasks.provision_range.run(rid)["status"] == "ready", "the lease was released for the retry"


def test_a_lease_left_by_a_dead_worker_expires(db):
    if not hasattr(tables, "range_leases"):
        pytest.skip("no lease table before the fix")
    rid = _range(db, "provisioning")
    with db.begin() as conn:
        conn.execute(
            tables.range_leases.insert().values(
                range_id=rid, holder="dead-worker", expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
    assert tasks.provision_range.run(rid)["status"] == "ready"


def test_the_task_time_limit_ends_a_task_before_the_broker_redelivers_it():
    from worker.celery_app import app

    visibility = app.conf.broker_transport_options["visibility_timeout"]
    assert app.conf.task_time_limit and app.conf.task_time_limit < visibility
    assert int(os.environ.get("VSPHERE_PROVISION_BUDGET", "3300")) < app.conf.task_time_limit


def test_on_postgres_a_second_copy_while_the_first_runs_does_nothing(monkeypatch):
    """The same, on PostgreSQL (the lease upsert's result is read differently there)."""
    from contextlib import contextmanager

    from app import models as m
    from app.sections import Base
    from sqlalchemy.orm import Session, sessionmaker

    admin_url = os.getenv("TEST_POSTGRES_ADMIN_URL")
    if not admin_url:
        pytest.skip("set TEST_POSTGRES_ADMIN_URL to run against Postgres")
    name = f"tn_lease_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    engine = sa.create_engine(sa.engine.make_url(admin_url).set(database=name), pool_size=10)
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

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
        backend = SlowBackend()
        monkeypatch.setattr(tasks, "_db_session", session)
        monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
        results: dict = {}
        with (
            patch.object(tasks, "_get_backend", return_value=backend),
            patch.object(tasks, "reserve_for_build", return_value={}),
        ):
            first = threading.Thread(target=lambda: results.update(first=tasks.provision_range.run(rid)))
            first.start()
            assert backend.started.wait(10), "the first copy never reached the hypervisor"
            results["second"] = tasks.provision_range.run(rid)
            backend.release.set()
            first.join(20)
        assert backend.calls == 1 and results["second"]["status"] == "deferred"
        assert results["first"]["status"] == "ready"
        assert tasks.provision_range.run(rid)["status"] == "skipped", (
            "after the first finished, a late copy does nothing"
        )
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


# ── From the re-review of #39 ──────────────────────────────────────────
def test_a_copy_finding_the_lease_held_is_deferred_not_dropped(db, requeued):
    """A re-send after a crash can arrive while the dead holder's lease still runs; it
    must come back later (and take over once the lease expires), not be acknowledged
    and lost with the range stuck in progress."""
    if not hasattr(tables, "range_leases"):
        pytest.skip("no lease table before the fix")
    rid = _range(db, "provisioning")
    with db.begin() as conn:
        conn.execute(
            tables.range_leases.insert().values(
                range_id=rid, holder="dead-worker", expires_at=datetime.now(UTC) + timedelta(seconds=30)
            )
        )
    with patch.object(tasks, "_get_backend") as backend:
        result = tasks.provision_range.run(rid)
    backend.assert_not_called()
    assert result["status"] == "deferred", result
    (call,) = requeued
    assert call["args"][0] == rid and call["countdown"] > 0


def test_a_soft_time_limit_records_failed_and_releases_the_lease(db):
    """The hard limit kills the process with no cleanup; the soft limit, before it,
    raises inside the task: it is final (no retry), records failed and frees the lease."""
    from celery.exceptions import SoftTimeLimitExceeded

    rid = _range(db, "provisioning")
    with (
        patch.object(tasks, "_get_backend", side_effect=SoftTimeLimitExceeded()),
        patch.object(tasks, "_last_attempt", return_value=False),  # retries would remain
        patch.object(tasks, "reserve_for_build", return_value={}),
        pytest.raises(SoftTimeLimitExceeded),
    ):
        tasks.provision_range.run(rid)
    with db.connect() as conn:
        state = conn.execute(sa.text("SELECT state FROM ranges WHERE id = :i"), {"i": rid}).scalar()
    assert state == "failed"


def test_the_soft_limit_comes_before_the_hard_one_and_both_before_redelivery():
    from celery.exceptions import SoftTimeLimitExceeded
    from worker.celery_app import app

    visibility = app.conf.broker_transport_options["visibility_timeout"]
    assert app.conf.task_soft_time_limit and app.conf.task_soft_time_limit < app.conf.task_time_limit < visibility
    assert SoftTimeLimitExceeded in tasks.ReliableTask.dont_autoretry_for


# ── From the third re-review ───────────────────────────────────────────
def test_a_destroy_hitting_the_soft_limit_records_failed(db):
    """It used to check only _last_attempt: with the retry suppressed (FINAL_ERRORS), the
    range stayed `destroying` with nothing to settle it."""
    from celery.exceptions import SoftTimeLimitExceeded

    rid = _range(db, "destroying", OUTPUT)
    with (
        patch.object(tasks, "_get_backend", side_effect=SoftTimeLimitExceeded()),
        patch.object(tasks, "_last_attempt", return_value=False),
        pytest.raises(SoftTimeLimitExceeded),
    ):
        tasks.destroy_range.run(rid)
    with db.connect() as conn:
        assert conn.execute(sa.text("SELECT state FROM ranges WHERE id = :i"), {"i": rid}).scalar() == "failed"


def test_the_soft_limit_is_not_held_up_by_a_hypervisor_call_in_a_thread(db):
    """asyncio.run waits for in-flight to_thread work before re-raising, so a long vSphere
    call could push the cleanup past the hard limit (which kills it). The task must surface
    the soft limit at once."""
    import signal
    import time

    from celery.exceptions import SoftTimeLimitExceeded

    class SlowThreadBackend:
        async def destroy(self, range_id, output):
            await asyncio.to_thread(time.sleep, 3)

    def soft_limit(signum, frame):
        raise SoftTimeLimitExceeded()

    rid = _range(db, "destroying", OUTPUT)
    previous = signal.signal(signal.SIGALRM, soft_limit)
    signal.setitimer(signal.ITIMER_REAL, 0.3)
    started = time.monotonic()
    try:
        with (
            patch.object(tasks, "_get_backend", return_value=SlowThreadBackend()),
            pytest.raises(SoftTimeLimitExceeded),
        ):
            tasks.destroy_range.run(rid)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert time.monotonic() - started < 1.5, "the cleanup waited for the hypervisor thread"


# ── From the fourth re-review ──────────────────────────────────────────
def _soft_limit_after(seconds: float):
    """Raise SoftTimeLimitExceeded in this (main) thread after ``seconds``, as Celery does."""
    import signal

    from celery.exceptions import SoftTimeLimitExceeded

    def soft_limit(signum, frame):
        raise SoftTimeLimitExceeded()

    previous = signal.signal(signal.SIGALRM, soft_limit)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    return lambda: (signal.setitimer(signal.ITIMER_REAL, 0), signal.signal(signal.SIGALRM, previous))


def test_after_the_soft_limit_the_hypervisor_calls_cleanup_runs():
    """vSphere closes its session and client in finally / async-with exits; abandoning
    the coroutine skipped them (leaked sessions)."""
    import time

    from celery.exceptions import SoftTimeLimitExceeded
    from worker.fencing import run_async

    cleaned = []

    async def call():
        try:
            await asyncio.to_thread(time.sleep, 3)
        finally:
            cleaned.append("finally")

    restore = _soft_limit_after(0.3)
    started = time.monotonic()
    try:
        with pytest.raises(SoftTimeLimitExceeded):
            run_async(call())
    finally:
        restore()
    assert cleaned == ["finally"]
    assert time.monotonic() - started < 1.5, "it waited for the hypervisor thread"


def test_after_the_soft_limit_the_range_stays_leased_while_the_call_may_still_run(db, requeued):
    """The hypervisor call can outlive the task in a thread; releasing the lease let a new
    destroy run alongside it. The lease is kept (until it expires) instead."""
    import time

    if not hasattr(tables, "range_leases"):
        pytest.skip("no lease table before the fix")

    class StuckBackend:
        async def destroy(self, range_id, output):
            await asyncio.to_thread(time.sleep, 2)

    from celery.exceptions import SoftTimeLimitExceeded

    rid = _range(db, "destroying", OUTPUT)
    restore = _soft_limit_after(0.3)
    try:
        with patch.object(tasks, "_get_backend", return_value=StuckBackend()), pytest.raises(SoftTimeLimitExceeded):
            tasks.destroy_range.run(rid)
    finally:
        restore()
    with db.begin() as conn:
        held = conn.execute(sa.text("SELECT count(*) FROM range_leases WHERE range_id = :i"), {"i": rid}).scalar()
        assert held == 1, "the lease was released while the destroy may still be running"
        conn.execute(sa.text("UPDATE ranges SET state = 'destroying' WHERE id = :i"), {"i": rid})  # the user retries
    backend = SlowBackend()
    with patch.object(tasks, "_get_backend", return_value=backend):
        assert tasks.destroy_range.run(rid)["status"] == "deferred"
    assert backend.calls == 0


def test_stop_after_the_soft_limit_keeps_the_lease_too(db, requeued):
    if not hasattr(tables, "range_leases"):
        pytest.skip("no lease table before the fix")
    from celery.exceptions import SoftTimeLimitExceeded

    rid = _range(db, "stopping", OUTPUT)
    with patch.object(tasks, "_get_backend", side_effect=SoftTimeLimitExceeded()), pytest.raises(SoftTimeLimitExceeded):
        tasks.stop_range.run(rid)
    with db.connect() as conn:
        assert conn.execute(sa.text("SELECT state FROM ranges WHERE id = :i"), {"i": rid}).scalar() == "failed"
        assert conn.execute(sa.text("SELECT count(*) FROM range_leases")).scalar() == 1


def test_a_restore_takes_the_range_lease(db, requeued):
    """Two restores of one range (a stale-looking one re-sent) must not overlap."""
    if not hasattr(tables, "range_leases"):
        pytest.skip("no lease table before the fix")
    rid = _range(db, "ready", OUTPUT)
    with db.begin() as conn:
        conn.execute(
            tables.range_leases.insert().values(
                range_id=rid, holder="other", expires_at=datetime.now(UTC) + timedelta(seconds=60)
            )
        )
    with patch.object(tasks, "_get_backend") as backend:
        result = tasks.restore_snapshot.run(rid, str(uuid.uuid4()))
    backend.assert_not_called()
    assert result["status"] == "deferred"
    assert requeued and requeued[0]["task"] == "worker.tasks.restore_snapshot"


# ── From the fifth re-review ───────────────────────────────────────────
@pytest.mark.parametrize("task", ["snapshot_range", "delete_snapshot"])
def test_snapshot_tasks_wait_for_the_range_lease_too(db, requeued, task):
    """A snapshot taken or deleted while a restore's thread may still be reverting the
    same VMs (or while a destroy runs) acted alongside it: both take the range's lease."""
    if not hasattr(tables, "range_leases"):
        pytest.skip("no lease table before the fix")
    rid = _range(db, "ready", OUTPUT)
    with db.begin() as conn:
        conn.execute(
            tables.range_leases.insert().values(
                range_id=rid, holder="other", expires_at=datetime.now(UTC) + timedelta(seconds=60)
            )
        )
    with patch.object(tasks, "_get_backend") as backend:
        result = getattr(tasks, task).run(rid, str(uuid.uuid4()))
    backend.assert_not_called()
    assert result["status"] == "deferred" and requeued[0]["task"] == f"worker.tasks.{task}"
