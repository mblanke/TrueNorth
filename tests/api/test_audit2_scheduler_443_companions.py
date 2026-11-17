"""Companion oracles for audit #443 (TN10X-AUDIT2-tests-scheduler), card #469.

One companion per strict-xfail oracle in ``test_audit2_scheduler_443.py``: a second
input that pins the same secure behaviour from another angle (a second role, a
two-tenant matrix, or a boundary value). Each fails at the base for the same reason
as its oracle and is marked ``xfail(strict=True)`` with the finding, plus
``pytest.mark.audit443`` (deliberately not registered, like the oracle file's mark).
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.classes.models import Class, ClassSession, RollCallEntry, RollCallStatus
from app.lab_sessions import service, tokens
from app.models import Range, RangeState, Template, User, UserRole
from app.scheduler.models import EventState, ScheduledEvent

# Fixtures are imported so pytest registers them here; the tests fetch them by name.
from test_lab_sessions import act, lab, launch, student, until, worker  # noqa: F401

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
SECOND_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000002")
ONE_VM = "nodes:\n  - id: ws\n    os: win11\n    count: 1\n"  # 2 vCPU, 4 GB, 60 GB


@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: tenant FIFO queue puts a class's own lab behind one self-serve lab "
    "waiting for site capacity (control-plane/api/app/lab_sessions/service.py:412) — companion",
)
def test_an_instructors_own_queued_lab_does_not_gate_a_class_students_lab(
    request, db_session, monkeypatch
):
    """Second role: the older self-serve waiter is an instructor's own lab, not a
    Student's. The FIFO check counts every queued lab of the tenant regardless of the
    owner's role, so the class Student's own-booking lab (ADR 0014 §18) is still held
    behind a staff lab that holds nothing and is itself only waiting for site capacity."""
    _, rid, course_id = request.getfixturevalue("lab")
    _ = request.getfixturevalue("worker")
    monkeypatch.setenv("CLUSTER_OVERHEAD_PCT", "0")
    monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "10")
    monkeypatch.setenv("CLUSTER_TOTAL_RAM_MB", "1048576")
    monkeypatch.setenv("CLUSTER_TOTAL_DISK_GB", "100000")
    monkeypatch.setenv("LAB_MAX_SESSIONS_PER_TENANT", "0")
    teacher = User(
        email=f"t{uuid.uuid4().hex[:6]}@example.test",
        display_name="T",
        role=UserRole.instructor,
        tenant_id=DEV_TENANT,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(teacher)
    db_session.flush()
    older, _ = launch(db_session, teacher, rid)
    assert older.state == "queued", (older.state, older.error)
    older.created_at = datetime.now(UTC) - timedelta(minutes=5)  # it is the older waiter
    db_session.commit()
    monkeypatch.setenv("LAB_MAX_SESSIONS_PER_TENANT", "1000")

    s = student(db_session)
    lead = User(
        email=f"lead{uuid.uuid4().hex[:6]}@example.test",
        display_name="L",
        role=UserRole.instructor,
        tenant_id=s.tenant_id,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(lead)
    db_session.flush()
    today = datetime.now(UTC).date()
    cls = Class(
        tenant_id=s.tenant_id,
        name="Scratch class",
        course_id=course_id,
        start_date=today,
        end_date=today + timedelta(days=1),
        lead_instructor_id=lead.id,
    )
    db_session.add(cls)
    db_session.flush()
    start = datetime.now(UTC) - timedelta(minutes=10)  # live now: its hold is drawn on
    # Holds 9 of the 10 vCPU for its Students' labs; the 2-vCPU instructor lab no longer fits.
    evt = ScheduledEvent(
        name="Scratch session",
        tenant_id=s.tenant_id,
        state=EventState.scheduled,
        start_time=start,
        end_time=start + timedelta(hours=3),
        course_id=course_id,
        vcpu_total=9,
        ram_mb_total=2048,
        disk_gb_total=40,
    )
    db_session.add(evt)
    db_session.flush()
    db_session.add(ClassSession(class_id=cls.id, event_id=evt.id, tenant_id=s.tenant_id, range_mode="individual"))
    db_session.add(
        RollCallEntry(
            class_id=cls.id,
            tenant_id=s.tenant_id,
            email=s.email.lower(),
            user_id=s.id,
            status=RollCallStatus.signed_in.value,
        )
    )
    db_session.commit()

    # The scenario of the finding: the older instructor lab is queued waiting for site capacity.
    act(db_session, service.advance, older)
    assert older.state == "queued" and "Labs are busy" in (older.error or ""), (older.state, older.error)

    b, _ = launch(db_session, s, rid)
    assert b.state == "provisioning", (b.state, b.error)


@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: staff opening a Student's lab console restarts the Student's idle "
    "clock and marks the lab active (control-plane/api/app/lab_sessions/service.py:1276) — companion",
)
def test_an_admin_opening_the_console_does_not_extend_the_students_idle_clock(
    request, db_session
):
    """Second role: an admin (not an instructor) opens the Student's console. A staff
    grant is not Student activity: the Student's idle clock belongs to the Student, so
    issuing the grant must leave ``idle_expires_at`` alone (models.ACTIVE: "the student
    has opened it"). This pins the clock, which the instructor oracle cannot reach: it
    fails one assertion earlier, on the active state."""
    from app.auth import CurrentUser, get_current_user
    from app.main import app as fastapi_app

    _ = request.getfixturevalue("worker")
    client, rid, _ = request.getfixturevalue("lab")
    s = student(db_session)
    a, _ = launch(db_session, s, rid)
    until(db_session, a, "ready")
    db_session.commit()
    before_idle = a.idle_expires_at

    admin = User(
        id=uuid.uuid4(),
        email="a@example.test",
        display_name="A",
        role=UserRole.admin,
        tenant_id=s.tenant_id,
        keycloak_id="kc-admin",
    )
    db_session.add(admin)
    db_session.commit()
    who = CurrentUser(
        id=str(admin.id),
        email=admin.email,
        display_name="A",
        role=UserRole.admin,
        tenant_id=str(admin.tenant_id),
        keycloak_id=admin.keycloak_id,
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        assert client.post(f"/lab-sessions/{a.id}/console").status_code == 200
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)

    db_session.expire_all()
    db_session.refresh(a)
    assert a.idle_expires_at == before_idle, (a.idle_expires_at, before_idle)
    assert a.state == "ready", a.state


@pytest.mark.audit443
def test_start_lab_during_a_teardown_hands_a_usable_token_at_the_just_expired_boundary(
    request, db_session
):
    """Boundary value: the lab's maximum lifetime lapsed one second ago — the tightest
    expiry that is already past. Resuming the teardown-bound session must still hand the
    browser a token the lab page can use; one minted from that just-past lifetime fails
    verification on the page's very first call, exactly as with a long-past expiry."""
    from app.routers.integrations import _launch_lab

    _ = request.getfixturevalue("worker")
    _, rid, course_id = request.getfixturevalue("lab")
    s = student(db_session)
    a, _ = launch(db_session, s, rid)
    until(db_session, a, "ready")
    worker.hold = True  # the destroy task is sent but not applied: teardown in progress
    a.max_expires_at = datetime.now(UTC) - timedelta(seconds=1)  # just expired
    db_session.commit()
    act(db_session, service.advance, a)
    assert a.state in service.ENDING and a.state not in service.TERMINAL, (a.state, a.error)

    target = _launch_lab(db_session, s, f"{course_id}:mod_006")
    assert uuid.UUID(target.split("/labs/")[1].split("?")[0]) == a.id  # resumed, not attempt 2
    token = target.rpartition("#token=")[2]
    claims = tokens.verify(db_session, token, a.id)  # what the lab page's first read runs
    assert claims["sub"] == str(a.id)


@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: ranges in 'destroying' are not counted as running load, so a lab is "
    "admitted while the previous range's VMs still exist (control-plane/api/app/capacity/service.py:117) — companion",
)
def test_a_destroying_range_of_a_second_tenant_still_counts_as_running_load(db_session):
    """Two-tenant matrix: site capacity is one cluster, so a range being destroyed holds
    its template's size whatever tenant owns it — a lab of any tenant is admitted over
    its live VMs otherwise (ADR 0006: a range holds its size until it is destroyed)."""
    from app.capacity import running_ranges
    from app.models import Tenant

    db_session.add(Tenant(id=SECOND_TENANT, name="second", slug="second"))
    db_session.flush()
    t = Template(name="t2", yaml=ONE_VM, tenant_id=SECOND_TENANT)
    db_session.add(t)
    db_session.flush()
    rng = Range(id=uuid.uuid4(), tenant_id=SECOND_TENANT, name="r2", template_id=t.id)
    rng.state = RangeState.destroying
    db_session.add(rng)
    db_session.commit()

    loads = {row.range_id: row for row in running_ranges(db_session)}
    assert rng.id in loads, sorted(loads)
    assert (loads[rng.id].vcpu, loads[rng.id].ram_mb, loads[rng.id].disk_gb) == (2, 4096, 60)


