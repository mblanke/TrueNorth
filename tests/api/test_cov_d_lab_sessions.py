# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Lab sessions (``app.routers.lab_sessions``): the signed-in and lab-page endpoints.

Complements test_lab_sessions.py (the service contract against a fake worker, plus the
authz and lab-token CR1-17 checks) and test_lab_capacity_gate.py (the gate's own maths)
with the endpoint behaviours those leave out: the launch endpoint's validation and
idempotency, the list endpoint's own-vs-all_students split, the signed-in reset and end
endpoints' state transitions, reconcile that asks the worker to recheck only this
tenant's finished labs, a launch refused because the cluster is full (409, nothing
written — not even the auto-enrolment), staff being kept out of a Student's lab
accounts, and the lab-page token reading the accounts of a lab that is still building.

The worker is the same fake as test_lab_sessions.py's (``service._dispatch`` and
``probes._probe`` are the seams; no broker, no hypervisor).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _release_kit import CATALOGUE, CROSSWALK, build
from app.auth import CurrentUser, get_current_user
from app.enrollment import ensure_enrollment
from app.lab_sessions import service, tokens
from app.lab_sessions.models import LabSession
from app.main import app as fastapi_app
from app.models import Enrollment, GoldenImage, Tenant, User, UserRole
from app.scheduler.models import EventState, ScheduledEvent
from test_course_releases import OTHER_TENANT, acting_as, upload
from test_lab_sessions import FakeWorker, act, launch, student, until

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER = uuid.UUID(OTHER_TENANT)


@pytest.fixture
def worker(db_session, monkeypatch):
    from app.lab_sessions import probes

    w = FakeWorker(db_session)
    monkeypatch.setattr(service, "_dispatch", w.dispatch)
    monkeypatch.setenv("PROVISIONER_BACKEND", "mock")
    monkeypatch.setenv("LAB_RECONCILE_GRACE", "0")
    monkeypatch.setattr(probes, "_probe", lambda check, vm: (True, "fake network"))
    return w


@pytest.fixture
def lab(client, db_session, tmp_path, worker, monkeypatch):
    """C304 with module 6 as a range activity, its release accepted, the image catalogued."""
    monkeypatch.setenv("PROVISIONER_BACKEND", "mock")
    client.post("/qsp/import-crosswalk", files={"file": ("crosswalk.csv", CROSSWALK.read_bytes(), "text/csv")})
    client.post("/courses/import-programme", files={"file": ("c.csv", CATALOGUE.read_bytes(), "text/csv")})
    db_session.add(
        GoldenImage(catalogue_id="ubuntu-lts", hypervisor="vsphere", template_name="ubuntu-2404", enabled=True)
    )
    db_session.commit()
    rid = upload(client, build(tmp_path, range_ordinals=frozenset({6}))).json()["id"]
    assert client.post(f"/course-releases/{rid}/accept", json={}).status_code == 200
    course_id = client.get(f"/course-releases/{rid}").json()["course_id"]
    return client, uuid.UUID(rid), uuid.UUID(course_id)


