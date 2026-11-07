"""Strict-xfail oracle tests for audit #443 (TN10X-AUDIT2-tests-scheduler).

One test per finding of audit #443 that reproduces at the base commit: each test below
fails at the base on its final assertion and is marked ``xfail(strict=True)`` with the
finding in the reason, plus ``pytest.mark.audit443`` (the integration decisions from
#444-#450). A fix that delivers the stated behaviour turns the xfail into an XPASS and
fails the suite. The ``audit443`` mark is deliberately not registered (pytest.ini is
protected): the PytestUnknownMarkWarning is expected.

Findings that could not be given an unambiguous oracle have no test here; they are
reported as UNKNOWN in the card's return block.
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

# Helpers act/launch/student/until are used directly; the lab/worker fixtures are imported
# only so pytest registers them here (tests fetch them by name, so their parameters do not
# shadow these module names).
from test_lab_sessions import act, lab, launch, student, until, worker  # noqa: F401

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
TWO_VMS = "nodes:\n  - id: ws\n    os: win11\n    count: 2\n"  # 4 vCPU, 8 GB, 120 GB


@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: tenant FIFO queue puts a class's own lab behind one self-serve lab "
    "waiting for site capacity (control-plane/api/app/lab_sessions/service.py:412)",
)
def test_a_class_own_lab_is_not_queued_behind_a_self_serve_lab_waiting_for_capacity(
    request, db_session, monkeypatch
):
    """A Student on the roll call of a live class session draws on that session's hold
    (capacity_gate.own_booking) and is "not gated" (ADR 0014 §18): the class's capacity is
    already reserved. It must not wait its turn behind an older self-serve lab that is
    itself only waiting for site capacity and holds nothing."""
    _, rid, course_id = request.getfixturevalue("lab")
    _ = request.getfixturevalue("worker")
    monkeypatch.setenv("CLUSTER_OVERHEAD_PCT", "0")
    monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "10")
    monkeypatch.setenv("CLUSTER_TOTAL_RAM_MB", "1048576")
    monkeypatch.setenv("CLUSTER_TOTAL_DISK_GB", "100000")
    monkeypatch.setenv("LAB_MAX_SESSIONS_PER_TENANT", "0")
    older, _ = launch(db_session, student(db_session), rid)
    assert older.state == "queued"
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
    # Holds 9 of the 10 vCPU for its Students' labs; the 2-vCPU self-serve lab no longer fits.
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

    # The scenario of the finding: the older lab is queued waiting for site capacity.
    act(db_session, service.advance, older)
    assert older.state == "queued" and "Labs are busy" in (older.error or ""), (older.state, older.error)

    b, _ = launch(db_session, s, rid)
    assert b.state == "provisioning", (b.state, b.error)


@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: staff opening a Student's lab console restarts the Student's idle "
    "clock and marks the lab active (control-plane/api/app/lab_sessions/service.py:1276)",
)
def test_staff_opening_the_console_does_not_touch_the_students_lab(request, db_session):
    """A staff console grant is not Student activity: the idle clock and the active state
    belong to the Student (models.ACTIVE: "the student has opened it"). Issuing a grant to
    staff leaves both alone; an open connection keeping the lab alive is ADR 0014 §11's
    separate, sweep-side rule."""
    from app.auth import CurrentUser, get_current_user
    from app.main import app as fastapi_app

    _ = request.getfixturevalue("worker")
    client, rid, _ = request.getfixturevalue("lab")
    s = student(db_session)
    a, _ = launch(db_session, s, rid)
    until(db_session, a, "ready")
    db_session.commit()
    before_idle = a.idle_expires_at

    teacher = User(
        id=uuid.uuid4(),
        email="t@example.test",
        display_name="T",
        role=UserRole.instructor,
        tenant_id=s.tenant_id,
        keycloak_id="kc-teach",
    )
    db_session.add(teacher)
    db_session.commit()
    who = CurrentUser(
        id=str(teacher.id),
        email=teacher.email,
        display_name="T",
        role=UserRole.instructor,
        tenant_id=str(teacher.tenant_id),
        keycloak_id=teacher.keycloak_id,
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        assert client.post(f"/lab-sessions/{a.id}/console").status_code == 200
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)

    db_session.expire_all()
    db_session.refresh(a)
    assert a.state == "ready", a.state
    assert a.idle_expires_at == before_idle


@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: Moodle 'Start lab' during an expired lab's teardown hands the "
    "Student an already-expired token (control-plane/api/app/routers/integrations.py:948)",
)
def test_start_lab_during_a_teardown_hands_a_token_the_lab_page_can_use(request, db_session):
    """A "Start lab" click while the Student's expired lab is being torn down resumes that
    still-non-terminal session and must hand the browser a token the lab page can use (or
    no token). A token minted from the session's already-past maximum lifetime 401s on the
    page's very first call instead of showing the ended-lab state."""
    from app.routers.integrations import _launch_lab

    _ = request.getfixturevalue("worker")
    _, rid, course_id = request.getfixturevalue("lab")
    s = student(db_session)
    a, _ = launch(db_session, s, rid)
    until(db_session, a, "ready")
    worker.hold = True  # the destroy task is sent but not applied: teardown in progress
    a.max_expires_at = datetime.now(UTC) - timedelta(hours=1)
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
    "admitted while the previous range's VMs still exist (control-plane/api/app/capacity/service.py:117)",
)
def test_a_range_being_destroyed_still_counts_as_running_load(db_session):
    """Its VMs exist until the worker records ``destroyed`` (ADR 0006: a range holds its
    size until it is destroyed): a range in ``destroying`` holds its template's size, or a
    self-serve lab is admitted over it."""
    from app.capacity import running_ranges

    t = Template(name="t", yaml=TWO_VMS, tenant_id=DEV_TENANT)
    db_session.add(t)
    db_session.flush()
    rng = Range(id=uuid.uuid4(), tenant_id=DEV_TENANT, name="r", template_id=t.id)
    rng.state = RangeState.destroying
    db_session.add(rng)
    db_session.commit()

    loads = {row.range_id: row for row in running_ranges(db_session)}
    assert rng.id in loads, sorted(loads)
    assert (loads[rng.id].vcpu, loads[rng.id].ram_mb, loads[rng.id].disk_gb) == (4, 8192, 120)


@pytest.mark.audit443
@pytest.mark.xfail(
    strict=True,
    reason="audit #443: the lab sweep runs once per uvicorn worker; the sweep-level lease "
    "the runner docstring promises does not exist (control-plane/api/app/lab_sessions/runner.py:5)",
)
def test_a_sweep_pass_takes_a_lease_on_the_sweep(monkeypatch):
    """runner.py's docstring: "Each pass takes a lease on the sweep itself (one API process
    sweeps at a time)". A second pass started while the first is inside the sweep must not
    enter it too — whether a fix blocks it or skips it."""
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
    try:
        deadline = time.monotonic() + 1.0  # a fair chance to (wrongly) enter, no long wait
        while time.monotonic() < deadline and len(events) < 2:
            time.sleep(0.01)
        assert events == ["enter"], events
    finally:
        release.set()
        t1.join(timeout=5)
        t2.join(timeout=5)
