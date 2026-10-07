"""The range lease is renewed while its task runs, expires soon after its worker dies,
and an execution that lost it does nothing more (worker/fencing.py).

Found by rerunning the s7 interruption exercise: a worker killed mid-provision left its
range's lease for LEASE_SECONDS (then an hour), and nothing could release it. After the
operator abandoned the operation every new provision was refused with 409 "a worker is
still acting on this range" until it expired.

These run the real tasks against SQLite with the hypervisor stubbed, and the lease
shortened to about a second.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

os.environ.setdefault("MOCK_PROVISION_DELAY", "0")
os.environ.setdefault("MOCK_FAILURE_RATE", "0")

import sqlalchemy as sa  # noqa: E402
from worker import fencing, tasks  # noqa: E402
from worker.provisioners.results import DestroyResult, ProvisionResult  # noqa: E402


@pytest.fixture(autouse=True)
def _short_lease(monkeypatch, requeued):
    """A one-second lease renewed every 0.1 s; re-queues recorded, not sent."""
    monkeypatch.setattr(fencing, "LEASE_SECONDS", 1)
    monkeypatch.setattr(fencing, "LEASE_HEARTBEAT_SECONDS", 0.1)


def _range(db, state: str) -> str:
    rid, tid = str(uuid.uuid4()), str(uuid.uuid4())
    with db.begin() as conn:
        conn.execute(sa.text("INSERT INTO templates VALUES (:id, 'id: t\n')"), {"id": tid})
        conn.execute(
            sa.text("INSERT INTO ranges (id, template_id, state, provisioner_backend) VALUES (:id, :t, :s, 'mock')"),
            {"id": rid, "t": tid, "s": state},
        )
    return rid


def _state(db, rid: str) -> tuple:
    with db.connect() as conn:
        q = sa.text("SELECT state, error_message, provisioner_output FROM ranges WHERE id = :id")
        return tuple(conn.execute(q, {"id": rid}).one())


def _set_state(db, rid: str, state: str, error: str | None = None) -> None:
    with db.begin() as conn:
        conn.execute(sa.text("UPDATE ranges SET state = :s, error_message = :e WHERE id = :i"),
                     {"s": state, "e": error, "i": rid})


def _lease(db, rid: str):
    with db.connect() as conn:
        return conn.execute(
            sa.select(fencing.range_leases.c.holder, fencing.range_leases.c.expires_at).where(
                fencing.range_leases.c.range_id == rid
            )
        ).first()


def _abandon(db, rid: str, action: str = "provision") -> int:
    """What app/range_ops/service.py's abandon does to the worker's tables: the range to
    failed, and the lease of the operation's action deleted."""
    _set_state(db, rid, "failed", f"{action} abandoned by an operator")
    with db.begin() as conn:
        return conn.execute(
            sa.delete(fencing.range_leases).where(
                fencing.range_leases.c.range_id == rid, fencing.range_leases.c.holder.like(f"{action}:%")
            )
        ).rowcount


def _expiry(value) -> datetime:
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class SlowBackend:
    """A hypervisor call that blocks until released (or cancelled), counting calls."""

    def __init__(self):
        self.calls = 0
        self.destroys = 0
        self.cancelled = False
        self.started = threading.Event()
        self.release = threading.Event()

    async def provision(self, range_id, template, allocations):
        self.calls += 1
        self.started.set()
        try:
            await asyncio.to_thread(self.release.wait, 10)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return ProvisionResult(status="ok", vms=[{"name": "x", "vm_id": "vm-9"}])

    async def destroy(self, range_id, output):
        self.destroys += 1
        return DestroyResult(status="ok", resources_removed=1)


def _run_in_thread(fn, results: dict, key: str) -> threading.Thread:
    t = threading.Thread(target=lambda: results.update({key: fn()}))
    t.start()
    return t


# ── the heartbeat ───────────────────────────────────────────────────────