def sign_in(u: User, role: UserRole = UserRole.student) -> None:
    """Every request acts as this real user until the test pops the override."""
    who = CurrentUser(
        id=str(u.id),
        email=u.email,
        display_name=u.display_name,
        role=role,
        tenant_id=str(u.tenant_id),
        keycloak_id=u.keycloak_id,
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who


# ── POST /lab-sessions: validation, idempotency ──────────────────────────


def test_the_launch_endpoint_validates_the_activity_and_relaunching_returns_the_same_lab(lab, db_session, worker):
    client, _, course_id = lab
    bad_pattern = client.post("/lab-sessions", json={"course_id": str(course_id), "activity_id": "mod_6"})
    assert bad_pattern.status_code == 422, bad_pattern.text

    not_a_range = client.post("/lab-sessions", json={"course_id": str(course_id), "activity_id": "mod_001"})
    assert not_a_range.status_code == 404, not_a_range.text
    assert "not a range activity" in not_a_range.json()["detail"]

    first = client.post("/lab-sessions", json={"course_id": str(course_id), "activity_id": "mod_006"})
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["attempt"] == 1
    assert body["state"] in ("queued", "provisioning")
    assert body["error"] == ""
    assert body["access_token"] is None
    assert body["ends_with_class"] is False  # a self-serve lab, not a booked class's

    again = client.post("/lab-sessions", json={"course_id": str(course_id), "activity_id": "mod_006"})
    assert again.status_code == 201, again.text
    assert again.json()["id"] == body["id"]
    assert [c[0] for c in worker.calls].count("provision_range") == 1  # never a second range


# ── GET /lab-sessions: own by default, all_students for staff ────────────


def test_listing_shows_the_callers_labs_and_all_students_needs_the_staff_right(lab, db_session, worker):
    client, rid, course_id = lab
    with acting_as(UserRole.student) as who:
        db_session.add(
            User(
                id=uuid.UUID(who.id),
                email=who.email,
                display_name="x",
                role=UserRole.student,
                tenant_id=DEV_TENANT,
                keycloak_id=who.keycloak_id,
            )
        )
        db_session.flush()
        ensure_enrollment(db_session, user_id=uuid.UUID(who.id), course_id=course_id, tenant_id=DEV_TENANT)
        db_session.commit()
        mine = client.post("/lab-sessions", json={"course_id": str(course_id), "activity_id": "mod_006"})
        assert mine.status_code == 201, mine.text
        own_id = mine.json()["id"]

        # A class's own lab (booking_id set) is reported as ending with the class.
        db_session.add(
            LabSession(
                tenant_id=DEV_TENANT,
                user_id=uuid.UUID(who.id),
                release_id=rid,
                activity_id="mod_999",
                state="ready",
                profile_id="x",
                profile_digest="0" * 64,
                backend="mock",
                vcpu=2,
                ram_mb=2048,
                booking_id=uuid.uuid4(),
            )
        )
        db_session.flush()

        listed = client.get("/lab-sessions")
        assert [row["id"] for row in listed.json()] and own_id in {r["id"] for r in listed.json()}
        class_lab = [r for r in listed.json() if r["activity_id"] == "mod_999"]
        assert class_lab and class_lab[0]["ends_with_class"] is True

        # all_students without the staff right is silently ignored: still only the caller's.
        assert {r["id"] for r in client.get("/lab-sessions?all_students=true").json()} == {
            r["id"] for r in listed.json()
        }

    with acting_as(UserRole.instructor):
        # No labs of their own, and without all_students they see nobody else's either.
        assert client.get("/lab-sessions").json() == []
        staff_view = client.get("/lab-sessions?all_students=true").json()
        assert own_id in {r["id"] for r in staff_view} and len(staff_view) == 2


# ── POST /lab-sessions/{id}/reset and /end ───────────────────────────────


def test_the_end_endpoint_completes_a_ready_lab_and_tears_it_down(lab, db_session, worker):
    client, rid, _ = lab
    s = student(db_session)
    session, _ = launch(db_session, s, rid)
    until(db_session, session, "ready")

    ended = client.post(f"/lab-sessions/{session.id}/end")
    assert ended.status_code == 200, ended.text
    body = ended.json()
    assert body["state"] == "completed" and body["end_reason"] == "completed"
    assert body["ended_at"] is not None
    assert [c[0] for c in worker.calls].count("destroy_range") == 1


def test_the_reset_endpoint_sends_a_lab_back_to_its_baseline(lab, db_session, worker):
    client, rid, _ = lab
    s = student(db_session)
    session, _ = launch(db_session, s, rid)
    until(db_session, session, "ready")

    reset = client.post(f"/lab-sessions/{session.id}/reset")
    assert reset.status_code == 200, reset.text
    assert reset.json()["state"] == "resetting"
    assert ("restore_snapshot", str(session.range_id), str(session.baseline_snapshot_id)) in worker.calls
    worker.run()
    assert until(db_session, session, "active").state == "active"


# ── POST /lab-sessions/reconcile ─────────────────────────────────────────


def test_reconcile_rechecks_only_this_tenants_finished_labs(lab, db_session, worker):
    client, rid, _ = lab
    assert client.post("/lab-sessions/reconcile").json() == {"ranges_checked": 0}

    s = student(db_session)
    session, _ = launch(db_session, s, rid)
    until(db_session, session, "ready")
    act(db_session, service.end, session)
    until(db_session, session, "destroyed")
    worker.calls.clear()

    checked = client.post("/lab-sessions/reconcile")
    assert checked.status_code == 200, checked.text
    assert checked.json() == {"ranges_checked": 1}
    assert ("reconcile_lab_vms", [str(session.range_id)], "mock") in worker.calls

    # Another tenant's reconcile sees none of them.
    with acting_as(UserRole.admin, tenant=OTHER_TENANT):
        assert client.post("/lab-sessions/reconcile").json() == {"ranges_checked": 0}


# ── a launch the cluster cannot take ─────────────────────────────────────


@pytest.fixture
def full_cluster(monkeypatch):
    """Ten vCPU in all, no headroom kept back (env supply: no hosts are discovered here)."""
    monkeypatch.setenv("CLUSTER_OVERHEAD_PCT", "0")
    monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "10")
    monkeypatch.setenv("CLUSTER_TOTAL_RAM_MB", "1048576")
    monkeypatch.setenv("CLUSTER_TOTAL_DISK_GB", "100000")
    monkeypatch.setenv("SITE_TIMEZONE", "UTC")


