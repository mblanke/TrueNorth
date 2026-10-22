"""Maintenance windows (ADR 0013 §7, range certification S7, issue #245).

Inside a window, provision, reset/restore and a person's normal destroy are refused with
409, the window's reason and its end time. Automated cleanup (a booking that ended, a lab's
end, expiry or failed build) and an authorised emergency teardown (range:admin, a reason,
audited) always run. Windows are per tenant, or platform-wide by a platform administrator.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.maintenance_windows import MaintenanceWindow
from app.maintenance_windows import service as windows
from app.models import AuditLog, Range, RangeSnapshot, RangeState, Template, Tenant, User, UserRole
from app.range_ops import RangeOperation
from app.scheduler import clock
from app.scheduler.models import EventState, ScheduledEvent

DEV = uuid.UUID("00000000-0000-0000-0000-000000000001")
VMS = json.dumps({"vms": [{"name": "r-dc01", "vm_id": "vm-1"}]})


# ── helpers ──────────────────────────────────────────────────────────────────
def _tenant(db, slug: str) -> uuid.UUID:
    t = Tenant(id=uuid.uuid4(), name=f"T {slug}", slug=slug)
    db.add(t)
    db.flush()
    return t.id


def _user(db, role: UserRole, tenant: uuid.UUID = DEV) -> User:
    u = User(
        id=uuid.uuid4(), email=f"{role.value}-{uuid.uuid4().hex[:6]}@example.test", display_name=role.value,
        role=role, tenant_id=tenant, keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    db.add(u)
    db.flush()
    return u


@contextmanager
def acting_as(db, role: UserRole, tenant: uuid.UUID = DEV):
    u = _user(db, role, tenant)
    who = CurrentUser(
        id=str(u.id), email=u.email, display_name=u.display_name, role=role, tenant_id=str(tenant),
        keycloak_id=u.keycloak_id,
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _range(db, state: RangeState = RangeState.created, *, tenant: uuid.UUID = DEV, output: str | None = None,
           tags: list[str] | None = None) -> Range:
    yml = "assets: []\n" + (f"tags: {json.dumps(tags)}\n" if tags else "")
    t = Template(id=uuid.uuid4(), name=f"t-{uuid.uuid4().hex[:6]}", version="1.0", yaml=yml, tenant_id=tenant)
    db.add(t)
    db.flush()
    r = Range(id=uuid.uuid4(), name=f"r-{uuid.uuid4().hex[:6]}", template_id=t.id, tenant_id=tenant,
              state=state, provisioner_backend="mock", provisioner_output=output)
    db.add(r)
    db.commit()
    return r


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    return datetime.now(UTC)


def _window(client, *, start: datetime | None = None, end: datetime | None = None, **extra) -> dict:
    body = {
        "reason": "vCenter upgrade",
        "starts_at": _iso(start or _now() - timedelta(hours=1)),
        "ends_at": _iso(end or _now() + timedelta(hours=2)),
        **extra,
    }
    resp = client.post("/maintenance-windows", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _ops(db, rng: Range) -> list[RangeOperation]:
    return db.query(RangeOperation).filter(RangeOperation.range_id == rng.id).order_by(RangeOperation.generation).all()


def _audits(db, action: str) -> list[AuditLog]:
    return db.query(AuditLog).filter(AuditLog.action == action).all()


@pytest.fixture
def platform_admin(monkeypatch):
    """The dev admin (AUTH_DISABLED) is the platform administrator."""
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(DEV))


# ── refusals inside a window, per action ─────────────────────────────────────
class TestRefusedInsideAWindow:
    def test_provision_is_refused_with_the_reason_and_end_time(self, client, db_session, no_real_broker):
        rng = _range(db_session)
        w = _window(client)
        resp = client.post(f"/ranges/{rng.id}/provision")
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "maintenance window" in detail and "vCenter upgrade" in detail
        assert datetime.fromisoformat(w["ends_at"]).isoformat() in detail
        assert 0 < int(resp.headers["Retry-After"]) <= 2 * 3600
        db_session.refresh(rng)
        assert rng.state == RangeState.created and _ops(db_session, rng) == [] and no_real_broker.sent == []

    def test_a_normal_destroy_is_refused(self, client, db_session, no_real_broker):
        rng = _range(db_session, RangeState.ready, output=VMS)
        _window(client)
        resp = client.post(f"/ranges/{rng.id}/destroy")
        assert resp.status_code == 409 and "Destroying is refused" in resp.json()["detail"]
        db_session.refresh(rng)
        assert rng.state == RangeState.ready and no_real_broker.sent == []

    def test_a_snapshot_restore_is_refused(self, client, db_session, no_real_broker):
        rng = _range(db_session, RangeState.ready, output=VMS)
        snap = RangeSnapshot(range_id=rng.id, name="base", tenant_id=DEV, snapshot_state="ready",
                             range_state_at_snapshot="ready")
        db_session.add(snap)
        db_session.commit()
        _window(client)
        resp = client.post(f"/ranges/{rng.id}/snapshots/{snap.id}/restore")
        assert resp.status_code == 409 and "Restoring a snapshot is refused" in resp.json()["detail"]
        db_session.refresh(snap)
        assert snap.snapshot_state == "ready" and no_real_broker.sent == []

    def test_batch_provision_is_refused_whole(self, client, db_session, no_real_broker):
        a, b = _range(db_session), _range(db_session)
        _window(client, scope="ranges", range_ids=[str(b.id)])
        resp = client.post("/ranges/batch-provision", json={"range_ids": [str(a.id), str(b.id)]})
        assert resp.status_code == 409 and "maintenance window" in resp.json()["detail"]
        assert db_session.query(RangeOperation).count() == 0, "one refusal accepts none"

    def test_stop_and_start_are_not_windowed(self, client, db_session, no_real_broker):
        rng = _range(db_session, RangeState.ready, output=VMS)
        _window(client)
        assert client.post(f"/ranges/{rng.id}/stop").status_code == 202

    def test_outside_the_window_everything_runs(self, client, db_session, no_real_broker):
        rng = _range(db_session)
        _window(client, start=_now() + timedelta(hours=1), end=_now() + timedelta(hours=3))  # not yet
        assert client.post(f"/ranges/{rng.id}/provision").status_code == 202

    def test_deleting_the_window_ends_it(self, client, db_session, no_real_broker):
        rng = _range(db_session)
        w = _window(client)
        assert client.post(f"/ranges/{rng.id}/provision").status_code == 409
        assert client.delete(f"/maintenance-windows/{w['id']}").status_code == 204
        assert client.post(f"/ranges/{rng.id}/provision").status_code == 202
        assert _audits(db_session, "delete")[-1].resource_type == "maintenance_window"


class TestScope:
    def test_a_ranges_window_covers_only_its_ranges(self, client, db_session, no_real_broker):
        held, free = _range(db_session), _range(db_session)
        _window(client, scope="ranges", range_ids=[str(held.id)])
        assert client.post(f"/ranges/{held.id}/provision").status_code == 409
        assert client.post(f"/ranges/{free.id}/provision").status_code == 202

    def test_a_tags_window_covers_ranges_whose_template_declares_the_tag(self, client, db_session, no_real_broker):
        soc, other = _range(db_session, tags=["SOC", "blue"]), _range(db_session, tags=["red"])
        _window(client, scope="tags", tags=["soc"])
        assert client.post(f"/ranges/{soc.id}/provision").status_code == 409
        assert client.post(f"/ranges/{other.id}/provision").status_code == 202

    @pytest.mark.parametrize(
        "body",
        [
            {"scope": "ranges"},
            {"scope": "tags"},
            {"scope": "all", "tags": ["x"]},
            {"scope": "all", "range_ids": [str(uuid.uuid4())]},
            {"scope": "everything"},
        ],
    )
    def test_an_inconsistent_scope_is_422(self, client, body):
        resp = client.post("/maintenance-windows", json={
            "reason": "x" * 5, "starts_at": _iso(_now()), "ends_at": _iso(_now() + timedelta(hours=1)), **body,
        })
        assert resp.status_code == 422


# ── bypasses: always run inside a window ────────────────────────────────────
class TestEmergencyTeardown:
    def test_it_runs_inside_a_window_is_audited_and_marked_for_the_worker(self, client, db_session, no_real_broker):
        rng = _range(db_session, RangeState.ready, output=VMS)
        w = _window(client)
        reason = "Range is attacking the management network; pulling it now"
        resp = client.post(f"/ranges/{rng.id}/emergency-teardown",
                           json={"confirm_range_id": str(rng.id), "reason": reason})
        assert resp.status_code == 202, resp.text
        assert resp.json()["state"] == "destroying"
        op = db_session.get(RangeOperation, uuid.UUID(resp.headers["Operation-Id"]))
        assert (op.action, op.window_bypass) == ("destroy", "emergency")
        assert no_real_broker.sent == [("worker.tasks.destroy_range", [str(rng.id)])]
        (entry,) = _audits(db_session, "emergency_teardown")
        assert entry.resource_id == str(rng.id) and reason in entry.detail
        assert "overrode maintenance window" in entry.detail and "vCenter upgrade" in entry.detail
        assert entry.user_id == DEV
        # the operation, as clients read it, says why it ran
        listed = client.get(f"/ranges/{rng.id}/operations").json()
        assert listed[0]["window_bypass"] == "emergency"
        assert w["active"] is True

    def test_it_also_runs_with_no_window(self, client, db_session, no_real_broker):
        rng = _range(db_session, RangeState.failed)
        resp = client.post(f"/ranges/{rng.id}/emergency-teardown",
                           json={"confirm_range_id": str(rng.id), "reason": "stuck failed build, clearing it"})
        assert resp.status_code == 202
        assert "no maintenance window in effect" in _audits(db_session, "emergency_teardown")[0].detail

    @pytest.mark.parametrize(
        "body", [{}, {"reason": "too short"}, {"reason": ""}, {"reason": "          "}, {"reason": "a b c d e f g h i"}]
    )
    def test_a_reason_is_mandatory(self, client, db_session, body):
        rng = _range(db_session, RangeState.ready, output=VMS)
        resp = client.post(f"/ranges/{rng.id}/emergency-teardown", json={"confirm_range_id": str(rng.id), **body})
        assert resp.status_code == 422
        assert _audits(db_session, "emergency_teardown") == [] and _ops(db_session, rng) == []

    def test_the_range_id_must_be_repeated(self, client, db_session):
        rng = _range(db_session, RangeState.ready, output=VMS)
        resp = client.post(f"/ranges/{rng.id}/emergency-teardown",
                           json={"confirm_range_id": str(uuid.uuid4()), "reason": "a good enough reason"})
        assert resp.status_code == 422 and _ops(db_session, rng) == []

    @pytest.mark.parametrize("role", [UserRole.instructor, UserRole.range_ops, UserRole.student, UserRole.observer])
    def test_without_range_admin_it_is_refused(self, client, db_session, no_real_broker, role):
        rng = _range(db_session, RangeState.ready, output=VMS)
        _window(client)
        with acting_as(db_session, role):
            resp = client.post(f"/ranges/{rng.id}/emergency-teardown",
                               json={"confirm_range_id": str(rng.id), "reason": "I really want it gone now"})
            assert resp.status_code == 403
            if role in (UserRole.instructor, UserRole.range_ops):  # range:destroy holders, still refused
                assert client.post(f"/ranges/{rng.id}/destroy").status_code == 409
        assert _ops(db_session, rng) == [] and no_real_broker.sent == []

    def test_another_tenants_range_is_not_found(self, client, db_session):
        other = _tenant(db_session, "other-org")
        rng = _range(db_session, RangeState.ready, tenant=other, output=VMS)
        resp = client.post(f"/ranges/{rng.id}/emergency-teardown",
                           json={"confirm_range_id": str(rng.id), "reason": "not mine to tear down"})
        assert resp.status_code == 404 and _ops(db_session, rng) == []

    def test_it_obeys_the_state_machine_like_any_destroy(self, client, db_session):
        rng = _range(db_session, RangeState.destroyed)
        resp = client.post(f"/ranges/{rng.id}/emergency-teardown",
                           json={"confirm_range_id": str(rng.id), "reason": "already gone, try anyway"})
        assert resp.status_code == 409 and _audits(db_session, "emergency_teardown") == []

    def test_it_is_written_to_the_ranges_certification_record(self, client, db_session, no_real_broker):
        """ADR 0013 §6/§7: who, why, the operation and the window, as an override entry."""
        from app.range_certification import service as certification

        rng = _range(db_session)
        assert client.post(f"/ranges/{rng.id}/provision").status_code == 202  # opens the record (S6)
        rng.state, rng.provisioner_output = RangeState.ready, VMS
        db_session.commit()
        w = _window(client)
        reason = "Range is attacking the management network; pulling it now"
        resp = client.post(f"/ranges/{rng.id}/emergency-teardown", json={"confirm_range_id": str(rng.id),
                                                                          "reason": reason})
        assert resp.status_code == 202
        record = certification.latest_record(db_session, rng.tenant_id, rng.id)
        assert record is not None
        [entry] = [e for e in certification.entries_of(db_session, record) if e.section == "overrides"]
        assert entry.reason == reason and entry.detail["kind"] == "override"
        assert entry.detail["action"] == "emergency_teardown"
        assert entry.detail["operation_id"] == resp.headers["Operation-Id"]
        assert entry.detail["maintenance_window_id"] == w["id"]

    def test_it_writes_the_certification_record_through_the_s6_hook(self, client, db_session, monkeypatch):
        from app.maintenance_windows import router

        seen = []
        monkeypatch.setattr(router, "record_override", lambda db, rng, user, reason, detail: seen.append(
            (rng.id, reason, detail)))
        rng = _range(db_session, RangeState.ready, output=VMS)
        w = _window(client)
        client.post(f"/ranges/{rng.id}/emergency-teardown",
                    json={"confirm_range_id": str(rng.id), "reason": "the reason recorded twice"})
        assert seen and seen[0][0] == rng.id and seen[0][1] == "the reason recorded twice"
        assert seen[0][2]["maintenance_window_id"] == w["id"]


class TestHeldOperations:
    """The worker marks an operation that reached it inside a window ``held``
    (worker/maintenance_windows.py): the API's re-send loop sends it once the window ends,
    and held operations never crowd out anyone else's re-send."""

    @staticmethod
    def _hold(db, op_id: str) -> RangeOperation:
        op = db.get(RangeOperation, uuid.UUID(op_id))
        op.status, op.error = "held", {"code": "maintenance_window", "message": "held"}  # as the worker does
        db.commit()
        return op

    def test_a_held_operation_is_sent_only_after_the_window(self, client, db_session, no_real_broker):
        from app.range_ops import service as ops

        rng = _range(db_session, RangeState.ready, output=VMS)
        op_id = client.post(f"/ranges/{rng.id}/destroy").headers["Operation-Id"]
        w = _window(client)
        self._hold(db_session, op_id)
        no_real_broker.sent.clear()
        assert ops.redispatch_pending(db_session, min_age=timedelta(0)) == 0 and no_real_broker.sent == []
        db_session.refresh(rng)
        assert rng.state == RangeState.destroying, "still in progress, not failed"
        assert client.post(f"/ranges/{rng.id}/stop").status_code == 409, "a held operation is still in flight"
        client.delete(f"/maintenance-windows/{w['id']}")
        assert ops.redispatch_pending(db_session, min_age=timedelta(0)) == 1
        assert no_real_broker.sent == [("worker.tasks.destroy_range", [str(rng.id)])]
        assert db_session.get(RangeOperation, uuid.UUID(op_id)).status == "dispatched"

    def test_held_operations_never_starve_another_tenants_resend(self, client, db_session, no_real_broker):
        """Review of #261 (PoC): 50 held for tenant A took every re-send slot from tenant B."""
        from app.range_ops import service as ops

        a, b = _tenant(db_session, "a-org"), _tenant(db_session, "b-org")
        with acting_as(db_session, UserRole.admin, a):
            ids = [client.post(f"/ranges/{_range(db_session, RangeState.ready, tenant=a, output=VMS).id}/destroy")
                   .headers["Operation-Id"] for _ in range(ops.HELD_PAGE + 10)]  # more than one page
            _window(client, end=_now() + timedelta(hours=8))
        for op_id in ids:
            self._hold(db_session, op_id)
        victim = _range(db_session, RangeState.ready, tenant=b, output=VMS)
        with acting_as(db_session, UserRole.admin, b):
            vop = client.post(f"/ranges/{victim.id}/destroy").headers["Operation-Id"]
        db_session.get(RangeOperation, uuid.UUID(vop)).status = "pending"  # its broker send failed
        db_session.commit()
        no_real_broker.sent.clear()
        assert ops.redispatch_pending(db_session, min_age=timedelta(0), limit=50) == 1
        assert no_real_broker.sent == [("worker.tasks.destroy_range", [str(victim.id)])]

    def test_a_released_hold_is_found_past_a_page_of_still_held_ones(self, client, db_session, no_real_broker):
        from app.range_ops import service as ops

        a, b = _tenant(db_session, "a-org"), _tenant(db_session, "b-org")
        with acting_as(db_session, UserRole.admin, a):
            ids = [client.post(f"/ranges/{_range(db_session, RangeState.ready, tenant=a, output=VMS).id}/destroy")
                   .headers["Operation-Id"] for _ in range(ops.HELD_PAGE + 5)]
            _window(client, end=_now() + timedelta(hours=8))
        with acting_as(db_session, UserRole.admin, b):
            freed = _range(db_session, RangeState.ready, tenant=b, output=VMS)
            last = client.post(f"/ranges/{freed.id}/destroy").headers["Operation-Id"]  # newest of all
        for i, op_id in enumerate([*ids, last]):  # oldest first; b's is last, past the first page
            self._hold(db_session, op_id).created_at = _now() - timedelta(hours=2) + timedelta(seconds=i)
        db_session.commit()
        no_real_broker.sent.clear()
        assert ops.redispatch_pending(db_session, min_age=timedelta(0)) == 1
        assert no_real_broker.sent == [("worker.tasks.destroy_range", [str(freed.id)])]

    def test_a_task_held_before_the_send_is_recorded_is_not_overwritten(self, client, db_session, monkeypatch):
        """Review of #261: dispatch() marks ``dispatched`` after the send; a worker that held the
        task in between must win, or the operation would be stuck ``dispatched``."""
        from app import celery_client
        from app.range_ops import service as ops

        rng = _range(db_session, RangeState.ready, output=VMS)
        monkeypatch.setattr(ops, "dispatch", lambda db, op: False)  # accepted, not yet sent
        op_id = client.post(f"/ranges/{rng.id}/destroy").headers["Operation-Id"]
        monkeypatch.undo()

        def send_and_the_worker_holds(name, *args):
            db_session.get(RangeOperation, uuid.UUID(op_id)).status = "held"  # the task arrived first
            db_session.flush()
            return "task-1"

        monkeypatch.setattr(celery_client, "dispatch", send_and_the_worker_holds)
        ops.dispatch(db_session, db_session.get(RangeOperation, uuid.UUID(op_id)))
        assert db_session.get(RangeOperation, uuid.UUID(op_id)).status == "held"

    def test_a_bypassed_operation_is_never_held(self, client, db_session, no_real_broker):
        from app.range_ops import service as ops

        rng = _range(db_session, RangeState.ready, output=VMS)
        _window(client)
        op_id = client.post(f"/ranges/{rng.id}/emergency-teardown",
                            json={"confirm_range_id": str(rng.id), "reason": "pull it now, it is misbehaving"}
                            ).headers["Operation-Id"]
        db_session.get(RangeOperation, uuid.UUID(op_id)).status = "pending"
        db_session.commit()
        assert ops.redispatch_pending(db_session, min_age=timedelta(0)) == 1


