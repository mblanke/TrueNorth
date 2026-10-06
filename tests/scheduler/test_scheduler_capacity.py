"""Booking against capacity (ADR 0004 slice 2).

- A booking's size comes from its template's VM specs, not typed totals.
- A booking holds capacity from its provisioning lead to its teardown grace.
- One that does not fit is refused with a reason per resource (policy `block`), or
  created with warnings that are audit-logged (policy `warn`). Only admins set the policy.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import AuditLog, Template, UserRole
from app.scheduler.models import EventState, ScheduledEvent

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"

# 3 x (4 vCPU, 8 GB, 100 GB) + 1 x worker defaults (2 vCPU, 4 GB, 60 GB); the switch is drawn, not built.
TEMPLATE_YAML = """
nodes:
  - id: ws
    os: win11
    count: 3
    specs: { cores: 4, memory_mb: 8192, disk_gb: 100 }
  - id: dc
    os: win2022
  - id: core-sw
    type: switch
"""
T_VMS, T_VCPU, T_RAM_MB, T_DISK_GB = 4, 14, 28672, 360

DAY = (datetime.now(UTC) + timedelta(days=40)).replace(hour=0, minute=0, second=0, microsecond=0)


@contextmanager
def acting_as(role: UserRole, *, tenant: str = DEV_TENANT):
    who = CurrentUser(
        id=str(uuid.uuid4()),
        email=f"{role.value}-{uuid.uuid4().hex[:6]}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=tenant,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def small_cluster(monkeypatch):
    """100 vCPU, 40 GB RAM, 1000 GB disk, no overhead: easy numbers."""
    monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "100")
    monkeypatch.setenv("CLUSTER_TOTAL_RAM_MB", str(40 * 1024))
    monkeypatch.setenv("CLUSTER_TOTAL_DISK_GB", "1000")
    monkeypatch.setenv("CLUSTER_OVERHEAD_PCT", "0")


def _template(db, *, tenant: str | None = DEV_TENANT, public: bool = False, yaml: str = TEMPLATE_YAML) -> Template:
    t = Template(name=f"t-{uuid.uuid4().hex[:6]}", yaml=yaml, tenant_id=uuid.UUID(tenant) if tenant else None)
    t.is_public = public
    db.add(t)
    db.flush()
    return t


def _hold(db, start: datetime, end: datetime, ram_mb: int) -> ScheduledEvent:
    ev = ScheduledEvent(
        name="existing",
        state=EventState.scheduled,
        tenant_id=uuid.UUID(OTHER_TENANT),
        start_time=start,
        end_time=end,
        ram_mb_total=ram_mb,
    )
    db.add(ev)
    db.flush()
    return ev


def _book(start: datetime, hours: float = 2, **extra) -> dict:
    return {
        "name": "Blue team drill",
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=hours)).isoformat(),
        **extra,
    }


def test_booking_is_sized_from_its_template_not_typed_totals(client, db_session, small_cluster):
    t = _template(db_session)
    with acting_as(UserRole.instructor):
        r = client.post(
            "/schedule/events",
            json=_book(DAY + timedelta(hours=9), template_id=str(t.id), vcpu_total=1, ram_mb_total=1),
        )
    assert r.status_code == 201, r.text
    ev = r.json()
    assert (ev["vm_count"], ev["vcpu_total"], ev["ram_mb_total"], ev["disk_gb_total"]) == (
        T_VMS,
        T_VCPU,
        T_RAM_MB,
        T_DISK_GB,
    )
    assert ev["warnings"] == []


def test_over_capacity_is_refused_with_a_reason_per_short_resource(client, db_session, small_cluster):
    t = _template(db_session)
    start = DAY + timedelta(hours=13)
    _hold(db_session, start, start + timedelta(hours=3), ram_mb=20 * 1024)
    with acting_as(UserRole.instructor):
        r = client.post("/schedule/events", json=_book(start, hours=3, template_id=str(t.id)))
    assert r.status_code == 409
    detail = r.json()["detail"]
    # 28 GB needed, 40 - 20 = 20 GB free over the held window (lead 30 min, grace 15 min).
    assert detail == f"RAM: need 28 GB, 20 GB free {DAY:%Y-%m-%d} 12:30–16:15 UTC"
    assert "vCPU" not in detail and "Disk" not in detail


@pytest.mark.parametrize(
    ("gap_minutes", "fits"),
    [(30, False), (44, False), (45, True), (90, True)],
)
def test_teardown_grace_and_provisioning_lead_hold_capacity(client, db_session, small_cluster, gap_minutes, fits):
    """An event holds [start - 30 min, end + 15 min], so back-to-back bookings need 45 minutes between them."""
    t = _template(db_session)
    end_of_existing = DAY + timedelta(hours=12)
    _hold(db_session, end_of_existing - timedelta(hours=2), end_of_existing, ram_mb=40 * 1024)
    with acting_as(UserRole.instructor):
        r = client.post(
            "/schedule/events", json=_book(end_of_existing + timedelta(minutes=gap_minutes), template_id=str(t.id))
        )
    assert r.status_code == (201 if fits else 409), r.text


def test_warn_policy_books_anyway_and_audit_logs_it(client, db_session, small_cluster):
    t = _template(db_session)
    start = DAY + timedelta(hours=9)
    _hold(db_session, start, start + timedelta(hours=2), ram_mb=40 * 1024)

    with acting_as(UserRole.admin):
        r = client.put("/schedule/policy", json={"overcapacity": "warn"})
    assert r.status_code == 200, r.text
    assert r.json() == {"overcapacity": "warn"}

    with acting_as(UserRole.instructor):
        r = client.post("/schedule/events", json=_book(start, template_id=str(t.id)))
    assert r.status_code == 201, r.text
    ev = r.json()
    assert ev["warnings"] and ev["warnings"][0].startswith("RAM: need 28 GB, 0 GB free")

    logged = {
        (a.action, a.resource_id) for a in db_session.query(AuditLog).filter(AuditLog.resource_type == "schedule").all()
    }
    assert ("update", "overcapacity_policy") in logged
    assert ("overcapacity_warning", ev["id"]) in logged


@pytest.mark.parametrize("role", [UserRole.instructor, UserRole.range_ops, UserRole.observer, UserRole.student])
def test_only_admins_set_the_policy(client, role):
    with acting_as(role):
        assert client.put("/schedule/policy", json={"overcapacity": "warn"}).status_code == 403


def test_policy_defaults_to_block_and_staff_can_read_it(client):
    with acting_as(UserRole.observer):
        r = client.get("/schedule/policy")
    assert r.status_code == 200
    assert r.json() == {"overcapacity": "block"}


def test_check_sizes_from_the_template_and_names_its_sources(client, db_session, small_cluster):
    t = _template(db_session)
    start = DAY + timedelta(hours=9)
    body = {"start_time": start.isoformat(), "end_time": (start + timedelta(hours=2)).isoformat()}
    with acting_as(UserRole.observer):
        r = client.post("/schedule/check", json={**body, "template_id": str(t.id), "vcpu_needed": 999})
    assert r.status_code == 200, r.text
    res = r.json()
    assert (res["vcpu_needed"], res["ram_mb_needed"], res["disk_gb_needed"]) == (T_VCPU, T_RAM_MB, T_DISK_GB)
    assert res["vm_count_needed"] == T_VMS
    assert res["fits"] is True and res["reasons"] == []
    assert res["supply_source"] == "env"
    assert res["policy"] == "block"
    assert res["vcpu_total"] == 100


def test_another_tenants_private_template_is_not_found(client, db_session, small_cluster):
    theirs = _template(db_session, tenant=OTHER_TENANT)
    shared = _template(db_session, tenant=OTHER_TENANT, public=True)
    with acting_as(UserRole.instructor):
        assert client.post("/schedule/events", json=_book(DAY, template_id=str(theirs.id))).status_code == 404
        assert client.post("/schedule/events", json=_book(DAY, template_id=str(shared.id))).status_code == 201


def test_a_template_that_declares_no_vms_cannot_size_a_booking(client, db_session, small_cluster):
    t = _template(db_session, yaml="name: empty\n")
    with acting_as(UserRole.instructor):
        r = client.post("/schedule/events", json=_book(DAY, template_id=str(t.id)))
    assert r.status_code == 422


def test_a_time_without_a_zone_is_taken_as_utc(client, small_cluster):
    naive_start = (DAY + timedelta(hours=9)).replace(tzinfo=None)
    with acting_as(UserRole.instructor):
        r = client.post(
            "/schedule/events",
            json={
                "name": "naive",
                "start_time": naive_start.isoformat(),
                "end_time": (DAY + timedelta(hours=11)).isoformat(),
            },
        )
    assert r.status_code == 201, r.text


def test_timeline_at_15_minutes_does_not_count_back_to_back_sessions_as_concurrent(client, db_session, small_cluster):
    morning = DAY + timedelta(hours=9)
    _hold(db_session, morning, morning + timedelta(hours=3), ram_mb=30 * 1024)  # held 08:30-12:15
    afternoon = DAY + timedelta(hours=13)
    _hold(db_session, afternoon, afternoon + timedelta(hours=3), ram_mb=30 * 1024)  # held 12:30-16:15
    with acting_as(UserRole.observer):
        hourly = client.get("/schedule/timeline", params={"start": DAY.isoformat(), "days": 1}).json()
        fine = client.get(
            "/schedule/timeline", params={"start": DAY.isoformat(), "days": 1, "resolution_minutes": 15}
        ).json()
    peak = lambda t: max(b["ram_mb_committed"] for b in t["buckets"])  # noqa: E731
    assert peak(hourly) == 60 * 1024  # the 12:00 hour touches both
    assert peak(fine) == 30 * 1024  # no 15-minute slot does
    assert len(fine["buckets"]) == 96
    assert (fine["lead_minutes"], fine["grace_minutes"], fine["resolution_minutes"]) == (30, 15, 15)


def test_timeline_series_matches_committed_slot_by_slot(db_session, small_cluster):
    from app.scheduler.capacity import EnvCapacity

    _hold(db_session, DAY + timedelta(hours=9), DAY + timedelta(hours=11), ram_mb=1024)
    _hold(db_session, DAY + timedelta(hours=10), DAY + timedelta(hours=14), ram_mb=2048)
    p, step = EnvCapacity(), timedelta(minutes=30)
    series = p.committed_series(db_session, DAY, DAY + timedelta(hours=18), step)
    for i, c in enumerate(series):
        assert c == p.committed(db_session, DAY + i * step, DAY + (i + 1) * step), i


@pytest.mark.parametrize("params", [{"resolution_minutes": 7}, {"days": 90, "resolution_minutes": 15}])
def test_timeline_refuses_bad_resolutions(client, params):
    with acting_as(UserRole.observer):
        assert client.get("/schedule/timeline", params=params).status_code == 422


def test_times_with_an_offset_are_stored_and_returned_in_utc(client, db_session, small_cluster):
    """On SQLite the offset used to be dropped: 09:00-04:00 was stored as 09:00 and
    returned without a zone, so capacity windows were four hours off."""
    from datetime import timezone

    eastern = timezone(timedelta(hours=-4))
    start = (DAY + timedelta(hours=13)).astimezone(eastern)  # 09:00-04:00
    with acting_as(UserRole.instructor):
        ev = client.post(
            "/schedule/events",
            json={"name": "tz", "start_time": start.isoformat(), "end_time": (start + timedelta(hours=1)).isoformat()},
        ).json()
    returned = datetime.fromisoformat(ev["start_time"])
    assert returned.tzinfo is not None
    assert returned == DAY + timedelta(hours=13)
    row = db_session.get(ScheduledEvent, uuid.UUID(ev["id"]))
    db_session.refresh(row)
    assert row.start_time.replace(tzinfo=UTC) == DAY + timedelta(hours=13)
