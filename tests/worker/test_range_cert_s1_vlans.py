"""Range certification S1: atomic VLAN allocation against the live switch.

* vsphere_vlans: what the live switch carries (trunks expanded, uplink port groups skipped)
  and when a planned port group collides with it (ported from the Ansible kit's preflight).
* range_alloc: VLANs used outside TrueNorth become external reservations no allocator
  hands out, refreshed per switch; reserved VLANs and site CIDRs are refused; a collision
  at creation releases the range's VLANs and fails it finally (``build_failure``).
* PostgreSQL (TEST_POSTGRES_ADMIN_URL, as CI): concurrent builds never share a VLAN, with
  the live read and the external recording inside the same advisory-locked transaction.

The fake-vCenter half (a VLAN appearing between allocation and creation) is in
test_vsphere_provision.py::TestLiveVlanRecheck; the vcsim half in
tests/integration/test_vsphere_sim.py.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import app.network_inventory  # noqa: F401 — registers network_reservations
import app.range_leases  # noqa: F401
import pytest
import sqlalchemy as sa
from app.db import Base
from sqlalchemy import StaticPool
from sqlalchemy.orm import Session, sessionmaker

tasks = pytest.importorskip("worker.tasks")
vim = pytest.importorskip("pyVmomi").vim
from worker import range_alloc as ra  # noqa: E402
from worker import reserved_networks  # noqa: E402
from worker.provisioners import vsphere_vlans as live  # noqa: E402
from worker.provisioners.base import AllocationNeed, LiveUsage  # noqa: E402
from worker.provisioners.results import ProvisionResult  # noqa: E402
from worker.tables import network_reservations, ranges  # noqa: E402

DOMAIN = "vsphere:vlans"
POOL = [str(v) for v in range(100, 110)]
SOURCE = "vc01/vDS-10G"
DVS_VLAN = vim.dvs.VmwareDistributedVirtualSwitch


# --------------------------------------------------------------------------- #
# The live switch (vsphere_vlans)
# --------------------------------------------------------------------------- #


def _pg(name, vlan_spec, description="", uplink=None, tag=()):
    config = SimpleNamespace(defaultPortConfig=SimpleNamespace(vlan=vlan_spec), description=description)
    if uplink is not None:
        config.uplink = uplink
    return SimpleNamespace(name=name, config=config, tag=list(tag))


def _trunk(*ranges_):
    return DVS_VLAN.TrunkVlanSpec(vlanId=[vim.NumericRange(start=a, end=b) for a, b in ranges_])


def test_trunk_ranges_are_expanded():
    assert live.expand(_trunk((100, 103), (150, 150))) == (frozenset({100, 101, 102, 103, 150}), True)
    assert live.expand(_trunk((0, 4094)))[0] == frozenset(range(1, 4095))  # 0 (untagged) is not a VLAN
    assert live.expand(DVS_VLAN.VlanIdSpec(vlanId=120)) == (frozenset({120}), False)
    assert live.expand(DVS_VLAN.VlanIdSpec(vlanId=0)) == (frozenset(), False)  # untagged
    assert live.expand(DVS_VLAN.PvlanSpec(pvlanId=130)) == (frozenset({130}), False)
    assert live.expand(None) == (frozenset(), False)


def test_uplink_port_groups_are_skipped_and_everything_else_is_read():
    dvs = SimpleNamespace(name="vDS-10G", portgroup=[
        _pg("vDS-10G-DVUplinks-35", _trunk((0, 4094))),  # by the kit's name rule
        _pg("uplinks-renamed", _trunk((0, 4094)), uplink=True),  # by vCenter's own flag
        _pg("tagged-uplinks", _trunk((0, 4094)), tag=[SimpleNamespace(key="SYSTEM/DVS.UPLINKPG")]),
        _pg("corp-voice", DVS_VLAN.VlanIdSpec(vlanId=101)),
        _pg("nested-esx-trunk", _trunk((104, 105))),
        _pg("tn-0a1b2c3d-v106", DVS_VLAN.VlanIdSpec(vlanId=106), live.owner_mark("0a1b2c3d-...")),
    ])
    uses = live.dvs_usage(dvs)
    assert [u.name for u in uses] == ["corp-voice", "nested-esx-trunk", "tn-0a1b2c3d-v106"]
    foreign, managed = live.by_value(uses, POOL)
    assert foreign == {"101": "corp-voice", "104": "nested-esx-trunk", "105": "nested-esx-trunk"}
    assert managed == {"106": "tn-0a1b2c3d-v106"}


def test_standard_switch_all_vlans_is_a_trunk():
    pg = lambda name, vlan: SimpleNamespace(spec=SimpleNamespace(name=name, vlanId=vlan))  # noqa: E731
    host = SimpleNamespace(config=SimpleNamespace(network=SimpleNamespace(portgroup=[
        pg("Management Network", 0), pg("trunk-all", 4095), pg("corp", 101)])))
    uses = {u.name: u for u in live.vss_usage([host, host])}
    assert uses["Management Network"].vlans == frozenset()
    assert uses["trunk-all"].trunk and 4094 in uses["trunk-all"].vlans
    assert uses["corp"].vlans == {101}


RID = "0a1b2c3d-1111-2222-3333-444455556666"
NAME = f"tn-{RID[:8]}-v101"


@pytest.mark.parametrize("uses, reason", [
    ([], None),
    ([live.PortgroupUse(NAME, frozenset({101}))], None),  # a retry: this range's own, unmarked (pre-S1)
    ([live.PortgroupUse(NAME, frozenset({101}), description=live.owner_mark(RID))], None),
    ([live.PortgroupUse(NAME, frozenset({102}))], "already exists on vDS with VLAN 102"),
    ([live.PortgroupUse(NAME, frozenset({101}), description="someone else's")], "not this range's"),
    ([live.PortgroupUse(NAME, frozenset({101}), trunk=True)], "(trunk)"),
    ([live.PortgroupUse("corp", frozenset({101}))], "VLAN 101 is already used by port group 'corp'"),
    ([live.PortgroupUse("lab-trunk", frozenset(range(100, 120)), trunk=True)],
     "VLAN 101 is already used by trunk port group 'lab-trunk'"),
])
def test_a_planned_port_group_collides_with_the_live_switch(uses, reason):
    got = live.conflict(uses, NAME, 101, RID, "vDS")
    assert (got is None) if reason is None else (reason in got), got


def test_check_names_the_collision_and_says_the_build_stopped():
    with pytest.raises(live.PortgroupConflictError) as err:
        live.check([live.PortgroupUse("corp", frozenset({101}))], NAME, 101, RID, "vDS")
    assert (err.value.vlan, err.value.used_by) == (101, "corp")
    assert "stopped before creating anything further" in str(err.value)
    assert live.check([live.PortgroupUse(NAME, frozenset({101}))], NAME, 101, RID, "vDS") is True  # reuse


# --------------------------------------------------------------------------- #
# The allocator (range_alloc), on SQLite
# --------------------------------------------------------------------------- #


class DB:
    def __init__(self, engine=None):
        self.engine = engine or sa.create_engine("sqlite://", poolclass=StaticPool,
                                                 connect_args={"check_same_thread": False})
        if engine is None:
            Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, class_=Session)
        self.opened = 0

    @contextmanager
    def session(self):
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

    def add_range(self, state="provisioning", output=None) -> str:
        from app.models import Range, Template, Tenant

        tid, rid = uuid.uuid4(), uuid.uuid4()
        with self.session() as s:  # ORM rows: PostgreSQL enforces the foreign keys
            s.add(Tenant(id=tid, name=f"t-{tid.hex[:8]}", slug=f"t-{tid.hex[:8]}"))
            s.flush()
            tmpl = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=tid)
            s.add(tmpl)
            s.flush()
            s.add(Range(id=rid, name="r", template_id=tmpl.id, tenant_id=tid, state=state,
                        provisioner_backend="vsphere_api", provisioner_output=json.dumps(output) if output else None))
        return str(rid)

    def rows(self) -> list[tuple]:
        nr = network_reservations
        with self.session() as s:
            return sorted((r.value, r.holder, str(r.range_id) if r.range_id else None) for r in s.execute(
                sa.select(nr.c.value, nr.c.holder, nr.c.range_id)).all())

    def externals(self) -> dict[str, str]:
        return {v: h for v, h, rid in self.rows() if rid is None}

    def output(self, rid) -> dict:
        with self.session() as s:
            return json.loads(s.execute(sa.select(ranges.c.provisioner_output).where(ranges.c.id == rid)).scalar()
                              or "{}")

    def state(self, rid, new):
        with self.session() as s:
            s.execute(sa.update(ranges).where(ranges.c.id == rid).values(state=new))


@pytest.fixture
def db():
    return DB()


class Prov:
    """Declares physical VLANs for ``holders`` and reports ``seen`` as the live switch."""

    def __init__(self, holders=("10", "20"), foreign=None, managed=None, excluded=(), source=SOURCE, fail=None):
        self.holders, self.excluded, self.source, self.fail = list(holders), frozenset(excluded), source, fail
        self.foreign, self.managed = dict(foreign or {}), dict(managed or {})
        self.reads = 0

    def allocation_needs(self, range_id, template):
        return [AllocationNeed("physical_vlans", "vlan", DOMAIN, POOL, self.holders, excluded=self.excluded)]

    def live_usage(self, needs):
        self.reads += 1
        if self.fail:
            raise self.fail
        return [LiveUsage(DOMAIN, "vlan", self.source, self.foreign, self.managed)]

    def planned_output(self, range_id, allocations):
        return {"networks": [{"vlan_id": int(k), "physical_vlan": int(v), "portgroup": f"tn-{range_id[:8]}-v{v}"}
                             for k, v in sorted(allocations["physical_vlans"].items())]}


def test_a_vlan_used_outside_truenorth_is_recorded_and_never_allocated(db):
    rid = db.add_range()
    got = ra.reserve_for_build(db.session, rid, Prov(foreign={"100": "corp-voice", "102": "esx-trunk"}), {})
    assert got == {"physical_vlans": {"10": "101", "20": "103"}}
    assert db.externals() == {"100": f"external:{SOURCE}:corp-voice", "102": f"external:{SOURCE}:esx-trunk"}


def test_a_vlan_freed_outside_truenorth_is_allocatable_again(db):
    a = db.add_range()
    ra.reserve_for_build(db.session, a, Prov(holders=["10"], foreign={"100": "corp", "101": "old"}), {})
    b = db.add_range()  # the next read of the same switch no longer sees 100
    assert ra.reserve_for_build(db.session, b, Prov(holders=["10"], foreign={"101": "old"}), {}) == {
        "physical_vlans": {"10": "100"}}
    assert db.externals() == {"101": f"external:{SOURCE}:old"}


def test_another_switchs_entries_stay_until_they_expire(db):
    a = db.add_range()
    ra.reserve_for_build(db.session, a, Prov(holders=["10"], foreign={"100": "corp"}, source="vc02/vDS-B"), {})
    b = db.add_range()
    got = ra.reserve_for_build(db.session, b, Prov(holders=["10"], foreign={}), {})  # vc01 sees nothing
    assert got == {"physical_vlans": {"10": "102"}}  # 100: vc02's still; 101: range a
    with db.session() as s:  # nobody has read vc02 for longer than the TTL
        ra.record_external(s, LiveUsage(DOMAIN, "vlan", SOURCE, {}), now=datetime.now(UTC) + ra.EXTERNAL_TTL
                           + timedelta(hours=1))
    assert db.externals() == {}


def test_an_orphaned_truenorth_port_group_keeps_its_vlan_but_a_live_ranges_does_not_double_count(db):
    a = db.add_range()
    ra.reserve_for_build(db.session, a, Prov(holders=["10"]), {})  # a holds 100
    b = db.add_range()
    managed = {"100": f"tn-{a[:8]}-v100", "101": "tn-deadbeef-v101"}  # 101: a destroyed range's leftover
    assert ra.reserve_for_build(db.session, b, Prov(holders=["10"], managed=managed), {}) == {
        "physical_vlans": {"10": "102"}}
    assert db.externals() == {"101": f"external:{SOURCE}:tn-deadbeef-v101 (orphaned TrueNorth port group)"}


def test_a_retry_keeps_its_own_vlans_even_though_its_port_groups_are_live(db):
    rid = db.add_range()
    first = ra.reserve_for_build(db.session, rid, Prov(), {})
    managed = {v: f"tn-{rid[:8]}-v{v}" for v in first["physical_vlans"].values()}
    assert ra.reserve_for_build(db.session, rid, Prov(managed=managed), {}) == first
    assert db.externals() == {}


def test_a_foreign_port_group_on_a_vlan_a_range_holds_is_logged_not_recorded(db, caplog):
    a = db.add_range()
    ra.reserve_for_build(db.session, a, Prov(holders=["10"]), {})  # 100
    b = db.add_range()
    with caplog.at_level("WARNING"):
        ra.reserve_for_build(db.session, b, Prov(holders=["10"], foreign={"100": "squatter"}), {})
    assert db.externals() == {}  # the unique value is the range's; the collision is on the wire
    assert "also used outside TrueNorth" in caplog.text and "squatter" in caplog.text


def test_an_unreadable_switch_reserves_nothing_and_is_retryable(db):
    rid = db.add_range()
    opened = db.opened
    with pytest.raises(RuntimeError, match="could not read the VLANs in use") as err:
        ra.reserve_for_build(db.session, rid, Prov(fail=OSError("vCenter unreachable")), {})
    assert not isinstance(err.value, tuple(tasks.FINAL_ERRORS))
    assert db.opened == opened and db.rows() == []  # no reservation transaction was opened


def test_an_exhausted_pool_says_how_much_is_used_outside_truenorth(db):
    rid = db.add_range()
    foreign = {v: "corp" for v in POOL[:9]}
    with pytest.raises(ra.PoolExhaustedError, match=r"\(9 in use outside TrueNorth on the live switch\)"):
        ra.reserve_for_build(db.session, rid, Prov(foreign=foreign), {})


def test_reserved_vlans_are_never_handed_out(db):
    rid = db.add_range()
    got = ra.reserve_for_build(db.session, rid, Prov(excluded={"100", "101"}), {})
    assert got == {"physical_vlans": {"10": "102", "20": "103"}}


def test_a_range_holding_a_vlan_that_became_reserved_is_refused(db):
    rid = db.add_range()
    ra.reserve_for_build(db.session, rid, Prov(), {})  # 100, 101
    with pytest.raises(ra.ReservedNetworkError, match="holds reserved vlan 100"):
        ra.reserve_for_build(db.session, rid, Prov(excluded={"100"}), {})
    assert ra.ReservedNetworkError.__mro__[1] is ra.AllocationError  # final


def test_the_vsphere_pool_leaves_out_reserved_vlans(monkeypatch):
    from worker import render
    from worker.provisioners import vsphere_api

    monkeypatch.setattr(vsphere_api, "VSPHERE_VLAN_POOL", "30-34")
    monkeypatch.setattr(vsphere_api, "VSPHERE_RESERVED_VLANS", "30-32")
    tpl = {"network": {"vlans": [{"id": 10, "name": "a", "cidr": "10.1.0.0/24"}]},
           "nodes": [{"id": "x", "os": "ubuntu-2404", "vlan": "a"}]}
    rendered = render.render_topology(tpl, RID, lambda alias: "tmpl")
    template = {"vms": rendered["vm_definitions"], "networks": rendered["network_definitions"]}
    (need, *_) = vsphere_api.VsphereAPIProvisioner().allocation_needs(RID, template)
    assert (list(need.pool), need.excluded) == (["33", "34"], frozenset({"30", "31", "32"}))


# -- reserved site CIDRs ----------------------------------------------------------


SITE = "10.30.30.0/24,10.30.31.0/24,10.30.32.0/24"


@pytest.mark.parametrize("template, hit", [
    ({"network": {"vlans": [{"name": "lan", "cidr": "10.30.31.0/25"}]}}, "network.vlans[0] lan"),
    ({"inputs": {"cidr_dmz": "10.30.0.0/16"}}, "inputs.cidr_dmz"),  # a supernet overlaps too
    ({"networks": [{"name": "lan", "vlan_id": 10, "cidr": "10.30.32.128/26"}]}, "networks[0] lan"),
    ({"network": {"vlans": [{"name": "lan", "cidr": "10.30.33.0/24"}]}}, None),
    ({"network": {"vlans": [{"name": "lan", "cidr": "not-a-cidr"}]}}, None),  # the schema reports it
])
def test_template_subnets_overlapping_site_networks(template, hit):
    found = reserved_networks.conflicts(template, reserved_networks.reserved_cidrs(SITE))
    assert (found == []) if hit is None else (found and all(f.startswith(hit) for f in found)), found


def test_a_template_on_a_site_network_is_refused_before_anything_is_reserved(db, monkeypatch):
    monkeypatch.setenv("TN_RANGE_RESERVED_CIDRS", SITE)
    rid = db.add_range()
    opened = db.opened
    template = {"networks": [{"name": "lan", "vlan_id": 10, "cidr": "10.30.30.0/24"}]}
    with pytest.raises(ra.ReservedNetworkError, match="10.30.30.0/24 overlaps the reserved site network"):
        ra.reserve_for_build(db.session, rid, Prov(), template)
    with pytest.raises(ra.ReservedNetworkError):  # also for a backend that reserves nothing (mock)
        ra.reserve_for_build(db.session, rid, object(), template)
    assert db.opened == opened and db.rows() == []
    monkeypatch.setenv("TN_RANGE_RESERVED_CIDRS", "")
    assert ra.reserve_for_build(db.session, rid, object(), template) == {}  # unset: no site networks


# -- a collision at creation (build_failure) ---------------------------------------


def _conflicted(rid, release, vlans):
    return ProvisionResult(status="failed", errors=["VLAN 101 is already used by port group 'squat' on vDS"],
                           conflict={"domain": DOMAIN, "source": SOURCE, "vlans": vlans, "release": release})


def test_a_collision_releases_the_vlans_records_it_and_fails_finally(db):
    rid = db.add_range()
    ra.reserve_for_build(db.session, rid, Prov(), {})  # 100, 101 and their planned port groups
    err = ra.build_failure(db.session, rid, _conflicted(rid, ["100", "101"], {"101": "squat"}))
    assert isinstance(err, ra.NetworkConflictError) and isinstance(err, tuple(tasks.FINAL_ERRORS))
    assert "already used by port group 'squat'" in str(err)
    assert db.rows() == [("101", f"external:{SOURCE}:squat", None)]  # nothing of the range's left
    assert db.output(rid)["networks"] == []  # its destroy will not look for (or remove) those names
    other = db.add_range()  # and nobody is handed the squatted VLAN
    assert ra.reserve_for_build(db.session, other, Prov(holders=["10"]), {}) == {"physical_vlans": {"10": "100"}}


def test_a_collision_keeps_what_an_earlier_attempt_built(db):
    rid = db.add_range()
    ra.reserve_for_build(db.session, rid, Prov(), {})
    ra.build_failure(db.session, rid, _conflicted(rid, ["101"], {"101": "squat"}))  # v100 exists: kept
    assert [(v, r) for v, _, r in db.rows()] == [("100", rid), ("101", None)]
    assert [n["physical_vlan"] for n in db.output(rid)["networks"]] == [100]


def test_a_collision_on_a_range_no_longer_provisioning_releases_nothing(db):
    rid = db.add_range()
    ra.reserve_for_build(db.session, rid, Prov(), {})
    db.state(rid, "failed")  # abandoned meanwhile: its destroy handles the values
    assert isinstance(ra.build_failure(db.session, rid, _conflicted(rid, ["100", "101"], {"101": "x"})),
                      ra.NetworkConflictError)
    assert [r for _, _, r in db.rows()] == [rid, rid]


def test_any_other_failed_build_is_a_retryable_error(db):
    err = ra.build_failure(db.session, "r", ProvisionResult(status="failed", errors=["clone failed"]))
    assert type(err) is RuntimeError and str(err) == "clone failed"


def test_the_provision_task_fails_a_collided_range_without_retrying(monkeypatch, lease_always_free):
    states = []
    released = []
    monkeypatch.setattr(tasks, "_update_range_state", lambda rid, st, **kw: states.append((st, kw)) or 1)
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    class _Nothing:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(tasks, "_db_session", _Nothing)
    monkeypatch.setattr(tasks.db_ops, "range_template_and_backend", lambda db, rid: ("{}", "vsphere_api"))
    monkeypatch.setattr(tasks.range_alloc, "reserve_for_build", lambda *a: {"physical_vlans": {"10": "100"}})
    monkeypatch.setattr(tasks.range_alloc, "release_conflict", lambda db, rid, c: released.append(c))
    monkeypatch.setattr(tasks.db_ops, "lock_range_in_state", lambda db, rid, st: True)

    async def provision(range_id, template, allocations):
        return _conflicted(range_id, ["100"], {"100": "squat"})

    monkeypatch.setattr(tasks, "_get_backend", lambda b, range_id=None, **kw: SimpleNamespace(provision=provision))
    monkeypatch.setattr(tasks.range_accounts, "provision", lambda prov, rid, tpl, alloc, session: prov.provision(
        rid, tpl, alloc))
    monkeypatch.setattr(tasks, "_last_attempt", lambda task: False)  # final anyway
    with pytest.raises(ra.NetworkConflictError):
        tasks.provision_range.run("r-1")
    assert states[-1][0] == "failed" and "squat" in states[-1][1]["error"]
    assert released == [{"domain": DOMAIN, "source": SOURCE, "vlans": {"100": "squat"}, "release": ["100"]}]
    assert ra.NetworkConflictError in tasks.provision_range.dont_autoretry_for or issubclass(
        ra.NetworkConflictError, tuple(tasks.provision_range.dont_autoretry_for))


# --------------------------------------------------------------------------- #
# PostgreSQL: concurrent builds
# --------------------------------------------------------------------------- #


@pytest.fixture
def pg():
    admin_url = os.getenv("TEST_POSTGRES_ADMIN_URL")
    if not admin_url:
        pytest.skip("set TEST_POSTGRES_ADMIN_URL to run against Postgres")
    import app.main  # noqa: F401 — every section's tables on Base.metadata

    name = f"tn_s1_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    engine = sa.create_engine(sa.engine.make_url(admin_url).set(database=name), pool_size=20)
    try:
        Base.metadata.create_all(engine)
        yield DB(engine)
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_on_postgres_a_second_build_waits_for_the_first_and_takes_other_vlans(pg):
    """A holds its transaction open after reserving; B (another range, same domain) must
    wait on the advisory lock, then take the next free VLANs, past A's and the external one."""
    a, b = pg.add_range(), pg.add_range()
    gate, out, errors = threading.Event(), {}, []

    @contextmanager
    def held_open():  # A's transaction: reserved, not yet committed
        s = pg.factory()
        try:
            yield s
            s.flush()
            gate.wait(10)
            s.commit()
        finally:
            s.close()

    def build(key, session, rid):
        try:
            out[key] = ra.reserve_for_build(session, rid, Prov(foreign={"100": "corp"}), {})
        except BaseException as exc:  # noqa: BLE001 — reported below
            errors.append(exc)

    ta = threading.Thread(target=build, args=("a", held_open, a))
    ta.start()
    for _ in range(100):  # A has reserved and is holding its transaction (and the domain lock)
        with pg.factory() as s:
            if s.execute(sa.select(sa.func.count()).select_from(sa.table("pg_locks", sa.column("locktype")))
                         .where(sa.column("locktype") == "advisory")).scalar():
                break
        threading.Event().wait(0.05)
    tb = threading.Thread(target=build, args=("b", pg.session, b))
    tb.start()
    tb.join(0.7)
    assert "b" not in out, "B did not wait for A's lock on the VLAN domain"
    gate.set()
    ta.join(10)
    tb.join(10)
    assert not errors, errors
    assert out["a"] == {"physical_vlans": {"10": "101", "20": "102"}}
    assert out["b"] == {"physical_vlans": {"10": "103", "20": "104"}}


def test_on_postgres_many_concurrent_builds_never_share_a_vlan(pg):
    rids = [pg.add_range() for _ in range(4)]
    barrier, out, errors = threading.Barrier(len(rids)), {}, []

    def build(rid):
        try:
            barrier.wait(10)
            out[rid] = ra.reserve_for_build(pg.session, rid, Prov(foreign={"105": "corp"}), {})
        except BaseException as exc:  # noqa: BLE001 — reported below
            errors.append(exc)

    threads = [threading.Thread(target=build, args=(r,)) for r in rids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    got = [v for r in rids for v in out[r]["physical_vlans"].values()]
    assert len(got) == len(set(got)) == 8 and "105" not in got
    assert sorted(got, key=int) == ["100", "101", "102", "103", "104", "106", "107", "108"]
    assert pg.externals() == {"105": f"external:{SOURCE}:corp"}  # recorded once, by whichever came first