@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: the lab sweep runs once per uvicorn worker; the sweep-level lease "
    "the runner docstring promises does not exist (control-plane/api/app/lab_sessions/runner.py:5) — companion",
)
def test_a_third_concurrent_sweep_pass_does_not_enter_the_sweep(monkeypatch):
    """Boundary value: three passes, not two. Without a lease on the sweep every pass
    enters, so a third pass started while the first is inside the sweep enters too —
    the same missing lease the two-pass oracle pins, at the next queue depth."""
    from app.lab_sessions import runner

    class _FakeDB:
        def close(self):
            pass

    events: list[str] = []
    inside, release = threading.Event(), threading.Event()

    def slow_sweep(db):
        events.append("enter")
        inside.set()
        release.wait(timeout=5)
        events.append("exit")
        return 0

    monkeypatch.setattr("app.lab_sessions.service.sweep", slow_sweep)
    monkeypatch.setattr("app.db.SessionLocal", lambda: _FakeDB())

    t1 = threading.Thread(target=runner.sweep_once)
    t1.start()
    assert inside.wait(timeout=5)
    t2 = threading.Thread(target=runner.sweep_once)
    t2.start()
    t3 = threading.Thread(target=runner.sweep_once)
    t3.start()
    try:
        deadline = time.monotonic() + 1.0  # a fair chance to (wrongly) enter, no long wait
        while time.monotonic() < deadline and len(events) < 3:
            time.sleep(0.01)
        assert events == ["enter"], events
    finally:
        release.set()
        t1.join(timeout=5)
        t2.join(timeout=5)
        t3.join(timeout=5)
