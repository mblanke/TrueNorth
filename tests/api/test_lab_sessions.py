"""Lab sessions against a fake worker (the dispatched range tasks applied to the test DB).

Proves the contract in docs/arc2-44-course-programme.md §6 "Small individual range":
idempotent launch, readiness only after probes and a reset point, isolated networks per
student, reset of one session alone, a failed create retried without leftovers, expiry
removing only its own resources, evidence surviving teardown, quotas and capacity
queueing, tenancy, the lab-page token, and course-scoped launches resolving the student's
pinned release.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from _release_kit import CATALOGUE, CROSSWALK, build
from app.lab_sessions import service, tokens
from app.lab_sessions.models import LabNetworkLease, LabSession
from app.models import GoldenImage, Range, RangeSnapshot, RangeState, Template, User, UserRole
from test_course_releases import OTHER_TENANT, acting_as, upload

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


class FakeWorker:
    """Applies what each worker task would do, now or when ``run()`` is called."""

    def __init__(self, db):
        self.db = db
        self.calls: list[tuple] = []
        self.queue: list[tuple] = []
        self.hold = False
        self.fail_provision: set[str] = set()

    def dispatch(self, task, *args):
        self.calls.append((task, *args))
        self.queue.append((task, *args))
        if not self.hold:
            self.run()
        return f"task-{len(self.calls)}"

    def run(self):
        while self.queue:
            task, *args = self.queue.pop(0)
            getattr(self, task)(*args)
        self.db.flush()

    def provision_range(self, range_id):
        rng = self.db.get(Range, uuid.UUID(range_id))
        if range_id in self.fail_provision:
            rng.state, rng.error_message = RangeState.failed, "Template 'ubuntu-lts' not found in library"
            return
        template = yaml.safe_load(self.db.get(Template, rng.template_id).yaml)
        vms = [
            {
                "vm_id": f"vm-{uuid.uuid4().hex[:6]}",
                "name": f"{range_id[:8]}-{n['id']}",
                "ip": f"10.9.0.{i + 10}",
                "status": "running",
                "tools_ready": True,
            }
            for i, n in enumerate(template["nodes"])
        ]
        rng.provisioner_output = json.dumps({"vms": vms, "networks": template["network"]["vlans"]})
        rng.state = RangeState.ready

    def snapshot_range(self, range_id, snapshot_id):
        self.db.get(RangeSnapshot, uuid.UUID(snapshot_id)).snapshot_state = "ready"

    def restore_snapshot(self, range_id, snapshot_id):
        self.db.get(RangeSnapshot, uuid.UUID(snapshot_id)).snapshot_state = "ready"

    def destroy_range(self, range_id):
        self.db.get(Range, uuid.UUID(range_id)).state = RangeState.destroyed

    def reconcile_lab_vms(self, range_ids, backend):
        pass


@pytest.fixture
def worker(db_session, monkeypatch):
    from app.lab_sessions import probes

    w = FakeWorker(db_session)
    monkeypatch.setattr(service, "_dispatch", w.dispatch)
    w.reachable = True
    monkeypatch.setattr(probes, "_probe", lambda check, vm: (w.reachable, "fake network"))
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


def student(db, tenant=DEV_TENANT) -> User:
    u = User(
        email=f"s{uuid.uuid4().hex[:8]}@example.test",
        display_name="s",
        role=UserRole.student,
        tenant_id=tenant,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    db.add(u)
    db.flush()
    return u


def launch(db, user, release_id, activity="mod_006"):
    session, created = service.launch(
        db, tenant_id=user.tenant_id, user_id=user.id, release_id=release_id, activity_id=activity
    )
    db.flush()
    return session, created


def until(db, session, state, steps=6):
    for _ in range(steps):
        service.advance(db, session)
        if session.state == state:
            return session
    raise AssertionError(f"session stuck in {session.state}: {session.error}")


class TestLaunch:
    def test_launching_twice_is_one_session_and_one_range(self, lab, db_session, worker):
        _, rid, _ = lab
        s = student(db_session)
        first, created = launch(db_session, s, rid)
        again, created_again = launch(db_session, s, rid)
        assert created and not created_again and again.id == first.id
        assert [c[0] for c in worker.calls].count("provision_range") == 1

    def test_ready_only_after_probes_pass_and_a_reset_point_exists(self, lab, db_session, worker):
        _, rid, _ = lab
        worker.hold = True
        session, _ = launch(db_session, student(db_session), rid)
        assert session.state == "provisioning"
        service.advance(db_session, session)
        assert session.state == "provisioning"  # the provisioner has not finished
        worker.run()
        service.advance(db_session, session)
        assert session.state == "baselining", (
            session.error,
            session.probes,
            db_session.get(Range, session.range_id).state,
        )
        assert json.loads(session.probes)[0]["ok"] is True
        worker.run()
        service.advance(db_session, session)
        assert session.state == "ready" and session.readiness_seconds is not None

    def test_unreachable_services_keep_it_provisioning_then_fail_it(self, lab, db_session, worker, monkeypatch):
        _, rid, _ = lab
        worker.reachable = False
        session, _ = launch(db_session, student(db_session), rid)
        service.advance(db_session, session)
        assert session.state == "provisioning" and json.loads(session.probes)[0]["ok"] is False
        session.provisioning_at = datetime.now(UTC) - timedelta(hours=2)
        service.advance(db_session, session)
        assert session.state in ("cleaning", "destroyed") and "not ready within" in session.error

    def test_a_profile_image_missing_from_the_catalogue_is_refused(self, lab, db_session):
        _, rid, _ = lab
        db_session.query(GoldenImage).delete()
        db_session.flush()
        with pytest.raises(service.LabRefusedError, match="not in the image catalogue"):
            launch(db_session, student(db_session), rid)

    def test_only_range_activities_have_labs(self, lab, db_session):
        _, rid, _ = lab
        with pytest.raises(service.LabRefusedError, match="not a range activity"):
            launch(db_session, student(db_session), rid, activity="mod_001")


class TestIsolation:
    @pytest.fixture
    def vsphere(self, monkeypatch, db_session):
        monkeypatch.setenv("PROVISIONER_BACKEND", "vsphere_api")
        monkeypatch.setenv("LAB_PORT_GROUPS", "pg-lab-01,pg-lab-02")

    def test_two_students_get_distinct_ranges_and_networks(self, lab, db_session, vsphere):
        _, rid, _ = lab
        a, _ = launch(db_session, student(db_session), rid)
        b, _ = launch(db_session, student(db_session), rid)
        assert a.range_id != b.range_id
        nets = {
            s.id: {row.port_group for row in db_session.query(LabNetworkLease).filter_by(session_id=s.id)}
            for s in (a, b)
        }
        assert nets[a.id] and nets[b.id] and not nets[a.id] & nets[b.id]
        template = yaml.safe_load(db_session.get(Template, db_session.get(Range, a.range_id).template_id).yaml)
        assert template["network"]["vlans"][0]["port_group"] in nets[a.id]

    def test_capacity_queues_and_a_freed_network_starts_the_next(self, lab, db_session, vsphere):
        _, rid, _ = lab
        a, _ = launch(db_session, student(db_session), rid)
        launch(db_session, student(db_session), rid)
        c, _ = launch(db_session, student(db_session), rid)
        assert c.state == "queued" and "networks are in use" in c.error and c.range_id is None
        until(db_session, a, "ready")
        service.end(db_session, a)
        until(db_session, a, "destroyed")
        assert db_session.query(LabNetworkLease).filter_by(session_id=a.id).count() == 0
        service.sweep(db_session)
        assert c.state in ("provisioning", "baselining", "ready")

    def test_resetting_one_session_leaves_the_other_alone(self, lab, db_session, worker):
        _, rid, _ = lab
        a, _ = launch(db_session, student(db_session), rid)
        b, _ = launch(db_session, student(db_session), rid)
        until(db_session, a, "ready")
        until(db_session, b, "ready")
        worker.calls.clear()
        worker.hold = True
        service.reset(db_session, a)
        assert a.state == "resetting" and b.state == "ready"
        assert worker.calls == [("restore_snapshot", str(a.range_id), str(a.baseline_snapshot_id))]
        worker.run()
        until(db_session, a, "active")


class TestFailureAndExpiry:
    def test_a_failed_create_is_torn_down_and_a_retry_is_a_fresh_attempt(self, lab, db_session, worker):
        _, rid, _ = lab
        s = student(db_session)
        worker.hold = True
        first, _ = launch(db_session, s, rid)
        worker.fail_provision.add(str(first.range_id))
        worker.run()
        service.advance(db_session, first)
        assert first.state == "cleaning" and "could not be built" in first.error
        worker.run()
        until(db_session, first, "destroyed")
        assert ("reconcile_lab_vms", [str(first.range_id)], "mock") in worker.calls  # leftovers removed by name
        worker.hold = False
        second, created = launch(db_session, s, rid)
        assert created and second.attempt == 2 and second.range_id != first.range_id

    def test_expiry_removes_only_that_sessions_resources_and_keeps_evidence(self, lab, db_session, worker):
        _, rid, _ = lab
        a, _ = launch(db_session, student(db_session), rid)
        b, _ = launch(db_session, student(db_session), rid)
        until(db_session, a, "ready")
        until(db_session, b, "ready")
        service.add_evidence(db_session, a, {"kind": "submission", "data": {"flag": "abc"}})
        a.max_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        service.advance(db_session, a)
        assert a.state in ("expired", "destroyed") and a.end_reason == "expired"
        until(db_session, a, "destroyed")
        assert db_session.get(Range, a.range_id).state == RangeState.destroyed
        assert db_session.get(Range, b.range_id).state == RangeState.ready and b.state == "ready"
        assert json.loads(a.evidence)[0]["data"] == {"flag": "abc"}

    def test_a_student_runs_one_lab_at_a_time(self, lab, db_session):
        _, rid, _ = lab
        s = student(db_session)
        launch(db_session, s, rid)
        # a second lab of the same student (another activity) would wait
        second = LabSession(
            tenant_id=s.tenant_id,
            user_id=s.id,
            release_id=rid,
            activity_id="mod_007",
            state="queued",
            profile_id="x",
            profile_digest="0" * 64,
            backend="mock",
            vcpu=2,
            ram_mb=2048,
        )
        db_session.add(second)
        db_session.flush()
        assert (
            service._quota_problem(db_session, second)
            == "you already have a lab running; end it before starting another"
        )


class TestApi:
    def test_only_the_owner_and_staff_see_a_session(self, lab, db_session):
        client, rid, course_id = lab
        owner = student(db_session)
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
            mine = client.post("/lab-sessions", json={"course_id": str(course_id), "activity_id": "mod_006"})
            assert mine.status_code == 201, mine.text
            again = client.post("/lab-sessions", json={"course_id": str(course_id), "activity_id": "mod_006"})
            assert again.json()["id"] == mine.json()["id"]
        other, _ = launch(db_session, owner, rid)
        with acting_as(UserRole.student):
            assert client.get(f"/lab-sessions/{other.id}").status_code == 404
        with acting_as(UserRole.instructor):
            assert client.get(f"/lab-sessions/{other.id}").status_code == 200
        with acting_as(UserRole.admin, tenant=OTHER_TENANT):
            assert client.get(f"/lab-sessions/{other.id}").status_code == 404

    def test_the_lab_page_token_opens_its_session_only(self, lab, db_session):
        client, rid, _ = lab
        s = student(db_session)
        a, _ = launch(db_session, s, rid)
        b, _ = launch(db_session, student(db_session), rid)
        db_session.commit()
        token = tokens.mint(db_session, a.id, s.id, a.max_expires_at)
        assert client.get(f"/lab-access/{a.id}", headers={"X-Lab-Token": token}).status_code == 200
        assert client.get(f"/lab-access/{b.id}", headers={"X-Lab-Token": token}).status_code == 401
        assert client.get(f"/lab-access/{a.id}").status_code == 401
        until(db_session, a, "ready")
        db_session.commit()
        console = client.post(f"/lab-access/{a.id}/console", headers={"X-Lab-Token": token})
        assert console.status_code == 200 and console.json()["url"].startswith("mock://console/")
        refused = client.post(f"/lab-access/{a.id}/console?node=ghost", headers={"X-Lab-Token": token})
        assert refused.status_code == 403

    def test_an_lti_lab_launch_starts_the_lab_and_hands_over_a_token(self, lab, db_session):
        from app.routers.integrations import _launch_lab

        _, _, course_id = lab
        s = student(db_session)
        target = _launch_lab(db_session, s, f"{course_id}:mod_006")
        session_id, _, fragment = target.partition("/labs/")[2].partition("#token=")
        session_id = session_id.split("?")[0]
        assert tokens.verify(db_session, fragment, uuid.UUID(session_id))["uid"] == str(s.id)
        assert _launch_lab(db_session, s, f"{course_id}:mod_006").split("#")[0] == target.split("#")[0]

    def test_a_course_lab_follows_the_students_pinned_release(self, lab, db_session, tmp_path):
        client, rid, course_id = lab
        s = student(db_session)
        first = service.release_for_student(db_session, tenant_id=DEV_TENANT, user_id=s.id, course_id=course_id)
        assert first.id == rid
        newer = upload(
            client, build(tmp_path / "v2", range_ordinals=frozenset({6}), title_suffix=" (rev)", slug="arc2-iot-b")
        )
        assert client.post(f"/course-releases/{newer.json()['id']}/accept", json={}).status_code == 200
        assert (
            service.release_for_student(db_session, tenant_id=DEV_TENANT, user_id=s.id, course_id=course_id).id == rid
        )
        late = student(db_session)
        assert (
            str(service.release_for_student(db_session, tenant_id=DEV_TENANT, user_id=late.id, course_id=course_id).id)
            == newer.json()["id"]
        )