def test_a_long_running_task_keeps_its_lease_past_the_lease_length(db):
    """A live task must never lose its lease, however long its hypervisor call takes."""
    rid = _range(db, "provisioning")
    backend = SlowBackend()
    results: dict = {}
    with patch.object(tasks, "_get_backend", return_value=backend):
        build = _run_in_thread(lambda: tasks.provision_range.run(rid), results, "build")
        assert backend.started.wait(10)
        first_expiry = _expiry(_lease(db, rid)[1])
        time.sleep(2.5)  # two and a half lease lengths
        holder, expires = _lease(db, rid)
        assert holder.startswith("provision:")
        assert _expiry(expires) > datetime.now(UTC), "the heartbeat let a running task's lease expire"
        assert _expiry(expires) > first_expiry, "the lease was not renewed"
        assert tasks.provision_range.run(rid)["status"] == "deferred", "a second copy took over a live task"
        backend.release.set()
        build.join(20)
    assert results["build"]["status"] == "ready" and backend.calls == 1
    assert _lease(db, rid) is None, "the lease is given back when the task ends"


def test_the_heartbeat_stops_when_the_task_ends(db):
    rid = _range(db, "provisioning")
    before = {t.name for t in threading.enumerate()}
    assert tasks.provision_range.run(rid)["status"] == "ready"
    time.sleep(0.3)
    assert f"lease-{rid}" not in {t.name for t in threading.enumerate()} - before


def test_a_dead_workers_lease_expires_and_a_new_build_proceeds(db):
    """The worker was killed: nothing renews its lease. Within LEASE_SECONDS a copy (or a
    new operation's task) takes over."""
    rid = _range(db, "provisioning")
    with db.begin() as conn:  # what a killed worker leaves: a fresh lease, never renewed
        conn.execute(
            fencing.range_leases.insert().values(
                range_id=rid, holder="provision:dead", expires_at=datetime.now(UTC) + timedelta(seconds=1)
            )
        )
    assert tasks.provision_range.run(rid)["status"] == "deferred"
    time.sleep(1.2)
    assert tasks.provision_range.run(rid)["status"] == "ready"


