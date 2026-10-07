"""CapacityService (ADR 0006): real host supply under the overcommit policy, and running
ranges that no booking covers counted as committed (the overtax gap ADR 0004 names)."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from app.auth import CurrentUser, get_current_user
from app.capacity import running_ranges, supply
from app.main import app as fastapi_app
from app.models import HypervisorConnection, HypervisorNode, Range, RangeState, Template, UserRole
from app.scheduler.capacity import ClusterCapacity
from app.scheduler.models import EventState, ScheduledEvent

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
SOON = (datetime.now(UTC) + timedelta(days=2)).replace(hour=13, minute=0, second=0, microsecond=0)
# 2 VMs at the worker's defaults: 4 vCPU, 8 GB, 120 GB.
TWO_VMS = "nodes:\n  - id: ws\n    os: win11\n    count: 2\n"


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    for k, v in {
        "CLUSTER_TOTAL_VCPU": "10",
        "CLUSTER_TOTAL_RAM_MB": str(20 * 1024),
        "CLUSTER_TOTAL_DISK_GB": "1000",
        "CLUSTER_OVERHEAD_PCT": "0",
        "CAPACITY_VCPU_RATIO": "4",
    }.items():
        monkeypatch.setenv(k, v)


def _conn(db, active: bool = True) -> HypervisorConnection:
    c = HypervisorConnection(
        id=uuid.uuid4(), name="vc", hypervisor_type="vsphere", host="vc.example.test", username="u", is_active=active
    )
    db.add(c)
    db.flush()
    return c


def _host(db, conn, cores=None, ram_gb=None, disk_gb=None, status="online") -> HypervisorNode:
    h = HypervisorNode(
        id=uuid.uuid4(),
        connection_id=conn.id,
        node_name=f"esxi-{uuid.uuid4().hex[:4]}",
        status=status,
        cpu_total=cores,
        memory_total_gb=ram_gb,
        storage_total_gb=disk_gb,
    )
    db.add(h)
    db.flush()
    return h


def _range(db, state=RangeState.running, yaml: str = TWO_VMS) -> Range:
    t = Template(name="t", yaml=yaml, tenant_id=uuid.UUID(DEV_TENANT))
    db.add(t)
    db.flush()
    r = Range(id=uuid.uuid4(), tenant_id=uuid.UUID(DEV_TENANT), name="r", template_id=t.id)
    r.state = state
    db.add(r)
    db.flush()
    return r


def _booking(db, rng: Range | None, start: datetime, hours: float = 2, vcpu: int = 0) -> ScheduledEvent:
    ev = ScheduledEvent(
        name="b",
        state=EventState.scheduled,
        tenant_id=uuid.UUID(DEV_TENANT),
        range_id=rng.id if rng else None,
        start_time=start,
        end_time=start + timedelta(hours=hours),
        vcpu_total=vcpu,
    )
    db.add(ev)
    db.flush()
    return ev


# -- Supply ---------------------------------------------------------------------


def test_no_reporting_hosts_means_the_env_fallback(db_session):
    s = supply(db_session)
    assert (s.vcpu, s.ram_mb, s.disk_gb, s.source) == (10, 20 * 1024, 1000, "env")


def test_discovered_hosts_under_the_overcommit_policy(db_session, monkeypatch):
    monkeypatch.setenv("CLUSTER_OVERHEAD_PCT", "10")
    c = _conn(db_session)
    _host(db_session, c, cores=16, ram_gb=256, disk_gb=2000)
    _host(db_session, c, cores=16, ram_gb=256, disk_gb=2000)
    s = supply(db_session)
    assert s.source == "discovered" and s.hosts == 2
    assert s.vcpu == int(32 * 4 * 0.9)  # 4:1 against physical cores, less headroom
    assert s.ram_mb == int(512 * 1024 * 0.9)
    assert s.disk_gb == int(4000 * 0.9)


def test_a_resource_not_every_host_reports_falls_back_alone(db_session):
    c = _conn(db_session)
    _host(db_session, c, cores=8, ram_gb=64)
    _host(db_session, c, cores=8, ram_gb=None)  # vCenter REST reports no memory
    s = supply(db_session)
    assert s.source == "discovered (RAM, disk: env)"
    assert s.vcpu == 64 and s.ram_mb == 20 * 1024


def test_offline_hosts_and_inactive_connections_supply_nothing(db_session):
    _host(db_session, _conn(db_session), cores=64, ram_gb=512, disk_gb=9000, status="offline")
    _host(db_session, _conn(db_session, active=False), cores=64, ram_gb=512, disk_gb=9000)
    assert supply(db_session).source == "env"


# -- Running ranges -------------------------------------------------------------


def test_running_ranges_are_sized_from_their_templates(db_session):
    up = _range(db_session, RangeState.running)
    off = _range(db_session, RangeState.stopped)
    _range(db_session, RangeState.destroyed)
    _range(db_session, RangeState.created)
    loads = {r.range_id: r for r in running_ranges(db_session)}
    assert set(loads) == {up.id, off.id}
    assert (loads[up.id].vcpu, loads[up.id].ram_mb, loads[up.id].disk_gb) == (4, 8192, 120)
    assert (loads[off.id].vcpu, loads[off.id].ram_mb, loads[off.id].disk_gb) == (0, 0, 120)  # disks only


def test_a_range_started_without_a_booking_now_counts(db_session):
    _range(db_session)
    c = ClusterCapacity(db_session).committed(db_session, SOON, SOON + timedelta(hours=2))
    assert (c.resources.vcpu, c.events, c.ranges) == (4, 0, 1)


def test_a_booked_range_is_counted_once_through_its_booking(db_session):
    rng = _range(db_session)
    _booking(db_session, rng, SOON, vcpu=4)
    c = ClusterCapacity(db_session).committed(db_session, SOON, SOON + timedelta(hours=1))
    assert (c.resources.vcpu, c.events, c.ranges) == (4, 1, 0)


def test_a_range_kept_up_between_sessions_counts_between_them(db_session):
    rng = _range(db_session)
    _booking(db_session, rng, SOON, hours=2, vcpu=4)
    _booking(db_session, rng, SOON + timedelta(days=1), hours=2, vcpu=4)
    gap = SOON + timedelta(hours=10)
    c = ClusterCapacity(db_session).committed(db_session, gap, gap + timedelta(hours=1))
    assert (c.resources.vcpu, c.events, c.ranges) == (4, 0, 1)


def test_running_ranges_do_not_reach_back_into_the_past(db_session):
    _range(db_session)
    past = datetime.now(UTC) - timedelta(days=2)
    assert ClusterCapacity(db_session).committed(db_session, past, past + timedelta(hours=1)).ranges == 0


# -- Through the API: the overtax gap is closed -----------------------------------


@contextmanager
def acting_as(role: UserRole):
    who = CurrentUser(
        id=str(uuid.uuid4()),
        email="i@example.test",
        display_name="i",
        role=role,
        tenant_id=DEV_TENANT,
        keycloak_id="kc",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def test_a_booking_is_refused_when_unbooked_running_ranges_fill_the_cluster(client, db_session):
    _range(db_session)  # 4 of the 10 vCPU, started with no booking
    _range(db_session)  # 8 of 10
    body = {
        "name": "Too much",
        "start_time": SOON.isoformat(),
        "end_time": (SOON + timedelta(hours=2)).isoformat(),
        "vcpu_total": 4,
    }
    with acting_as(UserRole.instructor):
        r = client.post("/schedule/events", json=body)
        cap = client.get("/schedule/capacity").json()
    assert r.status_code == 409
    assert r.json()["detail"].startswith("vCPU: need 4, 2 free")
    assert cap["vcpu_committed"] == 8 and cap["supply_source"] == "env"
