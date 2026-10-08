"""The range lease is renewed while its task runs, expires soon after its worker dies,
and an abandon fences a still-running execution out without handing the range to a new
operation under its in-flight work (worker/fencing.py).

Found by rerunning the s7 interruption exercise: a worker killed mid-provision left its
range's lease for LEASE_SECONDS (then an hour), and nothing could release it. After the
operator abandoned the operation every new provision was refused with 409 "a worker is
still acting on this range" until it expired. A first fix deleted the lease on abandon;
review of #95 found that let a new build start beside a live worker's vCenter work, so
an abandon now leaves a short tombstone instead.

These run the real tasks against SQLite (and PostgreSQL where marked) with the
hypervisor stubbed, and the lease shortened to about a second.
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
    failed, and the lease of the operation's action renamed to a tombstone expiring one
    lease from now."""
    _set_state(db, rid, "failed", f"{action} abandoned by an operator")
    leases = fencing.range_leases
    with db.begin() as conn:
        return conn.execute(
            sa.update(leases)
            .where(leases.c.range_id == rid, leases.c.holder.like(f"{action}:%"))
            .values(
                holder=sa.literal(fencing.ABANDONED).concat(leases.c.holder),
                expires_at=datetime.now(UTC) + timedelta(seconds=fencing.LEASE_SECONDS),
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


def test_a_database_outage_longer_than_the_lease_does_not_cost_a_healthy_build_its_range(db):
    """The heartbeat could not renew for longer than a lease (the database was down), but
    nobody took the lease: when the database is back the task re-takes its own lease and
    finishes, instead of giving up a healthy build with the range stuck in provisioning."""
    rid = _range(db, "provisioning")
    backend = SlowBackend()
    results: dict = {}
    with patch.object(tasks, "_get_backend", return_value=backend):
        build = _run_in_thread(lambda: tasks.provision_range.run(rid), results, "build")
        assert backend.started.wait(10)
        with db.begin() as conn:  # what the outage left: the lease long expired
            conn.execute(sa.update(fencing.range_leases).where(fencing.range_leases.c.range_id == rid)
                         .values(expires_at=datetime.now(UTC) - timedelta(seconds=30)))
        time.sleep(0.5)
        assert _expiry(_lease(db, rid)[1]) > datetime.now(UTC), "the heartbeat did not re-take its own lease"
        backend.release.set()
        build.join(20)
    assert results["build"]["status"] == "ready"


def test_an_expired_lease_another_execution_took_is_not_renewed(db):
    rid = _range(db, "provisioning")
    with db.begin() as conn:
        conn.execute(
            fencing.range_leases.insert().values(
                range_id=rid, holder="provision:new", expires_at=datetime.now(UTC) + timedelta(seconds=60)
            )
        )
    with tasks._db_session() as s:
        assert fencing._extend_lease(s, rid, "provision:slow") is False
        assert fencing._extend_lease(s, rid, "provision:new") is True


def test_after_the_soft_limit_the_kept_lease_lasts_long_and_a_renewal_never_shortens_it(db):
    rid = _range(db, "provisioning")
    holder = fencing.claim(tasks._db_session, tasks._in_state, rid, "provisioning", "provision")
    fencing.keep(tasks._db_session, rid, holder)
    with tasks._db_session() as s:  # a heartbeat that was in flight when the task stopped
        assert fencing._extend_lease(s, rid, holder)
    expires = _expiry(_lease(db, rid)[1])
    assert expires > datetime.now(UTC) + timedelta(seconds=fencing.KEPT_LEASE_SECONDS - 60)


# ── abandoned: the stale execution is fenced out ────────────────────────


def test_an_abandon_during_the_build_keeps_the_range_blocked_until_the_work_ends(db, requeued):
    """The operator abandoned the build while the worker was in fact alive. Its call is
    not cancelled (that stops neither its threads nor vCenter's tasks); its heartbeat keeps
    the tombstone past the tombstone's own life, so no new operation's task can take the
    range under it. When the call ends the build tears down what it built, writes
    nothing, is not retried, and only then gives the range back."""
    rid = _range(db, "provisioning")
    backend = SlowBackend()
    results: dict = {}
    with patch.object(tasks, "_get_backend", return_value=backend):
        build = _run_in_thread(lambda: tasks.provision_range.run(rid), results, "build")
        assert backend.started.wait(10)
        assert _abandon(db, rid) == 1
        time.sleep(2.5)  # past the tombstone's own expiry: alive only if the stale worker renews it
        holder, expires = _lease(db, rid)
        assert holder.startswith("abandoned:provision:") and _expiry(expires) > datetime.now(UTC)
        assert fencing.claim(tasks._db_session, tasks._in_state, rid, None, "provision") == fencing.LEASE_HELD, (
            "a new operation's task took the range while the abandoned build was still running"
        )
        assert not backend.cancelled
        backend.release.set()
        build.join(20)
    assert results["build"]["status"] == "discarded" and backend.destroys == 1
    assert _state(db, rid) == ("failed", "provision abandoned by an operator", None)
    assert _lease(db, rid) is None, "the tombstone is deleted once the work has ended"
    assert requeued == []
    _set_state(db, rid, "provisioning")  # the next operation, accepted now
    assert tasks.provision_range.run(rid)["status"] == "ready"


def test_an_abandon_after_the_build_discards_it_under_the_tombstone(db, requeued):
    """The abandon lands between the hypervisor call's end and the state write: the
    write is refused (range and lease rows locked), the result is not recorded, and what
    was built is torn down while the range is still blocked."""
    rid = _range(db, "provisioning")
    blocked_during_teardown = []

    class AbandonedDuringBuild(SlowBackend):
        async def provision(self, range_id, template, allocations):
            self.calls += 1
            _abandon(db, range_id)
            return ProvisionResult(status="ok", vms=[{"name": "x", "vm_id": "vm-9"}])

        async def destroy(self, range_id, output):
            blocked_during_teardown.append(_lease(db, range_id)[0].startswith("abandoned:"))
            return await super().destroy(range_id, output)

    backend = AbandonedDuringBuild()
    with (
        patch.object(fencing, "LEASE_HEARTBEAT_SECONDS", 60),  # the heartbeat does not notice first
        patch.object(tasks, "_get_backend", return_value=backend),
    ):
        assert tasks.provision_range.run(rid)["status"] == "discarded"
    assert _state(db, rid) == ("failed", "provision abandoned by an operator", None)
    assert blocked_during_teardown == [True]
    assert _lease(db, rid) is None and requeued == []


def test_after_an_abandon_no_hypervisor_call_is_started(db):
    rid = _range(db, "provisioning")
    backend = SlowBackend()

    def abandoned_then_backend(*a, **k):
        _abandon(db, rid)
        return backend

    with (
        patch.object(fencing, "LEASE_HEARTBEAT_SECONDS", 60),
        patch.object(tasks, "_get_backend", side_effect=abandoned_then_backend),
    ):
        assert tasks.provision_range.run(rid)["status"] == "abandoned"
    assert backend.calls == 0, "the hypervisor was called by an execution whose operation was abandoned"
    assert _state(db, rid)[0] == "failed" and _lease(db, rid) is None


def test_a_failure_after_an_abandon_waits_for_its_threads_writes_nothing_and_is_not_retried(db, requeued):
    """The call fails after the abandon but left a thread running (a clone): the range is
    not handed back until that thread is done; the abandon's message is not overwritten."""
    rid = _range(db, "provisioning")
    straggler_done = threading.Event()

    class FailsLeavingAThread(SlowBackend):
        async def provision(self, range_id, template, allocations):
            asyncio.get_running_loop().run_in_executor(None, lambda: (time.sleep(1), straggler_done.set()))
            _abandon(db, range_id)
            raise RuntimeError("vCenter refused the clone")

    with (
        patch.object(fencing, "LEASE_HEARTBEAT_SECONDS", 60),
        patch.object(tasks, "_get_backend", return_value=FailsLeavingAThread()),
    ):
        result = tasks.provision_range.run(rid)  # called directly: the last attempt, which writes failed
    assert result["status"] == "abandoned"
    assert straggler_done.is_set(), "the range was handed back while the call's thread was still running"
    assert _state(db, rid)[:2] == ("failed", "provision abandoned by an operator")
    assert _lease(db, rid) is None and requeued == []


def test_an_abandoned_dead_workers_tombstone_expires_and_frees_the_range(db):
    rid = _range(db, "provisioning")
    with db.begin() as conn:  # a killed worker's lease, recently renewed
        conn.execute(
            fencing.range_leases.insert().values(
                range_id=rid, holder="provision:dead", expires_at=datetime.now(UTC) + timedelta(seconds=60)
            )
        )
    assert _abandon(db, rid) == 1  # the tombstone lasts one lease, not the lease's remaining minute
    _set_state(db, rid, "provisioning")  # the next operation
    assert tasks.provision_range.run(rid)["status"] == "deferred"
    time.sleep(1.2)
    assert tasks.provision_range.run(rid)["status"] == "ready"


class _BuiltWithEverything(SlowBackend):
    """A build that made VMs, port groups, a mirror session and an uplink, abandoned
    before it could record them; its teardown answers ``teardown``."""

    def __init__(self, db, teardown: str = "ok"):
        super().__init__()
        self.db, self.teardown, self.torn_down = db, teardown, []

    async def provision(self, range_id, template, allocations):
        self.calls += 1
        _abandon(self.db, range_id)
        return ProvisionResult(
            status="ok", vms=[{"name": "r-dc01", "vm_id": "vm-1"}],
            networks=[{"vlan_id": 10, "portgroup": "tn-r-10"}],
            uplink={"vm": "r-fw01", "ip": "203.0.113.9"}, mirrors=[{"src": "r-dc01", "dst": "r-sensor"}],
        )

    async def destroy(self, range_id, output):
        self.torn_down.append(output)
        return DestroyResult(status=self.teardown, errors=[] if self.teardown == "ok" else ["vm-1: host unreachable"])


def test_a_discarded_build_tears_down_its_mirrors_and_uplink_too(db):
    rid = _range(db, "provisioning")
    backend = _BuiltWithEverything(db)
    with patch.object(fencing, "LEASE_HEARTBEAT_SECONDS", 60), patch.object(tasks, "_get_backend", return_value=backend):
        assert tasks.provision_range.run(rid)["status"] == "discarded"
    (output,) = backend.torn_down
    assert output["mirrors"] and output["uplink"] and output["networks"] and output["vms"], output


def test_a_discard_that_fails_records_the_leftover_so_no_build_goes_over_it(db):
    """The teardown of an abandoned build failed: its VMs are still on vCenter under the
    range's names. They are recorded on the range (the API's "still has N VMs; destroy it
    first" then refuses a provision) before the range is handed back."""
    import json

    rid = _range(db, "provisioning")
    backend = _BuiltWithEverything(db, teardown="partial")
    with patch.object(fencing, "LEASE_HEARTBEAT_SECONDS", 60), patch.object(tasks, "_get_backend", return_value=backend):
        result = tasks.provision_range.run(rid)
    assert result["status"] == "discard_failed" and "leftover" not in result
    state, error, output = _state(db, rid)
    recorded = json.loads(output)
    assert state == "failed" and error == "provision abandoned by an operator", "the range's state was touched"
    assert [v["vm_id"] for v in recorded["vms"]] == ["vm-1"] and recorded["mirrors"] and recorded["uplink"]
    assert "host unreachable" in recorded["warnings"][0]
    assert _lease(db, rid) is None


def test_a_discard_that_raises_is_recorded_too(db):
    import json

    rid = _range(db, "provisioning")
    backend = _BuiltWithEverything(db)

    async def boom(range_id, output):
        raise RuntimeError("vCenter session expired")

    backend.destroy = boom
    with patch.object(fencing, "LEASE_HEARTBEAT_SECONDS", 60), patch.object(tasks, "_get_backend", return_value=backend):
        assert tasks.provision_range.run(rid)["status"] == "discard_failed"
    assert json.loads(_state(db, rid)[2])["vms"][0]["vm_id"] == "vm-1"


def test_an_abandoned_destroy_does_not_announce_a_destroyed_range(db):
    """The destroy finished on the hypervisor after its operation was abandoned: the
    write of ``destroyed`` is refused, so neither the API's channel nor Greyspace may be
    told the range is destroyed."""
    rid = _range(db, "destroying")
    with db.begin() as conn:
        conn.execute(sa.text("UPDATE ranges SET provisioner_output = :o WHERE id = :i"),
                     {"o": '{"vms": [{"name": "r-dc01", "vm_id": "vm-1"}]}', "i": rid})
    notified: list = []

    class AbandonedDuringTeardown(SlowBackend):
        async def destroy(self, range_id, output):
            _abandon(db, range_id, "destroy")
            return DestroyResult(status="ok", resources_removed=1)

    with (
        patch.object(fencing, "LEASE_HEARTBEAT_SECONDS", 60),
        patch.object(tasks, "_get_backend", return_value=AbandonedDuringTeardown()),
        patch.object(tasks, "_notify_api", lambda channel, msg: notified.append(msg)),
        patch.object(tasks.greyspace, "after_destroy") as greyspace_after,
    ):
        assert tasks.destroy_range.run(rid)["status"] == "destroyed_unrecorded"
    assert not any(m.get("state") == "destroyed" for m in notified), notified
    greyspace_after.assert_not_called()
    assert _state(db, rid)[0] == "failed"


def test_a_restore_fenced_out_gives_its_snapshot_back(db):
    """restore_snapshot's on_lost: a fenced-out restore must not leave its snapshot in
    ``restoring``, which blocks further restores and its deletion."""
    rid = _range(db, "ready")
    with db.begin() as conn:
        conn.execute(sa.text("UPDATE ranges SET provisioner_output = :o WHERE id = :i"),
                     {"o": '{"vms": [{"name": "r-dc01", "vm_id": "vm-1"}]}', "i": rid})
        conn.execute(
            sa.text(
                "INSERT INTO range_snapshots (id, range_id, snapshot_state, snapshot_data, range_state_at_snapshot) "
                "VALUES ('s1', :r, 'restoring', '{\"snapshot_name\": \"tnx\"}', 'ready')"
            ),
            {"r": rid},
        )

    class TakenOverDuringRestore:
        async def restore(self, range_id, output, name, power_on=True):
            with db.begin() as conn:
                conn.execute(sa.update(fencing.range_leases).where(fencing.range_leases.c.range_id == range_id)
                             .values(holder="restore:other", expires_at=datetime.now(UTC) + timedelta(seconds=60)))
            from worker.provisioners.results import RestoreResult

            return RestoreResult(status="ok", vms_reverted=1)

    with (
        patch.object(fencing, "LEASE_HEARTBEAT_SECONDS", 60),
        patch.object(tasks, "_get_backend", return_value=TakenOverDuringRestore()),
    ):
        assert tasks.restore_snapshot.run(rid, "s1")["status"] == "lease_lost"
    with db.connect() as conn:
        snap = conn.execute(sa.text("SELECT snapshot_state FROM range_snapshots WHERE id = 's1'")).scalar()
    assert snap == "ready"


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

    backend = TakenOver()
    with (
        patch.object(fencing, "LEASE_HEARTBEAT_SECONDS", 60),
        patch.object(tasks, "_get_backend", return_value=backend),
    ):
        assert tasks.provision_range.run(rid)["status"] == "lease_lost"
    assert _state(db, rid)[0] == "provisioning"
    assert _lease(db, rid)[0] == "provision:new", "the old execution released the new holder's lease"
    assert backend.destroys == 0, "port groups are named by range: a teardown now could hit the new holder's build"


def test_the_holder_names_the_action_an_abandon_releases_by(db):
    rid = _range(db, "destroying")
    holder = fencing.claim(tasks._db_session, tasks._in_state, rid, "destroying", "destroy")
    assert holder.startswith("destroy:") and len(holder) <= 64  # range_leases.holder is String(64)
    assert _abandon(db, rid, "provision") == 0, "a provision's abandon released a destroy's lease"
    assert _abandon(db, rid, "destroy") == 1


def _pg(postgres_engine, monkeypatch):
    """A session factory on the migrated PostgreSQL schema, the tasks pointed at it, and
    one range in ``provisioning``."""
    from contextlib import contextmanager

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
    monkeypatch.setattr(tasks, "_db_session", session)
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    return factory, rng.id


def test_on_postgres_an_abandoned_build_is_fenced_and_keeps_the_range_until_done(postgres_engine, monkeypatch):
    """On the migrated schema, with the API's own fence_lease: renewal in database time,
    the tombstone renewed by the live stale worker, the API refusing a new provision
    meanwhile, and the range handed back once the build has torn down what it built."""
    from app import models as m
    from app.range_leases import RangeLease
    from app.range_ops import service as ops

    factory, range_uuid = _pg(postgres_engine, monkeypatch)
    monkeypatch.setenv("RANGE_LEASE_SECONDS", "1")  # the API's tombstone: alive after 2.5 s only if renewed
    rid = str(range_uuid)
    backend = SlowBackend()
    results: dict = {}
    with patch.object(tasks, "_get_backend", return_value=backend):
        build = _run_in_thread(lambda: tasks.provision_range.run(rid), results, "build")
        assert backend.started.wait(10)
        time.sleep(1.5)  # past the first lease: alive only if renewed
        with factory() as s:
            assert ops.worker_acting(s, range_uuid), "the running build's lease expired"
            assert ops.fence_lease(s, range_uuid, "provision")  # the abandon
            s.get(m.Range, range_uuid).state = m.RangeState.failed
            s.commit()
        time.sleep(2.5)  # past the tombstone's own life
        with factory() as s:
            holder = ops.worker_acting(s, range_uuid)
            assert holder and holder.startswith("abandoned:"), "a new provision would be accepted beside the build"
        backend.release.set()
        build.join(20)
    assert results["build"]["status"] == "discarded" and backend.destroys == 1
    with factory() as s:
        assert s.get(m.Range, range_uuid).state == m.RangeState.failed
        assert s.get(m.Range, range_uuid).provisioner_output is None
        assert s.get(RangeLease, range_uuid) is None


@pytest.mark.parametrize("racer", ["tombstone_only", "abandon", "takeover"])
def test_on_postgres_the_guarded_write_waits_for_a_racing_abandon_or_takeover(postgres_engine, monkeypatch, racer):
    """The check and the write cannot be split: a rename of the lease (a tombstone, or a
    takeover) that commits while the write waits on its lock is seen by the write, which
    then records nothing.

    ``tombstone_only`` renames the lease and leaves the range's state alone, so only the
    lease row's lock (and the holder read after it) can refuse the write: the state check
    would let it through. ``abandon`` is the API's whole abandon (range row, state, then
    the lease); ``takeover`` another execution's claim of an expired lease."""
    from app import models as m
    from app.range_leases import RangeLease

    factory, range_uuid = _pg(postgres_engine, monkeypatch)
    rid = str(range_uuid)
    holder = "provision:old"
    with factory() as s:
        s.add(RangeLease(range_id=range_uuid, holder=holder, expires_at=datetime.now(UTC) + timedelta(seconds=60)))
        s.commit()
    racing = factory()
    if racer == "abandon":  # the API: range row first, then the lease
        racing.query(m.Range).filter(m.Range.id == range_uuid).with_for_update().one().state = m.RangeState.failed
        racing.flush()
    if racer in ("abandon", "tombstone_only"):
        racing.query(RangeLease).filter(RangeLease.range_id == range_uuid).update(
            {RangeLease.holder: fencing.tombstone(holder)}, synchronize_session=False
        )
    else:  # another execution's claim of a lease it found expired: the lease row only
        racing.query(RangeLease).filter(RangeLease.range_id == range_uuid).update(
            {RangeLease.holder: "provision:new"}, synchronize_session=False
        )
    racing.flush()
    outcome: dict = {}

    def write():
        token = fencing._current.set(fencing._Lease(tasks._db_session, rid, holder))
        try:
            outcome["rows"] = tasks._update_range_state(rid, "ready", output='{"vms": []}', only_from=("provisioning",))
        except fencing.LeaseLost:
            outcome["rows"] = "lease_lost"
        finally:
            fencing._current.reset(token)

    writer = threading.Thread(target=write)
    writer.start()
    time.sleep(0.5)
    assert writer.is_alive(), "the guarded write did not wait for the racing transaction's lock"
    racing.commit()
    racing.close()
    writer.join(10)
    assert outcome["rows"] == ("lease_lost" if racer == "takeover" else 0)
    with factory() as s:
        rng = s.get(m.Range, range_uuid)
        assert rng.provisioner_output is None and rng.state != m.RangeState.ready
        if racer == "tombstone_only":
            assert rng.state == m.RangeState.provisioning, "the state was meant to let the write through"


def test_outside_a_fenced_task_state_writes_are_unguarded(db):
    """The claim's own state check, and untasked callers, hold no lease."""
    rid = _range(db, "ready")
    assert tasks._update_range_state(rid, "failed", only_from=("ready",)) == 1