class TestParkedRanges:
    """Review of #261: a range whose work a window holds can still be torn down."""

    def test_emergency_teardown_of_a_held_build(self, client, db_session, no_real_broker):
        rng = _range(db_session)
        op_id = client.post(f"/ranges/{rng.id}/provision").headers["Operation-Id"]
        _window(client, end=_now() + timedelta(hours=8))
        TestHeldOperations._hold(db_session, op_id)  # the worker held it; the range stays provisioning
        resp = client.post(f"/ranges/{rng.id}/emergency-teardown",
                           json={"confirm_range_id": str(rng.id), "reason": "pull it now, it is misbehaving"})
        assert resp.status_code == 202 and resp.json()["state"] == "destroying"
        held = db_session.get(RangeOperation, uuid.UUID(op_id))
        assert held.status == "superseded"
        assert _audits(db_session, "emergency_teardown")

    def test_emergency_teardown_of_a_build_never_picked_up(self, client, db_session, monkeypatch):
        """Pending (the broker was down), no worker lease: nothing runs, so it may come down."""
        from app.range_ops import service as ops

        rng = _range(db_session)
        monkeypatch.setattr(ops, "dispatch", lambda db, op: False)  # never sent
        client.post(f"/ranges/{rng.id}/provision")
        monkeypatch.undo()
        resp = client.post(f"/ranges/{rng.id}/emergency-teardown",
                           json={"confirm_range_id": str(rng.id), "reason": "pull it now, it is misbehaving"})
        assert resp.status_code == 202 and resp.json()["state"] == "destroying"

    def test_a_build_a_worker_holds_the_lease_for_is_not_parked(self, client, db_session, no_real_broker):
        from app.range_leases import RangeLease

        rng = _range(db_session)
        op_id = client.post(f"/ranges/{rng.id}/provision").headers["Operation-Id"]
        TestHeldOperations._hold(db_session, op_id)
        db_session.add(RangeLease(range_id=rng.id, holder="provision:abc", expires_at=_now() + timedelta(minutes=3)))
        db_session.commit()
        resp = client.post(f"/ranges/{rng.id}/emergency-teardown",
                           json={"confirm_range_id": str(rng.id), "reason": "pull it now, it is misbehaving"})
        assert resp.status_code == 409

    def test_a_build_that_is_running_is_not_parked(self, client, db_session, no_real_broker):
        rng = _range(db_session)
        client.post(f"/ranges/{rng.id}/provision")  # dispatched: a worker may be building it
        _window(client)
        resp = client.post(f"/ranges/{rng.id}/emergency-teardown",
                           json={"confirm_range_id": str(rng.id), "reason": "pull it now, it is misbehaving"})
        assert resp.status_code == 409

    def test_a_teardown_nothing_identified_has_a_way_out(self, client, db_session, no_real_broker):
        from app.maintenance_windows.policy import HELD_MARKER

        rng = _range(db_session, RangeState.destroying, output=VMS)
        rng.error_message = f"Destroying {HELD_MARKER} is in effect until ...: SAN firmware."  # as the worker
        db_session.commit()
        _window(client)
        resp = client.post(f"/ranges/{rng.id}/emergency-teardown",
                           json={"confirm_range_id": str(rng.id), "reason": "clear the held teardown now"})
        assert resp.status_code == 202
        assert "overrode maintenance window" in _audits(db_session, "emergency_teardown")[0].detail

    def test_force_release_explains_a_held_operation(self, client, db_session, no_real_broker):
        from app.range_leases import RangeLease

        rng = _range(db_session, RangeState.ready, output=VMS)
        op_id = client.post(f"/ranges/{rng.id}/destroy").headers["Operation-Id"]
        _window(client)
        TestHeldOperations._hold(db_session, op_id)
        db_session.add(RangeLease(range_id=rng.id, holder="abandoned:stop:abc",
                                  expires_at=_now() - timedelta(minutes=1)))
        db_session.commit()
        status = client.get(f"/ranges/{rng.id}/lease").json()
        assert status["force_release_allowed"] is False
        assert "held by a maintenance window" in status["reason"] and op_id in status["reason"]
        assert "Abandon the held operation first" in status["reason"]

    def test_a_destroying_range_without_the_hold_reason_is_not_parked(self, client, db_session):
        rng = _range(db_session, RangeState.destroying, output=VMS)
        resp = client.post(f"/ranges/{rng.id}/emergency-teardown",
                           json={"confirm_range_id": str(rng.id), "reason": "clear the held teardown now"})
        assert resp.status_code == 409