def test_a_launch_with_no_capacity_left_is_a_409_and_writes_nothing(lab, db_session, full_cluster):
    client, _, course_id = lab
    if db_session.get(Tenant, OTHER) is None:
        db_session.add(Tenant(id=OTHER, name="Other Org", slug="other"))
        db_session.flush()
    start = datetime.now(UTC) + timedelta(hours=1)
    db_session.add(
        ScheduledEvent(
            name="Another tenant's class",
            tenant_id=OTHER,
            state=EventState.scheduled,
            start_time=start,
            end_time=start + timedelta(hours=3),
            vcpu_total=10,
            ram_mb_total=1024,
            disk_gb_total=10,
        )
    )
    db_session.commit()

    refused = client.post("/lab-sessions", json={"course_id": str(course_id), "activity_id": "mod_006"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"].startswith("Labs are busy, next free at ")

    # The refusal undid the whole launch: no session, and not even the auto-enrolment.
    assert db_session.query(LabSession).count() == 0
    assert db_session.query(Enrollment).count() == 0


# ── lab accounts: the Student's own lab only ──────────────────────────────


def test_staff_who_can_act_on_a_lab_still_cannot_read_its_accounts(lab, db_session, worker):
    client, rid, _ = lab
    s = student(db_session)
    session, _ = launch(db_session, s, rid)
    until(db_session, session, "ready")

    # Staff reach the session itself (they may console, reset, end it) ...
    assert client.get(f"/lab-sessions/{session.id}").status_code == 200
    # ... but the accounts are the Student's alone, staff included.
    assert client.get(f"/lab-sessions/{session.id}/credentials").status_code == 404
    assert client.post(f"/lab-sessions/{session.id}/credentials/{uuid.uuid4()}/reveal").status_code == 404

    sign_in(s)
    try:
        listed = client.get(f"/lab-sessions/{session.id}/credentials")
        assert listed.status_code == 200, listed.text
        assert listed.json()["lab_session_id"] == str(session.id)
        assert listed.json()["accounts"] == []  # not set by a build yet
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def test_the_lab_token_reads_the_accounts_of_a_lab_still_building(lab, db_session, worker):
    """The lab page shows the logins as soon as the lab is usable; while it builds the
    answer is an empty list, and a reveal has no account to reveal (404)."""
    client, rid, _ = lab
    s = student(db_session)
    session, _ = launch(db_session, s, rid)
    db_session.commit()
    assert session.state not in ("ready", "active")  # still building: no usable range yet
    token = tokens.mint(db_session, session.id, s.id, None)
    headers = {"X-Lab-Token": token}

    listed = client.get(f"/lab-access/{session.id}/credentials", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["accounts"] == []

    revealed = client.post(f"/lab-access/{session.id}/credentials/{uuid.uuid4()}/reveal", headers=headers)
    assert revealed.status_code == 404, revealed.text
    assert "Account not found" in revealed.json()["detail"]

    assert client.get(f"/lab-access/{session.id}/credentials").status_code == 401


def test_the_lab_page_heartbeat_reports_use_over_the_token(lab, db_session, worker):
    client, rid, _ = lab
    s = student(db_session)
    session, _ = launch(db_session, s, rid)
    until(db_session, session, "ready")
    token = tokens.mint(db_session, session.id, s.id, None)
    headers = {"X-Lab-Token": token}
    before = session.idle_expires_at

    quiet = client.post(f"/lab-access/{session.id}/heartbeat", headers=headers, json={"active": False})
    assert quiet.status_code == 200, quiet.text
    assert quiet.json()["state"] == "ready"
    db_session.refresh(session)
    assert session.idle_expires_at == before  # an open tab nobody uses does not keep it open

    used = client.post(f"/lab-access/{session.id}/heartbeat", headers=headers, json={"active": True})
    assert used.status_code == 200, used.text
    assert used.json()["state"] == "active"
    db_session.refresh(session)
    assert session.idle_expires_at > before
