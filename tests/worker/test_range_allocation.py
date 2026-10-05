"""vSphere VLANs and uplink addresses come from ``network_reservations``, one owner (S5a).

Before: the worker picked a range's physical VLANs and its edge firewall's uplink address
by reading every other range's ``provisioner_output`` and writing its own, under a Redis
lock that was skipped when Redis was down. Two ranges provisioning at once with Redis
unavailable could get the same VLAN (two "isolated" ranges on one segment) or the same
WAN address. The vSphere provisioner also allocated on its own, unlocked, whenever the
worker had not.

Now the provisioner declares what it needs (``allocation_needs``) and the worker reserves
it on the API's ``network_reservations`` table (unique per domain, kind and value across
tenants) under a PostgreSQL advisory lock on the domain, the same lock the API takes
(app/network_inventory.py). No Redis. The provisioner refuses to build a VLAN nobody
reserved. A destroy releases what the range held.

The Postgres tests run when TEST_POSTGRES_ADMIN_URL is set (as in
tests/api/test_network_reservations.py); the rest run on SQLite with the API's schema.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
import zlib
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from app import models as m
from app.models_network import NetworkReservation
from app.sections import Base
from sqlalchemy.orm import Session, sessionmaker

tasks = pytest.importorskip("worker.tasks")
from worker.provisioners.results import DestroyResult, ProvisionResult  # noqa: E402
from worker.provisioners.vsphere_api import VsphereAPIProvisioner  # noqa: E402

VCENTER = "vc.lab.test"
VLAN_DOMAIN = f"vsphere:{VCENTER}:vDS-10G"
UPLINK_DOMAIN = f"vsphere:{VCENTER}:dPG-TN-SVC"
TEMPLATE = {
    "name": "lab",
    "network": {
        "vlans": [{"id": 200, "name": "lan", "cidr": "10.1.0.0/24"}, {"id": 201, "name": "dmz", "cidr": "10.2.0.0/24"}]
    },
    "nodes": [
        {"id": "fw", "os": "pfsense", "role": "firewall", "vlan": "lan",
         "interfaces": [{"vlan": "lan"}, {"vlan": "dmz"}]},
        {"id": "web", "os": "ubuntu-24.04", "vlan": "dmz"},
    ],
}


@pytest.fixture
def pools(monkeypatch):
    """The lab's settings; env for the code that reads env, attributes for the provisioner."""
    env = {
        "VSPHERE_URL": f"https://{VCENTER}",
        "VSPHERE_RANGE_DVS": "vDS-10G",
        "VSPHERE_RANGE_SWITCH_MODE": "vds",
        "VSPHERE_VLAN_POOL": "100-103",
        "VSPHERE_RANGE_UPLINK_NETWORK": "dPG-TN-SVC",
        "VSPHERE_RANGE_UPLINK_POOL": "10.30.32.100-10.30.32.102",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # Redis is down. Locking must not depend on it.
    monkeypatch.setattr(tasks, "REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    return env


class Recorder:
    """A real VsphereAPIProvisioner whose vCenter calls are replaced: records what it was given."""

    def __init__(self, pools: dict):
        self.builds: list[tuple[str, dict]] = []
        self.lock = threading.Lock()
        self.pools = pools

    def make(self):
        p = VsphereAPIProvisioner()
        p._base_url = self.pools["VSPHERE_URL"]
        p._dvs_name = self.pools["VSPHERE_RANGE_DVS"]
        p._switch_mode = "vds"
        p._vlan_pool = self.pools["VSPHERE_VLAN_POOL"]
        p._uplink_network = self.pools["VSPHERE_RANGE_UPLINK_NETWORK"]
        p._uplink_pool = self.pools["VSPHERE_RANGE_UPLINK_POOL"]

        async def provision(range_id, template, allocations):
            with self.lock:
                self.builds.append((range_id, allocations))
            return ProvisionResult(status="ok", vms=[], networks=[])

        async def destroy(range_id, output):
            return DestroyResult(status="ok", resources_removed=0)

        p.provision = provision
        p.destroy = destroy
        return p

    def got(self, range_id: str) -> tuple[dict[int, int], str | None]:
        [alloc] = [a for rid, a in self.builds if rid == range_id]
        vlans = {int(k): int(v) for k, v in (alloc.get("physical_vlans") or {}).items()}
        return vlans, alloc.get("uplink_ip")


def _wire(engine, monkeypatch, recorder: Recorder):
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    @contextmanager
    def _session():  # the same commit/rollback contract as tasks._db_session
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(tasks, "_db_session", _session)
    monkeypatch.setattr(tasks, "_get_backend", lambda backend=None: recorder.make())
    return factory


def _seed(factory, n: int, state: str = "provisioning") -> SimpleNamespace:
    with factory() as s:
        tenant = m.Tenant(name=f"t-{uuid.uuid4().hex[:6]}", slug=f"t-{uuid.uuid4().hex[:6]}")
        s.add(tenant)
        s.flush()
        tmpl = m.Template(name="lab", yaml=json.dumps(TEMPLATE), tenant_id=tenant.id)
        s.add(tmpl)
        s.flush()
        ranges = [
            m.Range(name=f"r{i}", template_id=tmpl.id, tenant_id=tenant.id, provisioner_backend="vsphere_api",
                    state=m.RangeState(state))
            for i in range(n)
        ]
        s.add_all(ranges)
        s.commit()
        return SimpleNamespace(tenant=tenant.id, ids=[str(r.id) for r in ranges])


def _held(factory, range_id: str | None = None) -> set[tuple[str, str, str]]:
    with factory() as s:
        q = s.query(NetworkReservation)
        if range_id:
            q = q.filter(NetworkReservation.range_id == uuid.UUID(range_id))
        return {(r.kind, r.holder, r.value) for r in q}


def _hold(factory, tenant, range_id: str, domain: str, kind: str, value: str, holder: str):
    with factory() as s:
        s.add(NetworkReservation(id=uuid.uuid4(), tenant_id=tenant, range_id=uuid.UUID(range_id), domain=domain,
                                 kind=kind, value=value, holder=holder))
        s.commit()


def _state(factory, range_id: str) -> tuple[str, str | None]:
    with factory() as s:
        r = s.get(m.Range, uuid.UUID(range_id))
        return r.state.value, r.error_message


# -- SQLite, the API's schema ----------------------------------------------------
@pytest.fixture
def sqlite_engine(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'tn.db'}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def world(sqlite_engine, pools, monkeypatch):
    rec = Recorder(pools)
    factory = _wire(sqlite_engine, monkeypatch, rec)
    return SimpleNamespace(factory=factory, rec=rec)


def test_a_vsphere_build_reserves_its_vlans_and_uplink_on_the_shared_table(world):
    [rid] = _seed(world.factory, 1).ids
    assert tasks.provision_range.run(rid)["status"] == "ready"
    vlans, uplink = world.rec.got(rid)
    assert vlans == {200: 100, 201: 101}
    assert uplink == "10.30.32.100"
    assert _held(world.factory, rid) == {
        ("vlan", "200", "100"), ("vlan", "201", "101"), ("uplink_ip", "edge", "10.30.32.100")
    }


def test_values_a_live_range_holds_are_skipped_and_a_destroyed_ranges_are_free(world):
    seed = _seed(world.factory, 1)
    live = _seed(world.factory, 1, state="ready")
    gone = _seed(world.factory, 1, state="destroyed")
    _hold(world.factory, live.tenant, live.ids[0], VLAN_DOMAIN, "vlan", "100", "200")
    _hold(world.factory, gone.tenant, gone.ids[0], VLAN_DOMAIN, "vlan", "101", "200")
    _hold(world.factory, live.tenant, live.ids[0], UPLINK_DOMAIN, "uplink_ip", "10.30.32.100", "edge")
    [rid] = seed.ids
    tasks.provision_range.run(rid)
    vlans, uplink = world.rec.got(rid)
    assert vlans == {200: 101, 201: 102}, "another tenant's live range keeps VLAN 100; the destroyed one's is free"
    assert uplink == "10.30.32.101"
    assert _held(world.factory, gone.ids[0]) == set()


def test_a_retry_gets_the_same_values(world):
    a, b = _seed(world.factory, 2).ids
    tasks.provision_range.run(a)
    first = world.rec.got(a)
    tasks.provision_range.run(b)
    with world.factory() as s:  # as the API leaves a range it provisions again
        s.get(m.Range, uuid.UUID(a)).state = m.RangeState.provisioning
        s.commit()
    world.rec.builds = [x for x in world.rec.builds if x[0] != a]
    tasks.provision_range.run(a)
    assert world.rec.got(a) == first


def test_an_exhausted_pool_fails_the_range_before_anything_is_built(world, monkeypatch):
    first, second, third = _seed(world.factory, 3).ids
    tasks.provision_range.run(first)
    tasks.provision_range.run(second)  # VLANs 100-103 are all held now
    monkeypatch.setattr(tasks, "_last_attempt", lambda task: True)
    with pytest.raises(Exception, match="(?i)pool"):
        tasks.provision_range.run(third)
    assert [rid for rid, _ in world.rec.builds] == [first, second], "nothing was built for the third range"
    state, error = _state(world.factory, third)
    assert state == "failed" and "pool" in error.lower()
    assert _held(world.factory, third) == set(), "a refused reservation holds nothing"


def test_destroy_releases_what_the_range_held(world):
    keep, rid = _seed(world.factory, 2).ids
    tasks.provision_range.run(keep)
    tasks.provision_range.run(rid)
    with world.factory() as s:  # as the API leaves it (fencing.py)
        s.get(m.Range, uuid.UUID(rid)).state = m.RangeState.destroying
        s.commit()
    assert tasks.destroy_range.run(rid)["status"] == "destroyed"
    assert _held(world.factory, rid) == set()
    assert len(_held(world.factory, keep)) == 3, "another range's reservations stay"


def test_the_provisioner_does_not_allocate_vlans_or_addresses_on_its_own(pools):
    """The old fallback picked VLANs and an uplink address itself, under no lock at all."""
    import asyncio

    p = Recorder(pools).make()
    del p.provision  # the real one
    template = {"vms": [VsphereAPIProvisioner._vm_plan("r", {"name": "fw", "os": "pfsense", "role": "firewall",
                                                              "nics": [{"vlan": 200, "ip": "10.1.0.1"}]})],
                "networks": [{"name": "lan", "vlan_id": 200, "cidr": "10.1.0.0/24"}]}
    result = asyncio.run(p.provision("r-unreserved", template, {}))
    assert result.status == "failed"
    assert "reserv" in " ".join(result.errors).lower()


def test_the_api_and_the_worker_take_the_same_lock_for_a_domain():
    from app import network_inventory as inv
    from worker import db_ops

    for domain, kind in [(VLAN_DOMAIN, "vlan"), (UPLINK_DOMAIN, "uplink_ip"), ("vsphere:x:TN-Noise", "noise_mgmt_ip")]:
        expected = zlib.crc32(f"{kind}:{domain}".encode()) - (1 << 31)
        assert inv.lock_key(domain, kind) == db_ops.reservation_lock_key(domain, kind) == expected


# -- PostgreSQL: the races -------------------------------------------------------
@pytest.fixture
def pg_engine():
    admin_url = os.getenv("TEST_POSTGRES_ADMIN_URL")
    if not admin_url:
        pytest.skip("set TEST_POSTGRES_ADMIN_URL to run against Postgres")
    name = f"tn_alloc_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    eng = sa.create_engine(sa.engine.make_url(admin_url).set(database=name), pool_size=20)
    try:
        Base.metadata.create_all(eng)
        yield eng
    finally:
        eng.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def test_on_postgres_concurrent_builds_never_share_a_vlan_or_an_uplink_address(pg_engine, pools, monkeypatch):
    monkeypatch.setenv("VSPHERE_VLAN_POOL", "100-199")
    monkeypatch.setenv("VSPHERE_RANGE_UPLINK_POOL", "10.30.32.100-10.30.32.199")
    pools.update(VSPHERE_VLAN_POOL="100-199", VSPHERE_RANGE_UPLINK_POOL="10.30.32.100-10.30.32.199")
    rec = Recorder(pools)
    factory = _wire(pg_engine, monkeypatch, rec)
    ids = _seed(factory, 8).ids
    start = threading.Barrier(len(ids))
    errors: list[BaseException] = []

    def build(rid):
        start.wait()
        try:
            tasks.provision_range.run(rid)
        except BaseException as e:  # noqa: BLE001 — reported below
            errors.append(e)

    threads = [threading.Thread(target=build, args=(rid,)) for rid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    vlans = [v for rid in ids for v in rec.got(rid)[0].values()]
    uplinks = [rec.got(rid)[1] for rid in ids]
    assert len(vlans) == 2 * len(ids) and len(set(vlans)) == len(vlans), f"a VLAN was handed out twice: {vlans}"
    assert None not in uplinks and len(set(uplinks)) == len(uplinks), f"an uplink was handed out twice: {uplinks}"


def test_on_postgres_with_redis_down_a_reservation_still_waits_for_the_domain_lock(pg_engine, pools, monkeypatch):
    rec = Recorder(pools)
    factory = _wire(pg_engine, monkeypatch, rec)
    [rid] = _seed(factory, 1).ids
    holder = factory()
    key = zlib.crc32(f"vlan:{VLAN_DOMAIN}".encode()) - (1 << 31)  # what app/network_inventory.py locks
    holder.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
    t = threading.Thread(target=lambda: tasks.provision_range.run(rid))
    t.start()
    t.join(1.0)
    try:
        assert t.is_alive() and not rec.builds, "the build went ahead while another transaction held the VLAN lock"
    finally:
        holder.rollback()
        holder.close()
        t.join(30)
    assert rec.got(rid)[0] == {200: 100, 201: 101}
