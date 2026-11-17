"""Failing oracles for the confirmed audit findings of tracking issue #443 in the area
"scheduler, lab sessions and capacity" (card TN10X-AUDIT-tests-scheduler).

Every test here is marked ``audit443`` and ``xfail(strict=True)`` — it is OBSERVED to
fail at the base commit 280a838b, and each asserts the correct behaviour its finding's
fix must deliver, naming the finding (file:line, title) in its docstring. One finding
was already fixed at the base and gets a note, not a test:

* FIXED in #442 (54dca7af + bcff2c74): "a console closes when its sign-in token expires"
  (console_relay/router.py:424 at the audit base). The token is now checked at connect
  only; the real policy re-reads the account (active, not deleted, current role) at every
  renewal; an 8 h ``MAX_OPEN_SECONDS`` ceiling backs a sign-out at the identity provider.
  Covered at the base by ``test_the_console_outlives_the_sign_in_token_it_was_opened_with``
  (tests/api/test_console_relay.py) and
  ``test_a_revocation_still_cuts_a_console_after_its_sign_in_token_expired``
  (tests/api/test_class_consoles_321.py); both pass here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.classes.models import Class, ClassSession, RollCallEntry, RollCallStatus
from app.lab_sessions import capacity_gate, service, tokens
from app.models import Range, RangeState, Template, User, UserRole
from app.scheduler.capacity import ClusterCapacity
from app.scheduler.models import EventState, ScheduledEvent
from test_lab_sessions import act, lab, launch, student, until, worker  # noqa: F401 — fixtures/helpers

# The worker's default template demand (tests/scheduler/test_capacity_service.py): 2 VMs,
# 4 vCPU, 8 GB, 120 GB.
TWO_VMS = "nodes:\n  - id: ws\n    os: win11\n    count: 2\n"
DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
SOON = (datetime.now(UTC) + timedelta(days=2)).replace(hour=13, minute=0, second=0, microsecond=0)


@pytest.mark.audit443
def test_a_lab_token_stops_working_when_its_owner_is_deactivated(lab, db_session, worker):  # noqa: F811 — the lab fixtures, imported from test_lab_sessions
    """audit443 HIGH finding, control-plane/api/app/routers/lab_sessions.py:146 (at the audit
    base; the ``_token_session`` path, :151 here): "Lab access token keeps working after the
    Student's account is deactivated (AD sync or admin delete)".

    ``_token_session`` verifies the JWT, matches ``uid`` to the session's owner and calls
    ``service.enrollment_closed`` — it never loads the owner's ``User`` row, so
    ``is_active=False`` / ``deleted_at`` are not consulted. Both deactivation paths set only
    ``is_active=False`` and keep the enrolment (routers/admin.py:257; ad_status_sync/
    service.py:378-382), while every other entry point revokes: the signed-in path
    (app/auth.py:209 "account_disabled"), the console relay's policy (re-reads ``is_active``
    at every renewal) and the LTI session hand-off (lti_identity/session.py:165).

    A deactivated owner must be refused on the token path exactly as an owner whose
    enrollment was withdrawn is (test_a_token_is_refused_once_the_enrollment_is_withdrawn:
    403, reading included)."""
    client, rid, _ = lab
    s = student(db_session)
    a, _ = launch(db_session, s, rid)
    until(db_session, a, "ready")
    token = tokens.mint(db_session, a.id, s.id, a.max_expires_at)
    s.is_active = False
    db_session.commit()
    resp = client.get(f"/lab-access/{a.id}", headers={"X-Lab-Token": token})
    assert resp.status_code == 403, ("read", resp.status_code, resp.text)
    resp = client.post(f"/lab-access/{a.id}/heartbeat", headers={"X-Lab-Token": token}, json={"active": True})
    assert resp.status_code == 403, ("heartbeat", resp.status_code, resp.text)
    resp = client.get(f"/lab-access/{a.id}/credentials", headers={"X-Lab-Token": token})
    assert resp.status_code == 403, ("credentials", resp.status_code, resp.text)


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: Staff opening the console touches the "
                                        "Student's lab, idle clock and ACTIVE state (control-plane/api/app/lab_sessions/service.py:1276)")
def test_staff_opening_the_console_does_not_touch_the_students_lab(lab, db_session):  # noqa: F811 — the lab fixtures, imported from test_lab_sessions
    """audit443 LOW finding, control-plane/api/app/lab_sessions/service.py:1276 (at the audit
    base; ``service.console``, the unconditional ``touch`` at its end): "Staff opening the
    console touches the Student's lab (idle clock and ACTIVE state)".

    A staff grant is not Student activity: the state means ACTIVE "the student has opened it"
    (models.py:25) and the idle clock exists because the Student uses the lab (the heartbeat
    rule pins the opposite intent for beats: "a forgotten open tab must not keep a lab open",
    test_lab_capacity_gate.py TestIdleMeansNoStudentActivity). ``own`` is already computed at
    service.py:1267 but only picks the audience; the touch runs outside every branch, so an
    instructor opening the console flips a READY lab to ACTIVE, pushes ``idle_expires_at`` and
    makes the Student look present. Grant issuance must leave the session untouched (a real
    staff connection keeping it alive is the sweep's separate rule, ADR 0014 §11)."""
    from app.auth import CurrentUser, get_current_user
    from app.main import app as fastapi_app

    client, rid, _ = lab
    s = student(db_session)
    a, _ = launch(db_session, s, rid)
    until(db_session, a, "ready")
    db_session.commit()
    before_state, before_idle = a.state, a.idle_expires_at
    teacher = User(email="t@example.test", display_name="T", role=UserRole.instructor,
                   tenant_id=s.tenant_id, keycloak_id="kc-teach")
    db_session.add(teacher)
    db_session.commit()
    who = CurrentUser(id=str(teacher.id), email=teacher.email, display_name="T", role=UserRole.instructor,
                      tenant_id=str(teacher.tenant_id), keycloak_id=teacher.keycloak_id)
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        assert client.post(f"/lab-sessions/{a.id}/console").status_code == 200
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
    db_session.refresh(a)
    assert (a.state, a.idle_expires_at) == (before_state, before_idle), (a.state, a.idle_expires_at)


@pytest.mark.audit443
def test_a_start_lab_during_teardown_hands_a_token_that_still_opens_the_page(lab, db_session, worker):  # noqa: F811 — the lab fixtures, imported from test_lab_sessions
    """audit443 LOW finding, control-plane/api/app/routers/integrations.py:948 (at the audit
    base; ``_launch_lab``'s token mint): "Start lab during the lab's teardown hands the
    browser an already-expired token".

    ``launch()`` returns the still-non-terminal session while its teardown runs
    (service.py:523-525: EXPIRED/CLEANING are in ENDING, not TERMINAL), and ``_launch_lab``
    mints the lab token from ``session.max_expires_at`` with no liveness check — in the past
    here. jwt.encode accepts it, ``tokens.verify`` refuses it, so every /lab-access read 401s
    and the lab page shows "This lab link has expired" instead of the ended-lab state, until
    the teardown reaches DESTROYED and Start lab makes attempt 2. The handed-over token must
    still open the page it was minted for."""
    from app.routers.integrations import _launch_lab

    _, rid, course_id = lab
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
    claims = tokens.verify(db_session, token, a.id)  # refuses today: "Signature has expired"
    assert claims["uid"] == str(s.id)


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: A range whose destroy is still running holds "
                                        "no capacity (control-plane/api/app/capacity/service.py:117)")