class TestSchedulerBypass:
    START = (datetime.now(UTC) + timedelta(days=30)).replace(hour=13, minute=0, second=0, microsecond=0)

    def _booking(self, db, rng: Range, state=EventState.scheduled, auto=False) -> ScheduledEvent:
        ev = ScheduledEvent(name="Drill", state=state, tenant_id=DEV, range_id=rng.id, start_time=self.START,
                            end_time=self.START + timedelta(hours=3), auto_provisioned=auto)
        db.add(ev)
        db.commit()
        return ev

    def test_the_teardown_of_a_booking_that_ended_runs_inside_a_window(self, client, db_session, no_real_broker):
        rng = _range(db_session, RangeState.ready, output=VMS)
        self._booking(db_session, rng, EventState.active, auto=True)
        _window(client)
        res = clock.tick(db_session, now=self.START + timedelta(hours=4))
        assert res.completed == 1
        db_session.refresh(rng)
        assert rng.state == RangeState.destroying
        assert _ops(db_session, rng)[-1].window_bypass == "booking_ended"

    def test_a_booking_end_takes_over_a_held_persons_destroy(self, client, db_session, no_real_broker):
        """Third review of #261: the range was already destroying (a person's destroy, held by the
        window), so the booking's teardown counted as settled and nothing ran until the window ended."""
        rng = _range(db_session, RangeState.ready, output=VMS)
        self._booking(db_session, rng, EventState.active, auto=True)
        op_id = client.post(f"/ranges/{rng.id}/destroy").headers["Operation-Id"]  # before the window
        _window(client)
        TestHeldOperations._hold(db_session, op_id)  # the worker held it: accepted inside the window
        no_real_broker.sent.clear()
        res = clock.tick(db_session, now=self.START + timedelta(hours=4))
        assert res.completed == 1
        op = db_session.get(RangeOperation, uuid.UUID(op_id))
        assert (op.window_bypass, op.status) == ("booking_ended", "dispatched")
        assert no_real_broker.sent == [("worker.tasks.destroy_range", [str(rng.id)])]
        assert len(_ops(db_session, rng)) == 1, "the same teardown, now with the bypass; not a second one"

    def test_a_cancelled_bookings_teardown_waits_for_the_window(self, client, db_session, no_real_broker):
        rng = _range(db_session, RangeState.ready, output=VMS)
        ev = self._booking(db_session, rng, EventState.scheduled, auto=True)
        w = _window(client)
        assert client.post(f"/schedule/events/{ev.id}/cancel").status_code == 200
        db_session.refresh(rng)
        db_session.refresh(ev)
        assert rng.state == RangeState.ready and ev.auto_provisioned is True, "a person's teardown: deferred"
        client.delete(f"/maintenance-windows/{w['id']}")
        clock.tick(db_session)  # step 3b retries
        db_session.refresh(rng)
        assert rng.state == RangeState.destroying and _ops(db_session, rng)[-1].window_bypass is None

    def test_the_clock_does_not_build_inside_a_window_and_builds_after_it(self, client, db_session, no_real_broker):
        rng = _range(db_session)
        ev = self._booking(db_session, rng)
        lead = self.START - timedelta(minutes=30)
        _window(client, start=lead - timedelta(hours=1), end=lead + timedelta(minutes=10))
        res = clock.tick(db_session, now=lead)
        assert res.deferred_by_window == 1 and res.provisioning == 0
        db_session.refresh(ev)
        assert ev.state == EventState.scheduled and no_real_broker.sent == []
        res = clock.tick(db_session, now=lead + timedelta(minutes=10))  # the window has ended: [start, end)
        assert res.provisioning == 1 and res.deferred_by_window == 0

    def test_a_session_starting_during_a_window_is_built_once_it_ends(self, client, db_session, no_real_broker):
        """Review of #261 (PoC): the clock moved such a booking to active and never built it."""
        rng = _range(db_session)
        ev = self._booking(db_session, rng)
        lead = self.START - timedelta(minutes=30)
        _window(client, start=lead - timedelta(hours=1), end=self.START + timedelta(minutes=30))
        assert clock.tick(db_session, now=lead).deferred_by_window == 1
        res = clock.tick(db_session, now=self.START)  # the session's start, inside the window
        db_session.refresh(ev)
        assert ev.state == EventState.scheduled and res.deferred_by_window == 1 and res.activated == 0
        res = clock.tick(db_session, now=self.START + timedelta(minutes=31))  # the window is over
        db_session.refresh(ev)
        db_session.refresh(rng)
        assert res.provisioning == 1 and res.activated == 1 and res.deferred_by_window == 0
        assert ev.state == EventState.active and ev.auto_provisioned is True
        assert rng.state == RangeState.provisioning
        assert [n for n, _ in no_real_broker.sent] == ["worker.tasks.provision_range"]

    def test_a_session_held_for_its_whole_length_completes_without_a_build(self, client, db_session, no_real_broker):
        rng = _range(db_session)
        ev = self._booking(db_session, rng)
        _window(client, start=self.START - timedelta(hours=2), end=self.START + timedelta(hours=10))
        clock.tick(db_session, now=self.START)
        clock.tick(db_session, now=self.START + timedelta(hours=4))  # the session is over
        db_session.refresh(ev)
        assert ev.state == EventState.completed and no_real_broker.sent == []

    def test_an_active_booking_whose_range_was_never_built_is_built_after_the_window(
        self, client, db_session, no_real_broker
    ):
        rng = _range(db_session)  # created: e.g. went active under the old clock, or a held build came back
        ev = self._booking(db_session, rng, EventState.active)
        _window(client, start=self.START - timedelta(hours=1), end=self.START + timedelta(minutes=30))
        res = clock.tick(db_session, now=self.START + timedelta(minutes=10))
        assert res.built_after_window == 0 and res.deferred_by_window == 1
        res = clock.tick(db_session, now=self.START + timedelta(minutes=30))
        db_session.refresh(ev)
        db_session.refresh(rng)
        assert res.built_after_window == 1 and ev.auto_provisioned is True and rng.state == RangeState.provisioning
        assert _audits(db_session, "build_after_window")

    def test_a_range_already_up_is_not_held(self, client, db_session, no_real_broker):
        rng = _range(db_session, RangeState.ready, output=VMS)
        self._booking(db_session, rng)
        lead = self.START - timedelta(minutes=30)
        _window(client, start=lead - timedelta(hours=1), end=lead + timedelta(hours=1))
        assert clock.tick(db_session, now=lead).deferred_by_window == 0

    def test_a_booking_overlapping_a_window_is_made_with_a_warning(self, client, db_session):
        rng = _range(db_session)
        start = self.START + timedelta(days=1)
        _window(client, start=start + timedelta(hours=1), end=start + timedelta(hours=5))
        body = {"name": "Class", "start_time": _iso(start), "end_time": _iso(start + timedelta(hours=2)),
                "range_id": str(rng.id)}
        resp = client.post("/schedule/events", json=body)
        assert resp.status_code == 201, resp.text
        (warning,) = resp.json()["maintenance_warnings"]
        assert "vCenter upgrade" in warning and "built on the scheduler's first tick after the window ends" in warning
        assert resp.json()["warnings"] == [], "not an over-capacity warning"
        later = {**body, "start_time": _iso(start + timedelta(hours=6)), "end_time": _iso(start + timedelta(hours=8))}
        assert client.post("/schedule/events", json=later).json()["maintenance_warnings"] == []

    def test_the_build_lead_counts_as_overlap(self, client, db_session):
        rng = _range(db_session)
        start = self.START + timedelta(days=2)
        _window(client, start=start - timedelta(hours=2), end=start - timedelta(minutes=10))  # ends before start
        body = {"name": "Class", "start_time": _iso(start), "end_time": _iso(start + timedelta(hours=2)),
                "range_id": str(rng.id)}
        assert client.post("/schedule/events", json=body).json()["maintenance_warnings"], "built 30 min before"