def test_an_expired_lease_is_not_revived_by_its_holder(db):
    rid = _range(db, "provisioning")
    with db.begin() as conn:
        conn.execute(
            fencing.range_leases.insert().values(
                range_id=rid, holder="provision:slow", expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
    with tasks._db_session() as s:
        assert fencing._extend_lease(s, rid, "provision:slow") is False


def test_after_the_soft_limit_the_kept_lease_lasts_long_and_is_not_renewed(db):
    rid = _range(db, "provisioning")
    holder = fencing.claim(tasks._db_session, tasks._in_state, rid, "provisioning", "provision")
    fencing.keep(tasks._db_session, rid, holder)
    expires = _expiry(_lease(db, rid)[1])
    assert expires > datetime.now(UTC) + timedelta(seconds=fencing.KEPT_LEASE_SECONDS - 60)


# ── abandoned: the stale execution is fenced out ────────────────────────


def test_a_lease_released_during_the_hypervisor_call_cancels_it_and_records_nothing(db):
    """The operator abandoned the build (its lease deleted) while the worker was in fact
    alive: its heartbeat sees the lease gone and cancels the call; nothing is written."""
    rid = _range(db, "provisioning")
    backend = SlowBackend()
    results: dict = {}
    with patch.object(tasks, "_get_backend", return_value=backend):
        build = _run_in_thread(lambda: tasks.provision_range.run(rid), results, "build")
        assert backend.started.wait(10)
        assert _abandon(db, rid) == 1
        _set_state(db, rid, "provisioning")  # and a new build was accepted at once
        build.join(10)
        assert not build.is_alive(), "the stale task kept running after losing its lease"
    assert results["build"]["status"] == "lease_lost" and backend.cancelled
    assert _state(db, rid) == ("provisioning", None, None), "the stale task wrote over the new operation's range"
    backend.release.set()
    # The new operation's task is not held up by the stale one: it claims and builds.
    assert tasks.provision_range.run(rid)["status"] == "ready"


def test_a_lease_lost_after_the_build_records_nothing_and_does_not_retry(db, requeued):
    """The abandon lands between the hypervisor call's end and the state write: the
    write is refused in the same statement, the result is not recorded, the error path
    (write failed, retry) does not run, and nothing is torn down."""
    rid = _range(db, "provisioning")

    class AbandonedDuringBuild(SlowBackend):
        async def provision(self, range_id, template, allocations):
            self.calls += 1
            _abandon(db, range_id)
            return ProvisionResult(status="ok", vms=[{"name": "x", "vm_id": "vm-9"}])

    backend = AbandonedDuringBuild()
    with (
        patch.object(fencing, "LEASE_HEARTBEAT_SECONDS", 60),  # the heartbeat does not notice first
        patch.object(tasks, "_get_backend", return_value=backend),
        patch.object(tasks, "_last_attempt", return_value=False),
    ):
        assert tasks.provision_range.run(rid)["status"] == "lease_lost"
    state, error, output = _state(db, rid)
    assert state == "failed" and error == "provision abandoned by an operator" and output is None
    assert backend.destroys == 0, "a fenced-out build must not tear down VMs the operator now owns"
    assert requeued == [], "a fenced-out task is not retried"


def test_a_lease_lost_before_the_hypervisor_call_makes_no_call(db):
    rid = _range(db, "provisioning")
    backend = SlowBackend()

    def abandoned_then_backend(*a, **k):
        _abandon(db, rid)
        _set_state(db, rid, "provisioning")  # a new build accepted: the state alone would let it through
        return backend

    with (
        patch.object(fencing, "LEASE_HEARTBEAT_SECONDS", 60),
        patch.object(tasks, "_get_backend", side_effect=abandoned_then_backend),
    ):
        assert tasks.provision_range.run(rid)["status"] == "lease_lost"
    assert backend.calls == 0, "the hypervisor was called by an execution that no longer held the lease"
    assert _state(db, rid)[0] == "provisioning"


def test_a_lease_taken_over_after_expiry_fences_the_old_execution(db):
    """The heartbeat could not reach the database for a whole lease (the worker was
    partitioned): another execution took the expired lease. The old one, coming back,
    finds it gone and writes nothing."""
    rid = _range(db, "provisioning")

    class TakenOver(SlowBackend):
        async def provision(self, range_id, template, allocations):
            with db.begin() as conn:
                conn.execute(
                    sa.update(fencing.range_leases)
                    .where(fencing.range_leases.c.range_id == range_id)
                    .values(holder="provision:new", expires_at=datetime.now(UTC) + timedelta(seconds=60))
                )
            return ProvisionResult(status="ok", vms=[{"name": "x", "vm_id": "vm-9"}])

    with (
        patch.object(fencing, "LEASE_HEARTBEAT_SECONDS", 60),
        patch.object(tasks, "_get_backend", return_value=TakenOver()),
    ):
        assert tasks.provision_range.run(rid)["status"] == "lease_lost"
    assert _state(db, rid)[0] == "provisioning"
    assert _lease(db, rid)[0] == "provision:new", "the old execution released the new holder's lease"


def test_the_holder_names_the_action_an_abandon_releases_by(db):
    rid = _range(db, "destroying")
    holder = fencing.claim(tasks._db_session, tasks._in_state, rid, "destroying", "destroy")
    assert holder.startswith("destroy:") and len(holder) <= 64  # range_leases.holder is String(64)
    assert _abandon(db, rid, "provision") == 0, "a provision's abandon released a destroy's lease"
    assert _abandon(db, rid, "destroy") == 1


def test_on_postgres_an_abandoned_build_is_fenced_out_and_renewal_works(postgres_engine, monkeypatch):
    """The same on the migrated schema: the heartbeat's UPDATE and the guarded state
    write's EXISTS against timestamptz, and the API's own abandon deleting the lease."""
    from contextlib import contextmanager

    from app import models as m
    from app.range_leases import RangeLease
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
        build = _run_in_thread(lambda: tasks.provision_range.run(rid), results, "build")
        assert backend.started.wait(10)
        time.sleep(1.5)  # past the first lease: alive only if renewed
        with factory() as s:
            lease = s.get(RangeLease, rng.id)
            assert lease is not None and lease.expires_at > datetime.now(UTC)
            from app.range_ops.service import release_lease

            assert release_lease(s, rng.id, "provision")  # the abandon's release
            s.get(m.Range, rng.id).state = m.RangeState.failed
            s.commit()
        build.join(10)
    backend.release.set()
    assert results["build"]["status"] == "lease_lost" and backend.cancelled
    with factory() as s:
        assert s.get(m.Range, rng.id).state == m.RangeState.failed
        assert s.get(m.Range, rng.id).provisioner_output is None


def test_outside_a_fenced_task_state_writes_are_unguarded(db):
    """The claim's own state check, and untasked callers, hold no lease."""
    rid = _range(db, "ready")
    assert tasks._update_range_state(rid, "failed", only_from=("ready",)) == 1
