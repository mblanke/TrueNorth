"""Reserving a build's VLANs and uplink address on network_reservations (worker/range_alloc.py),
and the tasks.py hooks around it.

The tables are the API's own (``Base.metadata``, with S4a's unique constraints), on SQLite.
The PostgreSQL advisory lock that serialises concurrent builds is not exercised here
(SQLite serialises writers itself); ``test_postgres_takes_the_api_lock`` checks the
statement it would run.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import app.network_inventory  # noqa: F401 — registers network_reservations
import app.range_leases  # noqa: F401
import pytest
import sqlalchemy as sa
from app.db import Base
from sqlalchemy import StaticPool
from sqlalchemy.orm import Session, sessionmaker

tasks = pytest.importorskip("worker.tasks")
from worker import db_ops  # noqa: E402
from worker import range_alloc as ra  # noqa: E402
from worker.provisioners.base import AllocationNeed  # noqa: E402
from worker.provisioners.results import DestroyResult, ProvisionResult  # noqa: E402
from worker.tables import network_reservations, ranges  # noqa: E402

TENANT_A, TENANT_B = uuid.uuid4(), uuid.uuid4()
POOL = [str(v) for v in range(100, 106)]
DOMAIN = "vsphere:vlans"


class DB:
    def __init__(self):
        self.engine = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, class_=Session)
        self.opened = 0

    @contextmanager
    def session(self):  # tasks._db_session: commit on success, roll back on an exception
        self.opened += 1
        s = self.factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def add_range(self, state="provisioning", tenant=TENANT_A, output=None) -> str:
        rid = uuid.uuid4()
        with self.session() as s:
            s.execute(sa.insert(ranges).values(
                id=rid, name="r", template_id=uuid.uuid4(), state=state, tenant_id=tenant,
                provisioner_backend="vsphere_api", provisioner_output=output, description="",
                created_at=sa.func.now(), updated_at=sa.func.now()))
        return str(rid)

    def rows(self, range_id=None) -> list:
        with self.session() as s:
            q = sa.select(network_reservations.c.holder, network_reservations.c.value)
            if range_id:
                q = q.where(network_reservations.c.range_id == range_id)
            return sorted(s.execute(q).all())

    def state(self, range_id, new):
        with self.session() as s:
            s.execute(sa.update(ranges).where(ranges.c.id == range_id).values(state=new))

    def output(self, range_id) -> dict:
        with self.session() as s:
            raw = s.execute(sa.select(ranges.c.provisioner_output).where(ranges.c.id == range_id)).scalar()
        return json.loads(raw or "{}")


@pytest.fixture
def db():
    return DB()


class Prov:
    """A backend that needs one VLAN per holder and, optionally, one uplink address."""

    def __init__(self, holders=("200", "201"), uplink=False, domain=DOMAIN, pool=POOL):
        self.holders, self.uplink, self.domain, self.pool = list(holders), uplink, domain, pool

    def allocation_needs(self, range_id, template):
        needs = [AllocationNeed("physical_vlans", "vlan", self.domain, self.pool, self.holders)] if self.holders else []
        if self.uplink:
            needs.append(AllocationNeed("uplink_ip", "uplink_ip", "vsphere:uplink", ["10.30.32.100", "10.30.32.101"],
                                        ["edge"], single=True))
        return needs

    def planned_output(self, range_id, allocations):
        return {"networks": [{"vlan_id": int(k), "physical_vlan": int(v)} for k, v in
                             sorted(allocations.get("physical_vlans", {}).items())]}


# -- reserve_for_build / release_after_destroy -----------------------------------


def test_nothing_needed_touches_nothing(db):
    assert ra.reserve_for_build(db.session, "r", Prov(holders=()), {}) == {}
    assert ra.reserve_for_build(db.session, "r", object(), {}) == {}  # a backend that declares nothing
    assert db.opened == 0


def test_lowest_free_values_across_tenants(db):
    other = db.add_range(tenant=TENANT_B)
    ra.reserve_for_build(db.session, other, Prov(holders=["300"]), {})  # takes 100, in another tenant
    rid = db.add_range()
    got = ra.reserve_for_build(db.session, rid, Prov(uplink=True), {})
    assert got == {"physical_vlans": {"200": "101", "201": "102"}, "uplink_ip": "10.30.32.100"}


def test_a_retry_gets_the_same_values(db):
    rid = db.add_range()
    first = ra.reserve_for_build(db.session, rid, Prov(), {})
    again = ra.reserve_for_build(db.session, rid, Prov(), {})
    assert first == again and len(db.rows(rid)) == 2


def test_holders_the_build_no_longer_has_are_freed(db):
    rid = db.add_range()
    ra.reserve_for_build(db.session, rid, Prov(holders=["200", "201", "202"]), {})
    ra.reserve_for_build(db.session, rid, Prov(holders=["200"]), {})
    assert db.rows(rid) == [("200", "100")]


def test_an_exhausted_pool_reserves_nothing(db):
    rid = db.add_range()
    ra.reserve_for_build(db.session, rid, Prov(uplink=True), {})
    big = db.add_range()
    with pytest.raises(ra.PoolExhaustedError, match="has 4 free of 6; this range needs 6 more"):
        ra.reserve_for_build(db.session, big, Prov(holders=[str(v) for v in range(6)], uplink=True), {})
    assert db.rows(big) == []  # an uplink reserved earlier in that transaction is rolled back too


def test_a_destroyed_range_holds_nothing(db):
    old = db.add_range()
    ra.reserve_for_build(db.session, old, Prov(holders=[str(v) for v in range(6)]), {})
    db.state(old, "destroyed")  # destroyed, but nobody released it (a crash after the destroy)
    rid = db.add_range()
    assert ra.reserve_for_build(db.session, rid, Prov(), {})["physical_vlans"] == {"200": "100", "201": "101"}


def test_a_range_that_left_provisioning_reserves_nothing(db):
    rid = db.add_range(state="destroying")  # abandoned between the claim and the reservation
    with pytest.raises(ra.AllocationError, match="no longer provisioning"):
        ra.reserve_for_build(db.session, rid, Prov(), {})
    assert db.rows() == []


def test_a_changed_domain_is_refused(db):
    rid = db.add_range()
    ra.reserve_for_build(db.session, rid, Prov(), {})
    with pytest.raises(ra.DomainChangedError, match="site-b:vlans"):
        ra.reserve_for_build(db.session, rid, Prov(domain="site-b:vlans"), {})


def test_the_plan_is_recorded_before_the_build(db):
    rid = db.add_range(output=json.dumps({"provider": "vsphere_api", "note": "kept"}))
    ra.reserve_for_build(db.session, rid, Prov(), {})
    assert db.output(rid) == {"provider": "vsphere_api", "note": "kept",
                              "networks": [{"vlan_id": 200, "physical_vlan": 100},
                                           {"vlan_id": 201, "physical_vlan": 101}]}


def test_the_database_constraint_has_the_last_word(db):
    """Two holders handed one value in one domain (a lock that did not hold) fail the insert."""
    a, b = db.add_range(), db.add_range()
    with db.session() as s:
        db_ops.insert_reservations(s, a, DOMAIN, "vlan", {"200": "100"})
    with pytest.raises(sa.exc.IntegrityError), db.session() as s:
        db_ops.insert_reservations(s, b, DOMAIN, "vlan", {"200": "100"})


def test_release_frees_only_a_destroyed_range(db):
    a, b = db.add_range(), db.add_range()
    ra.reserve_for_build(db.session, a, Prov(), {})
    ra.reserve_for_build(db.session, b, Prov(), {})
    assert ra.release_after_destroy(db.session, a) == 0  # still live: a late call frees nothing
    db.state(a, "destroyed")
    assert ra.release_after_destroy(db.session, a) == 2
    assert db.rows(a) == [] and len(db.rows(b)) == 2
    assert ra.release_after_destroy(db.session, a) == 0  # idempotent


def test_the_vsphere_provisioner_gets_what_it_declared(db, monkeypatch):
    """allocation_needs -> reserve -> the allocations provision() checks for."""
    from worker import render
    from worker.provisioners import vsphere_api

    monkeypatch.setattr(vsphere_api, "VSPHERE_VLAN_POOL", "100-102")
    prov = vsphere_api.VsphereAPIProvisioner()
    tpl = {"network": {"vlans": [{"id": 10, "name": "a", "cidr": "10.1.0.0/24"},
                                 {"id": 20, "name": "b", "cidr": "10.2.0.0/24"}]},
           "nodes": [{"id": "x", "os": "ubuntu-2404", "vlan": "a"}, {"id": "y", "os": "ubuntu-2404", "vlan": "b"}]}
    rid = db.add_range()
    rendered = render.render_topology(tpl, rid, lambda alias: "tmpl")
    template = {"vms": rendered["vm_definitions"], "networks": rendered["network_definitions"]}
    assert ra.reserve_for_build(db.session, rid, prov, template) == {"physical_vlans": {"10": "100", "20": "101"}}
    assert [(n["vlan_id"], n["physical_vlan"], n["portgroup"]) for n in db.output(rid)["networks"]] == [
        (10, 100, f"tn-{rid[:8]}-v100"), (20, 101, f"tn-{rid[:8]}-v101")]
    other = db.add_range()
    with pytest.raises(ra.PoolExhaustedError):  # 100-102 has one left; the next range needs two
        ra.reserve_for_build(db.session, other, prov, template)


def test_lock_key_is_the_apis():
    import zlib

    assert ra.lock_key(DOMAIN, "vlan") == zlib.crc32(b"vlan:vsphere:vlans") - (1 << 31)


def test_postgres_takes_the_api_lock():
    seen = []
    fake = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
                           execute=lambda stmt: seen.append(stmt))
    db_ops.lock_reservation_domain(fake, ra.lock_key(DOMAIN, "vlan"))
    (stmt,) = seen
    from sqlalchemy.dialects import postgresql

    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert sql == f"SELECT pg_advisory_xact_lock({ra.lock_key(DOMAIN, 'vlan')}) AS pg_advisory_xact_lock_1"


# -- the tasks.py hooks -------------------------------------------------------------


class _NoDb:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def task_env(monkeypatch, lease_always_free):
    calls: dict = {"states": [], "order": []}

    def state(range_id, new_state, **kw):
        calls["states"].append((new_state, kw))
        return 1

    monkeypatch.setattr(tasks, "_update_range_state", state)
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "_db_session", _NoDb)
    monkeypatch.setattr(tasks.db_ops, "range_template_and_backend", lambda db, rid: ("{}", "vsphere_api"))
    monkeypatch.setattr(tasks.db_ops, "range_output_and_backend", lambda db, rid: ("{}", "vsphere_api"))
    return calls


def test_provision_reserves_first_and_stores_uplink_mirrors_warnings(task_env, monkeypatch):
    seen = {}

    def reserve(session, range_id, provisioner, template):
        task_env["order"].append("reserve")
        return {"physical_vlans": {"200": "101"}}

    class Prov:
        async def provision(self, range_id, template, allocations):
            task_env["order"].append("provision")
            seen["allocations"] = allocations
            return ProvisionResult(status="ok", vms=[{"vm_id": "vm-1", "name": "x"}], networks=[{"vlan_id": 200}],
                                   uplink={"ip": "10.30.32.100"}, mirrors=[{"name": "tn-x-mon"}], warnings=["w"])

    monkeypatch.setattr(tasks.range_alloc, "reserve_for_build", reserve)
    monkeypatch.setattr(tasks, "_get_backend", lambda b: Prov())
    assert tasks.provision_range.run("r-1")["status"] == "ready"
    assert task_env["order"] == ["reserve", "provision"]
    assert seen["allocations"] == {"physical_vlans": {"200": "101"}}
    (ready,) = [kw for s, kw in task_env["states"] if s == "ready"]
    assert json.loads(ready["output"]) == {
        "provider": "vsphere_api", "range_id": "r-1", "vms": [{"vm_id": "vm-1", "name": "x"}],
        "networks": [{"vlan_id": 200}], "uplink": {"ip": "10.30.32.100"}, "mirrors": [{"name": "tn-x-mon"}],
        "warnings": ["w"]}


def test_a_full_pool_fails_the_range_at_once_without_building(task_env, monkeypatch):
    def reserve(session, range_id, provisioner, template):
        raise ra.PoolExhaustedError("vlan pool for vsphere:vlans has 0 free of 100; this range needs 3 more")

    monkeypatch.setattr(tasks.range_alloc, "reserve_for_build", reserve)
    monkeypatch.setattr(tasks, "_get_backend", lambda b: SimpleNamespace(provision=pytest.fail))
    monkeypatch.setattr(tasks, "_last_attempt", lambda task: False)  # final anyway: no retry
    with pytest.raises(ra.PoolExhaustedError):
        tasks.provision_range.run("r-1")
    assert [s for s, _ in task_env["states"]][-1:] == ["failed"]  # after the fencing claim
    assert ra.AllocationError in tasks.FINAL_ERRORS and tasks.provision_range.dont_autoretry_for == tasks.FINAL_ERRORS


def test_destroy_releases_after_recording_destroyed(task_env, monkeypatch):
    def release(session, range_id):
        task_env["order"].append(("release", [s for s, _ in task_env["states"]]))
        return 2

    class Prov:
        async def destroy(self, range_id, output):
            return DestroyResult(status="ok", resources_removed=1)

    monkeypatch.setattr(tasks.range_alloc, "release_after_destroy", release)
    monkeypatch.setattr(tasks, "_get_backend", lambda b: Prov())
    assert tasks.destroy_range.run("r-1")["status"] == "destroyed"
    assert task_env["order"] == [("release", ["destroying", "destroyed"])]  # the claim, then destroyed


def test_a_failed_destroy_releases_nothing(task_env, monkeypatch):
    class Prov:
        async def destroy(self, range_id, output):
            return DestroyResult(status="failed", errors=["VM vm-1: busy"])

    monkeypatch.setattr(tasks.range_alloc, "release_after_destroy", lambda *a: pytest.fail("released"))
    monkeypatch.setattr(tasks, "_get_backend", lambda b: Prov())
    monkeypatch.setattr(tasks, "_last_attempt", lambda task: True)
    with pytest.raises(RuntimeError, match="busy"):
        tasks.destroy_range.run("r-1")