# ── administration, permissions and tenancy ─────────────────────────────────
class TestAdministration:
    def test_create_list_get_update_delete_all_audited(self, client, db_session):
        w = _window(client)
        assert w["platform_wide"] is False and w["tenant_id"] == str(DEV) and w["can_edit"] is True
        assert client.get("/maintenance-windows").json()["total"] == 1
        assert client.get(f"/maintenance-windows/{w['id']}").json()["reason"] == "vCenter upgrade"
        body = {"reason": "vCenter 8 upgrade", "starts_at": w["starts_at"], "ends_at": w["ends_at"]}
        upd = client.put(f"/maintenance-windows/{w['id']}", json=body)
        assert upd.status_code == 200 and upd.json()["reason"] == "vCenter 8 upgrade"
        assert client.delete(f"/maintenance-windows/{w['id']}").status_code == 204
        assert client.get(f"/maintenance-windows/{w['id']}").status_code == 404
        actions = [a.action for a in db_session.query(AuditLog).filter(AuditLog.resource_type == "maintenance_window")]
        assert actions == ["create", "update", "delete"]

    def test_ended_windows_are_listed_only_on_request(self, client, db_session):
        w = _window(client)
        db_session.get(MaintenanceWindow, uuid.UUID(w["id"])).ends_at = _now() - timedelta(minutes=1)
        db_session.commit()
        assert client.get("/maintenance-windows").json()["total"] == 0
        assert client.get("/maintenance-windows?include_past=true").json()["total"] == 1

    @pytest.mark.parametrize("role", [UserRole.instructor, UserRole.range_ops, UserRole.observer, UserRole.student])
    def test_only_range_admin_writes(self, client, db_session, role):
        with acting_as(db_session, role):
            resp = client.post("/maintenance-windows", json={
                "reason": "mine", "starts_at": _iso(_now()), "ends_at": _iso(_now() + timedelta(hours=1)),
            })
        assert resp.status_code == 403

    def test_students_see_the_banner_but_not_the_list(self, client, db_session):
        _window(client)
        with acting_as(db_session, UserRole.student):
            assert client.get("/maintenance-windows").status_code == 403
            active = client.get("/maintenance-windows/active")
        assert active.status_code == 200 and [w["reason"] for w in active.json()] == ["vCenter upgrade"]
        assert active.json()[0]["can_edit"] is False

    def test_active_for_a_range_lists_only_what_covers_it(self, client, db_session):
        held, free = _range(db_session), _range(db_session)
        _window(client, scope="ranges", range_ids=[str(held.id)])
        assert len(client.get(f"/maintenance-windows/active?range_id={held.id}").json()) == 1
        assert client.get(f"/maintenance-windows/active?range_id={free.id}").json() == []

    def test_a_window_cannot_end_in_the_past(self, client):
        resp = client.post("/maintenance-windows", json={
            "reason": "late", "starts_at": _iso(_now() - timedelta(hours=3)), "ends_at": _iso(_now() - timedelta(hours=1)),
        })
        assert resp.status_code == 422 and "past" in resp.json()["detail"]

    def test_bringing_an_ended_window_back_counts_against_the_cap(self, client, db_session):
        ended = _window(client)
        db_session.get(MaintenanceWindow, uuid.UUID(ended["id"])).ends_at = _now() - timedelta(minutes=1)
        db_session.commit()
        for _ in range(windows.MAX_LIVE_WINDOWS):
            _window(client)
        body = {"reason": "back again", "starts_at": _iso(_now()), "ends_at": _iso(_now() + timedelta(hours=1))}
        resp = client.put(f"/maintenance-windows/{ended['id']}", json=body)
        assert resp.status_code == 422 and str(windows.MAX_LIVE_WINDOWS) in resp.json()["detail"]

    @pytest.mark.parametrize("reason", ["   ", " a  b ", "x"])
    def test_a_reason_needs_three_characters_besides_spaces(self, client, reason):
        resp = client.post("/maintenance-windows", json={
            "reason": reason, "starts_at": _iso(_now()), "ends_at": _iso(_now() + timedelta(hours=1)),
        })
        assert resp.status_code == 422

    def test_a_reason_is_stored_trimmed(self, client):
        w = _window(client, reason="  vCenter upgrade  ")
        assert w["reason"] == "vCenter upgrade"

    def test_platform_wide_is_fixed_once_created(self, client, db_session):
        w = _window(client)
        body = {"reason": "now global", "starts_at": w["starts_at"], "ends_at": w["ends_at"], "platform_wide": True}
        assert client.put(f"/maintenance-windows/{w['id']}", json=body).status_code == 422


