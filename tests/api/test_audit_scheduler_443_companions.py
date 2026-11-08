"""Companion oracles for the confirmed audit findings of tracking issue #443 in the area
"scheduler, lab sessions and capacity" (card TN10X-AUDIT-tests-scheduler).

One companion per strict-xfail oracle in ``test_audit_scheduler_443.py``: a second input
that pins the same secure behaviour from another angle — a second deactivation path, a
second staff role, the page the token is handed to, a two-tenant matrix, the queue's own
re-decide. Each is OBSERVED to fail at the base commit 259d84b2 for the same reason as its
oracle, and is marked ``audit443`` and ``xfail(strict=True)`` with the finding named
(file:line). The originals are in ``test_audit_scheduler_443.py``; this file never repeats
one of its inputs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.classes.models import Class, ClassSession, RollCallEntry, RollCallStatus
from app.lab_sessions import capacity_gate, service, tokens
from app.models import Range, RangeState, Template, Tenant, User, UserRole
from app.scheduler.models import EventState, ScheduledEvent
from test_lab_sessions import act, lab, launch, student, until, worker  # noqa: F401 — fixtures/helpers

# The worker's default template demand (tests/scheduler/test_capacity_service.py): 2 VMs,
# 4 vCPU, 8 GB, 120 GB.
TWO_VMS = "nodes:\n  - id: ws\n    os: win11\n    count: 2\n"
DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
SOON = (datetime.now(UTC) + timedelta(days=2)).replace(hour=13, minute=0, second=0, microsecond=0)


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: Lab access token keeps working after the "
                                        "Student's account is deactivated (control-plane/api/app/routers/lab_sessions.py:151"
                                        " at the audit base) — companion: the soft-deleted owner")
def test_a_lab_token_stops_working_when_its_owner_is_soft_deleted(lab, db_session, worker):  # noqa: F811
    """Companion to test_a_lab_token_stops_working_when_its_owner_is_deactivated (audit443
    HIGH finding, the ``_token_session`` path — control-plane/api/app/routers/lab_sessions.py:151
    at the audit base): the boundary of the
    owner's account state the token path must consult — not only ``is_active=False`` but the
    other recorded deactivation, the soft delete (``deleted_at``, models.py:113-125), which
    leaves ``is_active`` True. The LTI session hand-off refuses both
    (lti_identity/session.py:165-170: ``not user.is_active or user.deleted_at is not None``);
    ``_token_session`` loads no ``User`` row at all, so a soft-deleted owner's token keeps
    working today. A deleted owner must be refused on the token path like a deactivated one."""
    client, rid, _ = lab
    s = student(db_session)
    a, _ = launch(db_session, s, rid)
    until(db_session, a, "ready")
    token = tokens.mint(db_session, a.id, s.id, a.max_expires_at)
    s.deleted_at = datetime.now(UTC)  # soft delete; is_active stays True
    db_session.commit()
    resp = client.get(f"/lab-access/{a.id}", headers={"X-Lab-Token": token})
    assert resp.status_code == 403, ("read", resp.status_code, resp.text)
    resp = client.post(f"/lab-access/{a.id}/heartbeat", headers={"X-Lab-Token": token}, json={"active": True})
    assert resp.status_code == 403, ("heartbeat", resp.status_code, resp.text)
    resp = client.get(f"/lab-access/{a.id}/credentials", headers={"X-Lab-Token": token})
    assert resp.status_code == 403, ("credentials", resp.status_code, resp.text)


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: Staff opening the console touches the "
                                        "Student's lab, idle clock and ACTIVE state (control-plane/api/app/lab_sessions/service.py:1276)"
                                        " — companion: the second staff role")
def test_an_admin_opening_the_console_does_not_touch_the_students_lab(lab, db_session):  # noqa: F811
    """Companion to test_staff_opening_the_console_does_not_touch_the_students_lab (audit443
    LOW finding, control-plane/api/app/lab_sessions/service.py:1276): the second staff role.
    The unconditional ``touch`` runs outside every branch of ``service.console``, so it fires
    for whoever the staff grant names — an admin (``UserRole.admin`` is every permission,
    rbac.py:190-192) exactly as an instructor. An admin opening the console must leave the
    Student's lab untouched: not ACTIVE, idle clock unmoved."""
    from app.auth import CurrentUser, get_current_user
    from app.main import app as fastapi_app

    client, rid, _ = lab
    s = student(db_session)
    a, _ = launch(db_session, s, rid)
    until(db_session, a, "ready")
    db_session.commit()
    before_state, before_idle = a.state, a.idle_expires_at
    admin = User(email=f"a{uuid.uuid4().hex[:6]}@example.test", display_name="A", role=UserRole.admin,
                 tenant_id=s.tenant_id, keycloak_id=f"kc-{uuid.uuid4().hex[:8]}")
    db_session.add(admin)
    db_session.commit()
    who = CurrentUser(id=str(admin.id), email=admin.email, display_name="A", role=UserRole.admin,
                      tenant_id=str(admin.tenant_id), keycloak_id=admin.keycloak_id)
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        assert client.post(f"/lab-sessions/{a.id}/console").status_code == 200
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
    db_session.refresh(a)
    assert (a.state, a.idle_expires_at) == (before_state, before_idle), (a.state, a.idle_expires_at)


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: Start lab during the lab's teardown hands the "
                                        "browser an already-expired token (control-plane/api/app/routers/integrations.py:948)"
                                        " — companion: the page the token is handed to")
