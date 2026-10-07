"""Reserving a build's VLANs and uplink address on network_reservations (worker/range_alloc.py).

SQLite stands in for PostgreSQL: the unique constraints hold, the advisory lock is
PostgreSQL-only (it serialises concurrent builds there; SQLite serialises writers itself).
Concurrency on PostgreSQL is not exercised here.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from worker import range_alloc as ra
from worker.provisioners.base import AllocationNeed

TENANT_A, TENANT_B = uuid.uuid4(), uuid.uuid4()
POOL = [str(v) for v in range(100, 106)]
DOMAIN = "vsphere:vlans"


@pytest.fixture
def db():
    engine = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    ra.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session)
    sessions = {"opened": 0}

    @contextmanager
    def session():  # tasks._db_session: commit on success, roll back on an exception
        sessions["opened"] += 1
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def add_range(state="provisioning", tenant=TENANT_A, output=None):
        rid = uuid.uuid4()
        with session() as s:
            s.execute(sa.insert(ra.ranges).values(id=rid, tenant_id=tenant, state=state, provisioner_output=output))
        return str(rid)

    def rows(range_id=None):
        with session() as s:
            q = sa.select(ra.network_reservations.c.holder, ra.network_reservations.c.value)
            if range_id:
                q = q.where(ra.network_reservations.c.range_id == range_id)
            return sorted(s.execute(q).all())

    def state(range_id, new):
        with session() as s:
            s.execute(sa.update(ra.ranges).where(ra.ranges.c.id == range_id).values(state=new))

    def output(range_id):
        with session() as s:
            return json.loads(s.execute(sa.select(ra.ranges.c.provisioner_output)
                                        .where(ra.ranges.c.id == range_id)).scalar() or "{}")

    return type("DB", (), {"engine": engine, "session": staticmethod(session), "add_range": staticmethod(add_range),
                           "rows": staticmethod(rows), "state": staticmethod(state), "output": staticmethod(output),
                           "sessions": sessions})


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


def test_nothing_needed_touches_nothing(db):
    assert ra.reserve_for_build(db.session, "r", Prov(holders=()), {}) == {}
    assert db.sessions["opened"] == 0


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


def test_without_the_table_a_build_that_needs_it_fails(db):
    with db.engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE network_reservations"))
    rid = db.add_range()
    with pytest.raises(ra.ReservationsUnavailableError, match="migrations"):
        ra.reserve_for_build(db.session, rid, Prov(), {})
    assert ra.release_after_destroy(db.session, rid) == 0  # and the destroy side is a no-op


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
    got = ra.reserve_for_build(db.session, rid, prov, template)
    assert got == {"physical_vlans": {"10": "100", "20": "101"}}
    assert [(n["vlan_id"], n["physical_vlan"], n["portgroup"]) for n in db.output(rid)["networks"]] == [
        (10, 100, f"tn-{rid[:8]}-v100"), (20, 101, f"tn-{rid[:8]}-v101")]
    other = db.add_range()
    with pytest.raises(ra.PoolExhaustedError):  # 100-102 has one left; the next range needs two
        ra.reserve_for_build(db.session, other, prov, template)


def test_lock_keys_are_signed_32_bit_and_stable():
    key = ra.lock_key(DOMAIN, "vlan")
    assert -(2**31) <= key < 2**31 and key == ra.lock_key(DOMAIN, "vlan") != ra.lock_key(DOMAIN, "uplink_ip")