class TestTenancy:
    def test_a_tenant_window_covers_only_its_tenants_ranges(self, client, db_session, no_real_broker):
        other = _tenant(db_session, "other-org")
        theirs = _range(db_session, tenant=other)
        _window(client)  # the dev tenant's
        with acting_as(db_session, UserRole.admin, other):
            assert client.post(f"/ranges/{theirs.id}/provision").status_code == 202

    def test_another_tenants_window_is_invisible(self, client, db_session):
        other = _tenant(db_session, "other-org")
        with acting_as(db_session, UserRole.admin, other):
            theirs = _window(client)
        assert client.get(f"/maintenance-windows/{theirs['id']}").status_code == 404
        assert client.get("/maintenance-windows").json()["total"] == 0
        body = {"reason": "hijack", "starts_at": theirs["starts_at"], "ends_at": theirs["ends_at"]}
        assert client.put(f"/maintenance-windows/{theirs['id']}", json=body).status_code == 404
        assert client.delete(f"/maintenance-windows/{theirs['id']}").status_code == 404

    def test_a_tenant_window_may_not_name_another_tenants_range(self, client, db_session):
        other = _tenant(db_session, "other-org")
        theirs = _range(db_session, tenant=other)
        resp = client.post("/maintenance-windows", json={
            "reason": "reach", "starts_at": _iso(_now()), "ends_at": _iso(_now() + timedelta(hours=1)),
            "scope": "ranges", "range_ids": [str(theirs.id)],
        })
        assert resp.status_code == 404

    def test_platform_wide_needs_a_platform_admin(self, client, db_session):
        _tenant(db_session, "second-tenant")  # two tenants, PLATFORM_TENANT_ID unset: nobody is
        resp = client.post("/maintenance-windows", json={
            "reason": "all of it", "starts_at": _iso(_now()), "ends_at": _iso(_now() + timedelta(hours=1)),
            "platform_wide": True,
        })
        assert resp.status_code == 403

    def test_a_platform_window_covers_every_tenant_and_only_its_admin_edits_it(
        self, client, db_session, no_real_broker, platform_admin
    ):
        other = _tenant(db_session, "other-org")
        theirs = _range(db_session, tenant=other)
        w = _window(client, platform_wide=True)
        assert w["platform_wide"] is True and w["tenant_id"] is None
        with acting_as(db_session, UserRole.admin, other):
            resp = client.post(f"/ranges/{theirs.id}/provision")
            assert resp.status_code == 409 and "vCenter upgrade" in resp.json()["detail"]
            seen = client.get(f"/maintenance-windows/{w['id']}").json()
            assert seen["can_edit"] is False
            body = {"reason": "not yours", "starts_at": w["starts_at"], "ends_at": w["ends_at"],
                    "platform_wide": True}
            assert client.put(f"/maintenance-windows/{w['id']}", json=body).status_code == 403
            assert client.delete(f"/maintenance-windows/{w['id']}").status_code == 403

    def test_a_tenant_cannot_crowd_out_a_platform_window(self, client, db_session, no_real_broker, platform_admin):
        """Review of #261 (PoC): a LIMIT before scope matching let decoys hide the platform window."""
        other = _tenant(db_session, "other-org")
        _window(client, platform_wide=True, end=_now() + timedelta(hours=2))
        decoy, target = _range(db_session, tenant=other), _range(db_session, tenant=other)
        with acting_as(db_session, UserRole.admin, other):
            for _ in range(windows.MAX_LIVE_WINDOWS):
                _window(client, scope="ranges", range_ids=[str(decoy.id)], end=_now() + timedelta(days=3650))
            resp = client.post(f"/ranges/{target.id}/provision")
            assert resp.status_code == 409 and "vCenter upgrade" in resp.json()["detail"]

    def test_a_tenant_may_hold_only_so_many_live_windows(self, client, db_session):
        for _ in range(windows.MAX_LIVE_WINDOWS):
            _window(client)
        resp = client.post("/maintenance-windows", json={
            "reason": "one too many", "starts_at": _iso(_now()), "ends_at": _iso(_now() + timedelta(hours=1)),
        })
        assert resp.status_code == 422 and str(windows.MAX_LIVE_WINDOWS) in resp.json()["detail"]
        db_session.query(MaintenanceWindow).update({MaintenanceWindow.ends_at: _now() - timedelta(minutes=1)})
        db_session.commit()
        assert client.post("/maintenance-windows", json={
            "reason": "after the others ended", "starts_at": _iso(_now()), "ends_at": _iso(_now() + timedelta(hours=1)),
        }).status_code == 201, "ended windows do not count"

    def test_a_platform_window_shows_a_tenant_only_its_own_range_ids(self, client, db_session, platform_admin):
        other = _tenant(db_session, "other-org")
        mine, theirs = _range(db_session), _range(db_session, tenant=other)
        _window(client, platform_wide=True, scope="ranges", range_ids=[str(mine.id), str(theirs.id)])
        with acting_as(db_session, UserRole.admin, other):
            (seen,) = client.get("/maintenance-windows").json()["items"]
        assert seen["range_ids"] == [str(theirs.id)]