def test_the_token_handed_out_during_teardown_still_opens_the_lab_page(lab, db_session, worker):  # noqa: F811
    """Companion to test_a_start_lab_during_teardown_hands_a_token_that_still_opens_the_page
    (audit443 LOW finding, control-plane/api/app/routers/integrations.py:948): the other side
    of the hand-over — not what ``tokens.verify`` accepts but the page the browser actually
    opens with the token Start lab gave it. ``_launch_lab`` mints from the past
    ``max_expires_at``; the page's first call, the read of ``/lab-access/<id>`` (the
    ``_token_session`` path with ``act=False``), must open it — reading an ended lab stays
    allowed exactly so the page can say it ended (routers/lab_sessions.py:152-160) — instead
    of 401 "This lab link has expired"."""
    from app.routers.integrations import _launch_lab

    client, rid, course_id = lab
    s = student(db_session)
    a, _ = launch(db_session, s, rid)
    until(db_session, a, "ready")
    a.max_expires_at = datetime.now(UTC) - timedelta(hours=1)  # its lifetime has passed
    db_session.commit()
    worker.hold = True  # the destroy task is sent but not applied yet: teardown in progress
    act(db_session, service.advance, a)
    assert a.state in service.ENDING and a.state not in service.TERMINAL, (a.state, a.error)
    target = _launch_lab(db_session, s, f"{course_id}:mod_006")
    assert uuid.UUID(target.split("/labs/")[1].split("?")[0]) == a.id  # resumed, not attempt 2
    token = target.rpartition("#token=")[2]
    resp = client.get(f"/lab-access/{a.id}", headers={"X-Lab-Token": token})
    assert resp.status_code == 200, ("read", resp.status_code, resp.text)  # 401 today: expired


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: A range whose destroy is still running holds "
                                        "no capacity (control-plane/api/app/capacity/service.py:125"
                                        " at the audit base) — companion: two tenants")
def test_destroying_ranges_of_two_tenants_still_commit(db_session, monkeypatch):
    """Companion to test_a_range_being_destroyed_still_counts_as_committed (audit443 LOW
    finding, the ``running_ranges`` state filter — control-plane/api/app/capacity/service.py:125
    at the audit base): the two-tenant matrix. A
    destroying range holds its VMs whatever tenant it belongs to, and ``running_ranges``
    counts no tenant at all — so until ``destroyed``, each tenant's destroying range must
    commit its template's size: two of them, 8 vCPU across the two tenants, not the zero the
    window counts today."""
    for key, value in {
        "CLUSTER_TOTAL_VCPU": "20",
        "CLUSTER_TOTAL_RAM_MB": str(40 * 1024),
        "CLUSTER_TOTAL_DISK_GB": "2000",
        "CLUSTER_OVERHEAD_PCT": "0",
        "CAPACITY_VCPU_RATIO": "4",
    }.items():
        monkeypatch.setenv(key, value)
    other = Tenant(name=f"Other {uuid.uuid4().hex[:8]}", slug=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    committed = []
    for tenant_id in (DEV_TENANT, other.id):
        template = Template(name=f"t-{uuid.uuid4().hex[:8]}", yaml=TWO_VMS, tenant_id=tenant_id)
        db_session.add(template)
        db_session.flush()
        rng = Range(id=uuid.uuid4(), tenant_id=tenant_id, name="r", template_id=template.id)
        rng.state = RangeState.destroying
        db_session.add(rng)
        db_session.flush()
        from app.scheduler.capacity import ClusterCapacity

        committed.append(ClusterCapacity(db_session).committed(db_session, SOON, SOON + timedelta(hours=2)))
    db_session.commit()
    # Each tenant's destroying range commits its own template's size, on its own and beside
    # the other tenant's. ClusterCapacity is cluster-wide — ``running_ranges`` has no tenant
    # scope (capacity/service.py:112) and ``_sum`` adds every range it returned
    # (scheduler/capacity.py:191-210) — so the second snapshot, taken with both destroying
    # ranges in the db, reads both of them: 8 vCPU across the two tenants.
    assert (committed[0].resources.vcpu, committed[0].events, committed[0].ranges) == (4, 0, 1), \
        (committed[0].resources.vcpu, committed[0].events, committed[0].ranges)
    assert (committed[1].resources.vcpu, committed[1].events, committed[1].ranges) == (8, 0, 2), \
        (committed[1].resources.vcpu, committed[1].events, committed[1].ranges)


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: A class's own lab queues behind a self-serve "
                                        "lab that is itself waiting for site capacity (control-plane/api/app/lab_sessions/service.py:412)"
                                        " — companion: the queue's own re-decide")
