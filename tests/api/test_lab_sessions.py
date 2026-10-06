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
from app.enrollment import ensure_enrollment
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
        self.down: set[str] = set()  # tasks the broker refuses
        self.fail_destroy = False

    def dispatch(self, task, *args):
        if task in self.down:
            return None
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
        self.db.get(Range, uuid.UUID(range_id)).state = RangeState.failed if self.fail_destroy else RangeState.destroyed

    def reconcile_lab_vms(self, range_ids, backend):
        pass


@pytest.fixture
def worker(db_session, monkeypatch):
    from app.lab_sessions import probes

    w = FakeWorker(db_session)
    monkeypatch.setattr(service, "_dispatch", w.dispatch)
    monkeypatch.setenv("LAB_RECONCILE_GRACE", "0")
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
    """As the API does it: launch, commit, then send the queued worker tasks."""
    session, created = service.launch(
        db, tenant_id=user.tenant_id, user_id=user.id, release_id=release_id, activity_id=activity
    )
    db.commit()
    service.flush_outbox(db)
    return session, created


def act(db, fn, session, *args, **kwargs):
    """One operation as the API runs it: under the session's lease, tasks after commit."""
    return service.run_locked(db, session, fn, *args, **kwargs)


def until(db, session, state, steps=6):
    for _ in range(steps):
        act(db, service.advance, session)
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
        act(db_session, service.advance, session)
        assert session.state == "provisioning"  # the provisioner has not finished
        worker.run()
        act(db_session, service.advance, session)
        assert session.state == "baselining", (
            session.error,
            session.probes,
            db_session.get(Range, session.range_id).state,
        )
        assert json.loads(session.probes)[0]["ok"] is True
        worker.run()
        act(db_session, service.advance, session)
        assert session.state == "ready" and session.readiness_seconds is not None

    def test_unreachable_services_keep_it_provisioning_then_fail_it(self, lab, db_session, worker, monkeypatch):
        _, rid, _ = lab
        worker.reachable = False
        session, _ = launch(db_session, student(db_session), rid)
        act(db_session, service.advance, session)
        assert session.state == "provisioning" and json.loads(session.probes)[0]["ok"] is False
        session.provisioning_at = datetime.now(UTC) - timedelta(hours=2)
        act(db_session, service.advance, session)
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
        act(db_session, service.end, a)
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
        act(db_session, service.reset, a)
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
        act(db_session, service.advance, first)
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
        act(db_session, service.add_evidence, a, {"kind": "submission", "data": {"flag": "abc"}})
        a.max_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        act(db_session, service.advance, a)
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
            refused = client.post("/lab-sessions", json={"course_id": str(course_id), "activity_id": "mod_006"})
            assert refused.status_code == 403 and "not enrolled" in refused.json()["detail"]
            ensure_enrollment(db_session, user_id=uuid.UUID(who.id), course_id=course_id, tenant_id=DEV_TENANT)
            db_session.commit()
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
        first = service.release_for_student(
            db_session, tenant_id=DEV_TENANT, user_id=s.id, course_id=course_id, auto_enroll=True
        )
        assert first.id == rid
        newer = upload(
            client, build(tmp_path / "v2", range_ordinals=frozenset({6}), title_suffix=" (rev)", slug="arc2-iot-b")
        )
        assert client.post(f"/course-releases/{newer.json()['id']}/accept", json={}).status_code == 200
        assert (
            service.release_for_student(
                db_session, tenant_id=DEV_TENANT, user_id=s.id, course_id=course_id, auto_enroll=True
            ).id
            == rid
        )
        late = student(db_session)
        assert (
            str(
                service.release_for_student(
                    db_session, tenant_id=DEV_TENANT, user_id=late.id, course_id=course_id, auto_enroll=True
                ).id
            )
            == newer.json()["id"]
        )


# -- review findings (2026-10-05) ---------------------------------------------


class TestQueue:
    def test_queued_labs_do_not_block_each_other_and_start_in_order(self, lab, db_session, monkeypatch):
        _, rid, _ = lab
        monkeypatch.setenv("LAB_MAX_SESSIONS_PER_TENANT", "1")
        a, _ = launch(db_session, student(db_session), rid)
        b, _ = launch(db_session, student(db_session), rid)
        c, _ = launch(db_session, student(db_session), rid)
        assert b.state == "queued" and c.state == "queued"
        until(db_session, a, "ready")
        act(db_session, service.end, a)
        until(db_session, a, "destroyed")
        service.sweep(db_session)
        assert b.state != "queued"  # the oldest waiter starts
        assert c.state == "queued" and c.error

    def test_a_queued_lab_gives_up_after_the_queue_timeout(self, lab, db_session, monkeypatch):
        _, rid, _ = lab
        monkeypatch.setenv("LAB_MAX_SESSIONS_PER_TENANT", "1")
        launch(db_session, student(db_session), rid)
        b, _ = launch(db_session, student(db_session), rid)
        b.state_since = datetime.now(UTC) - timedelta(hours=3)
        db_session.commit()
        act(db_session, service.advance, b)
        assert b.state == "destroyed" and b.end_reason == "queue_timeout"

    def test_a_student_has_one_lab_at_a_time_across_activities(self, lab, db_session):
        _, rid, _ = lab
        s = student(db_session)
        launch(db_session, s, rid)
        with pytest.raises(service.LabRefusedError, match="already have a lab running or waiting"):
            service.launch(db_session, tenant_id=s.tenant_id, user_id=s.id, release_id=rid, activity_id="mod_007")