# ── time zones and edges ─────────────────────────────────────────────────────
class TestTimeZones:
    def test_a_time_without_a_zone_is_refused(self, client):
        resp = client.post("/maintenance-windows", json={
            "reason": "naive", "starts_at": "2099-01-01T10:00:00", "ends_at": "2099-01-01T12:00:00",
        })
        assert resp.status_code == 422

    def test_offsets_are_converted_to_utc(self, client):
        resp = client.post("/maintenance-windows", json={
            "reason": "zoned", "starts_at": "2099-01-01T10:00:00+05:00", "ends_at": "2099-01-01T12:00:00-04:00",
        })
        assert resp.status_code == 201
        w = resp.json()
        assert datetime.fromisoformat(w["starts_at"]) == datetime(2099, 1, 1, 5, 0, tzinfo=UTC)
        assert datetime.fromisoformat(w["ends_at"]) == datetime(2099, 1, 1, 16, 0, tzinfo=UTC)

    def test_end_before_start_after_conversion_is_422(self, client):
        # 10:00+05:00 is 05:00Z; 06:00+02:00 is 04:00Z, an hour before it.
        resp = client.post("/maintenance-windows", json={
            "reason": "backwards", "starts_at": "2099-01-01T10:00:00+05:00", "ends_at": "2099-01-01T06:00:00+02:00",
        })
        assert resp.status_code == 422

    def test_the_window_is_start_inclusive_end_exclusive(self, client, db_session, monkeypatch, no_real_broker):
        start = datetime(2099, 3, 1, 22, 0, tzinfo=UTC)
        end = start + timedelta(hours=2)
        _window(client, start=start, end=end)
        rng = _range(db_session)
        for at, refused in [
            (start - timedelta(microseconds=1), False), (start, True), (end - timedelta(microseconds=1), True),
            (end, False),
        ]:
            monkeypatch.setattr(windows, "_now", lambda at=at: at)
            assert bool(windows.covering_range(db_session, rng)) is refused, at

    def test_a_window_across_a_daylight_saving_change_keeps_its_real_length(self, client, db_session, monkeypatch):
        # America/Toronto falls back at 2099-11-01 02:00 EDT (-04:00) to 01:00 EST (-05:00).
        resp = client.post("/maintenance-windows", json={
            "reason": "overnight patching", "starts_at": "2099-11-01T00:00:00-04:00",
            "ends_at": "2099-11-01T04:00:00-05:00",
        })
        w = resp.json()
        start, end = datetime.fromisoformat(w["starts_at"]), datetime.fromisoformat(w["ends_at"])
        assert end - start == timedelta(hours=5)  # four wall-clock hours, five real ones
        rng = _range(db_session)
        monkeypatch.setattr(windows, "_now", lambda: datetime(2099, 11, 1, 8, 30, tzinfo=UTC))  # 03:30 EST
        assert windows.covering_range(db_session, rng)
        monkeypatch.setattr(windows, "_now", lambda: datetime(2099, 11, 1, 9, 0, tzinfo=UTC))  # 04:00 EST
        assert not windows.covering_range(db_session, rng)