def test_a_range_being_destroyed_still_counts_as_committed(db_session, monkeypatch):
    """audit443 LOW finding, control-plane/api/app/capacity/service.py:117 (at the audit base;
    ``running_ranges``): "A range whose destroy is still running holds no capacity".

    ``end``/``sweep`` set the range to ``destroying`` immediately and only the worker task
    records ``destroyed`` after the backend deletes the VMs (worker/tasks.py:211-245), so for
    the whole window the VMs exist. But ``running_ranges`` counts only BUILT (provisioning,
    ready, running) plus stopped, ``holdings.resetting_loads`` covers only RESETTING labs, and
    ADR 0006 counts a range "until it is destroyed" — so the window counts as zero and a
    self-serve lab is admitted over a live range. The mirror-image ``provisioning`` IS counted
    conservatively. Until ``destroyed``, the range must still commit its template's size."""
    for key, value in {
        "CLUSTER_TOTAL_VCPU": "10",
        "CLUSTER_TOTAL_RAM_MB": str(20 * 1024),
        "CLUSTER_TOTAL_DISK_GB": "1000",
        "CLUSTER_OVERHEAD_PCT": "0",
        "CAPACITY_VCPU_RATIO": "4",
    }.items():
        monkeypatch.setenv(key, value)
    template = Template(name="t", yaml=TWO_VMS, tenant_id=DEV_TENANT)
    db_session.add(template)
    db_session.flush()
    rng = Range(id=uuid.uuid4(), tenant_id=DEV_TENANT, name="r", template_id=template.id)
    rng.state = RangeState.destroying
    db_session.add(rng)
    db_session.commit()
    c = ClusterCapacity(db_session).committed(db_session, SOON, SOON + timedelta(hours=2))
    assert (c.resources.vcpu, c.events, c.ranges) == (4, 0, 1), (c.resources.vcpu, c.events, c.ranges)


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: A class's own lab queues behind a self-serve "
                                        "lab that is itself waiting for site capacity (control-plane/api/app/lab_sessions/service.py:412)")
def test_a_classs_own_lab_starts_beside_a_queued_self_serve_lab(lab, db_session, worker, monkeypatch):  # noqa: F811 — the lab fixtures, imported from test_lab_sessions
    """audit443 MEDIUM finding, control-plane/api/app/lab_sessions/service.py:412 (at the
    audit base; ``_quota_problem``'s FIFO check): "A class's own lab queues behind a self-serve
    lab that is itself waiting for site capacity".

    A self-serve lab queued behind the tenant quota (or the site) holds nothing — the
    function's own docstring says queued labs "never count against each other" — yet the FIFO
    check counts any older queued lab of the tenant and refuses every later launch with
    "other labs are waiting; this lab starts in turn", including one that draws on its live
    class's hold (``own_booking``, capacity_gate.py:96-117) and needs no site capacity. The
    class's Students — whose capacity is already reserved — wait up to LAB_QUEUE_TIMEOUT for
    a lab that holds nothing. A lab that can start now must not be blocked by queued labs
    that themselves cannot."""
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
    act(db_session, service.advance, a)
    assert a.state == "queued" and capacity_gate.BUSY in a.error, (a.state, a.error)
    b_session, _ = launch(db_session, b, rid)
    assert b_session.state == "provisioning", (b_session.state, b_session.error)