class TestDispatch:
    def test_a_task_the_broker_refuses_is_kept_and_sent_again(self, lab, db_session, worker):
        _, rid, _ = lab
        worker.down.add("provision_range")
        s, _ = launch(db_session, student(db_session), rid)
        assert json.loads(s.pending) == [["provision_range", [str(s.range_id)]]]
        worker.down.clear()
        service.sweep(db_session)
        assert ("provision_range", str(s.range_id)) in worker.calls and s.pending == "[]"

    def test_tasks_go_out_only_after_the_transaction_commits(self, lab, db_session, worker):
        _, rid, _ = lab
        s = student(db_session)
        service.launch(db_session, tenant_id=s.tenant_id, user_id=s.id, release_id=rid, activity_id="mod_006")
        assert worker.calls == []  # nothing sent before the caller commits and flushes
        db_session.commit()
        service.flush_outbox(db_session)
        assert [c[0] for c in worker.calls] == ["provision_range"]

    # F19 (docs/review/codereview1.md): the process dies after the commit, before the send.

    def _later(self, monkeypatch, minutes=5):
        """The restarted process's clock: past any lease the dead one held."""
        later = service._now() + timedelta(minutes=minutes)
        monkeypatch.setattr(service, "_now", lambda: later)

    def test_a_launch_task_lost_with_its_process_is_sent_after_restart(self, lab, db_session, worker, monkeypatch):
        _, rid, _ = lab
        s = student(db_session)
        session, _ = service.launch(
            db_session, tenant_id=s.tenant_id, user_id=s.id, release_id=rid, activity_id="mod_006"
        )
        db_session.commit()
        db_session.info.pop(service.OUTBOX, None)  # the process dies here: the send never happens
        assert worker.calls == []
        self._later(monkeypatch)
        service.sweep(db_session)
        assert worker.calls == [("provision_range", str(session.range_id))]

    def test_a_reset_task_lost_with_its_process_is_sent_after_restart(self, lab, db_session, worker, monkeypatch):
        _, rid, _ = lab
        session, _ = launch(db_session, student(db_session), rid)
        until(db_session, session, "ready")
        sent = len(worker.calls)

        def die(db):
            db.info.pop(service.OUTBOX, None)
            raise SystemExit("process died after the commit")

        monkeypatch.setattr(service, "flush_outbox", die)
        with pytest.raises(SystemExit):
            act(db_session, service.reset, session)
        assert session.state == "resetting" and len(worker.calls) == sent
        monkeypatch.undo()  # restarted process: real flush, real workers' fakes
        monkeypatch.setattr(service, "_dispatch", worker.dispatch)
        self._later(monkeypatch)
        service.sweep(db_session)
        assert [c[0] for c in worker.calls[sent:]] == ["restore_snapshot"]

    def test_a_sweep_between_the_commit_and_the_send_does_not_send_twice(self, lab, db_session, worker):
        _, rid, _ = lab
        s = student(db_session)
        session, _ = service.launch(
            db_session, tenant_id=s.tenant_id, user_id=s.id, release_id=rid, activity_id="mod_006"
        )
        db_session.commit()
        outbox = db_session.info.pop(service.OUTBOX, None)  # another process sweeps meanwhile
        service.sweep(db_session)
        db_session.info[service.OUTBOX] = outbox
        service.flush_outbox(db_session)
        assert worker.calls == [("provision_range", str(session.range_id))]

    def test_an_action_on_a_lab_another_process_holds_is_refused_not_doubled(self, lab, db_session, worker):
        _, rid, _ = lab
        a, _ = launch(db_session, student(db_session), rid)
        until(db_session, a, "ready")
        assert service.claim(db_session, a)
        db_session.commit()
        with pytest.raises(service.LabRefusedError, match="busy"):
            service.run_locked(db_session, a, service.reset)
        assert service.run_locked(db_session, a, service.advance, busy_ok=True) is a