def test_the_queue_lets_a_classs_own_lab_through_when_it_redecides(lab, db_session, worker, monkeypatch):  # noqa: F811
    """Companion to test_a_classs_own_lab_starts_beside_a_queued_self_serve_lab (audit443
    MEDIUM finding, control-plane/api/app/lab_sessions/service.py:412): the other entry into
    the FIFO check. A class Student whose launch was itself refused queues too, and the queue
    re-decides every queued lab (``advance`` -> ``_start``, service.py:726-731). There the
    same FIFO check counts the older queued self-serve lab — waiting for the site, holding
    nothing — and keeps the class's own lab waiting "in turn" although it draws on its live
    class's hold and needs no site capacity. The re-decide must let it through as the launch
    must."""
    _, rid, course_id = lab
    # 1. a self-serve lab queued behind the tenant quota; the quota is lifted while the site
    #    is full -> it waits in the queue for site capacity (and holds nothing).
    monkeypatch.setenv("LAB_MAX_SESSIONS_PER_TENANT", "0")
    a, _ = launch(db_session, student(db_session), rid)
    assert a.state == "queued"
    monkeypatch.setenv("LAB_MAX_SESSIONS_PER_TENANT", "1000")
    lead = User(email=f"lead{uuid.uuid4().hex[:6]}@example.test", display_name="L",
                role=UserRole.instructor, tenant_id=DEV_TENANT, keycloak_id=f"kc-{uuid.uuid4().hex[:8]}")
    db_session.add(lead)
    db_session.flush()
    today = datetime.now(UTC).date()
    cls = Class(tenant_id=DEV_TENANT, name="Scratch class", course_id=course_id,
                start_date=today, end_date=today + timedelta(days=1), lead_instructor_id=lead.id)
    db_session.add(cls)
    db_session.flush()
    start = datetime.now(UTC) - timedelta(minutes=10)  # live now (own_booking's window)
    _booking = ScheduledEvent(
        name="A full site elsewhere", tenant_id=DEV_TENANT, state=EventState.scheduled,
        start_time=start, end_time=start + timedelta(hours=3), vcpu_total=10,
        ram_mb_total=1048576, disk_gb_total=100000,
    )
    db_session.add(_booking)
    # 2. the class's session of the lab's course is live now and holds its Students' labs;
    #    a Student on its roll call draws on that hold and needs no site capacity.
    evt = ScheduledEvent(
        name="Scratch session", tenant_id=DEV_TENANT, state=EventState.scheduled,
        start_time=start, end_time=start + timedelta(hours=3), course_id=course_id,
        vcpu_total=8, ram_mb_total=4096, disk_gb_total=100,
    )
    db_session.add(evt)
    db_session.flush()
    db_session.add(ClassSession(class_id=cls.id, event_id=evt.id, tenant_id=DEV_TENANT,
                                range_mode="individual"))
    b = student(db_session)
    db_session.add(RollCallEntry(class_id=cls.id, tenant_id=DEV_TENANT, email=b.email.lower(),
                                 user_id=b.id, status=RollCallStatus.signed_in.value))
    db_session.commit()
    # 3. the class's Student queues too (the quota is back down), then is lifted for good:
    #    both labs now sit in the queue, the older one waiting for the site.
    monkeypatch.setenv("LAB_MAX_SESSIONS_PER_TENANT", "0")
    b_session, _ = launch(db_session, b, rid)
    assert b_session.state == "queued"
    monkeypatch.setenv("LAB_MAX_SESSIONS_PER_TENANT", "1000")
    act(db_session, service.advance, a)
    assert a.state == "queued" and capacity_gate.BUSY in a.error, (a.state, a.error)
    # 4. the queue's own re-decide of the class's lab: it can start now (its class's hold,
    #    no site capacity needed) and must not be kept waiting by the lab that cannot.
    act(db_session, service.advance, b_session)
    assert b_session.state == "provisioning", (b_session.state, b_session.error)