class TestTeardown:
    def test_a_teardown_that_keeps_failing_stops_and_keeps_its_networks(self, lab, db_session, worker, monkeypatch):
        _, rid, _ = lab
        monkeypatch.setenv("PROVISIONER_BACKEND", "vsphere_api")
        monkeypatch.setenv("LAB_PORT_GROUPS", "pg-lab-01")
        a, _ = launch(db_session, student(db_session), rid)
        until(db_session, a, "ready")
        worker.fail_destroy = True
        act(db_session, service.end, a)
        for _ in range(12):
            act(db_session, service.advance, a)
        assert a.state == "failed" and "operator" in a.error
        assert db_session.query(LabNetworkLease).filter_by(session_id=a.id).count() == 1  # not handed on

    def test_a_rebuild_reset_keeps_its_networks_and_lifetime(self, lab, db_session, worker, monkeypatch):
        _, rid, _ = lab
        monkeypatch.setenv("PROVISIONER_BACKEND", "vsphere_api")
        monkeypatch.setenv("LAB_PORT_GROUPS", "pg-lab-01,pg-lab-02")
        monkeypatch.setattr(
            service,
            "_profile",
            lambda db, s: {
                **service.release_profile(db, db.get(service.CourseRelease, s.release_id), s.activity_id),
                "reset": {"mode": "rebuild"},
            },
        )
        a, _ = launch(db_session, student(db_session), rid)
        until(db_session, a, "ready")
        nets, ends, old = service._session_networks(db_session, a), a.max_expires_at, a.range_id
        act(db_session, service.reset, a)
        until(db_session, a, "ready")
        assert a.range_id != old and service._session_networks(db_session, a) == nets
        assert a.max_expires_at == ends and str(old) in json.loads(a.retired_ranges)


class TestBoundaries:
    def test_a_template_may_not_pin_a_network(self, client):
        yml = "name: t\nnetwork:\n  vlans:\n    - name: lab\n      cidr: 10.0.0.0/24\n      port_group: Management Network\n"
        resp = client.post("/templates", json={"name": "t", "version": "1.0", "yaml": yml, "is_public": False})
        assert resp.status_code == 422 and "port_group" in resp.json()["detail"]

    def test_lab_ranges_are_hidden_from_students_and_driven_only_by_their_session(self, lab, db_session):
        client, rid, _ = lab
        a, _ = launch(db_session, student(db_session), rid)
        assert client.post(f"/ranges/{a.range_id}/destroy").status_code == 409
        with acting_as(UserRole.student):
            assert str(a.range_id) not in {r["id"] for r in client.get("/ranges").json()}
            assert client.get(f"/ranges/{a.range_id}").status_code == 404

    def test_a_token_must_be_a_lab_token_for_this_student(self, lab, db_session):
        import jwt as pyjwt
        from app import lti13

        client, rid, _ = lab
        s = student(db_session)
        a, _ = launch(db_session, s, rid)
        key = lti13.get_tool_key(db_session)
        now = int(datetime.now(UTC).timestamp())
        base = {"iss": "truenorth", "aud": "truenorth-lab", "sub": str(a.id), "iat": now, "exp": now + 60}
        untyped = pyjwt.encode({**base, "uid": str(s.id)}, key.private_key_pem, algorithm="RS256")
        stranger = pyjwt.encode(
            {**base, "typ": "lab", "uid": str(uuid.uuid4())}, key.private_key_pem, algorithm="RS256"
        )
        assert client.get(f"/lab-access/{a.id}", headers={"X-Lab-Token": untyped}).status_code == 401
        assert client.get(f"/lab-access/{a.id}", headers={"X-Lab-Token": stranger}).status_code == 404

    def test_students_submit_work_but_not_results(self, lab, db_session):
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
            ensure_enrollment(db_session, user_id=uuid.UUID(who.id), course_id=course_id, tenant_id=DEV_TENANT)
            db_session.commit()
            sid = client.post("/lab-sessions", json={"course_id": str(course_id), "activity_id": "mod_006"}).json()[
                "id"
            ]
            ok = client.post(f"/lab-sessions/{sid}/evidence", json={"kind": "submission", "data": {"note": "done"}})
            assert ok.status_code == 200
            forged = client.post(f"/lab-sessions/{sid}/evidence", json={"kind": "validator", "data": {"pass": True}})
            assert forged.status_code == 403
            big = client.post(f"/lab-sessions/{sid}/evidence", json={"kind": "submission", "data": {"x": "a" * 70000}})
            assert big.status_code == 413


@pytest.mark.parametrize(("count", "ok"), [(20, True), (21, False)])
def test_a_student_lab_of_up_to_twenty_vms_is_allowed_by_default(monkeypatch, count, ok):
    """Programme policy: any course may give each student up to 20 VMs."""
    from _release_kit import LAB_PROFILE

    for var in ("LAB_MAX_VMS_PER_LAB", "LAB_MAX_VM_RAM_MB", "LAB_MAX_VM_DISK_GB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(service, "_catalogue", lambda db, hypervisor: None)
    node = LAB_PROFILE["nodes"][0]
    profile = json.loads(json.dumps(LAB_PROFILE))
    profile["module_ids"] = ["mod_006"]
    profile["nodes"] = [dict(node, name=f"n{i}") for i in range(count)]
    profile["health_checks"] = [{"node": f"n{i}", "kind": "ssh", "port": 22, "timeout_s": 300} for i in range(count)]
    profile["access"] = [{"node": "n0", "kind": "console"}]
    profile["evidence_checks"] = [{"id": "e", "node": "n0", "description": "x"}]
    profile["limits"] = {
        "max_vms": count,
        "vcpu_total": 2 * count,
        "ram_mb_total": 2048 * count,
        "disk_gb_total": 20 * count,
    }
    if ok:
        service.check_profile(None, profile, "mod_006")
    else:
        with pytest.raises(service.LabRefusedError, match="above this platform's limit"):
            service.check_profile(None, profile, "mod_006")
