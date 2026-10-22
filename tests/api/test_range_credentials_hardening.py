"""ADR 0013 §3 (issue #241): rotation, admin rotate, lab session credentials, snapshots.

* a rebuild of a range built before gives every Student-visible generated account a new
  password; staff accounts and scenario accounts keep theirs;
* a snapshot restore (a lab reset is one) does the same in place: the guests still have the
  old password (``previous_password``) until the worker sets the new one, and a refused
  dispatch puts everything back;
* a snapshot records the passwords the accounts had (the rotation after a restore logs in
  with them);
* ``POST .../rotate`` is admin-only, audited with its reason, refuses a scenario account and
  a range no rotation can reach, and asks the worker only for a built, running range;
* a lab session's range gets its own generated logins, handed to its Student (owner only,
  signed in or by lab token), every reveal audited with the lab session.
"""

from __future__ import annotations

import json
import uuid

import pytest
import test_lab_sessions as _labs
from _shared import act_as, real_tenant, real_user
from app.lab_sessions import service as lab_service
from app.models import AuditLog, Range, RangeSnapshot, RangeState, Template, UserRole
from app.range_credentials import RangeCredential, RangeCredentialSnapshot
from app.range_ops import RangeOperation
from test_lab_sessions import act, launch, student, until
from test_range_credentials import ALL_ACCOUNTS, DC_PW, TEMPLATE_YAML

lab, worker = _labs.lab, _labs.worker  # the lab-session fixtures (a fake worker applies the tasks)
STUDENT_ROWS = {("web01", "analyst")}


@pytest.fixture
def world(db_session):
    a, b = real_tenant(db_session, "rot-a"), real_tenant(db_session, "rot-b")
    w = {"a": a, "b": b}
    for name, role, tenant in (("admin", UserRole.admin, a), ("instructor", UserRole.instructor, a),
                               ("range_ops", UserRole.range_ops, a), ("foreign_admin", UserRole.admin, b)):
        w[name] = real_user(db_session, role, tenant.id)
    return w


def _range(client, db, world, state: RangeState | None = None) -> Range:
    tmpl = Template(id=uuid.uuid4(), name="creds-lab", version="1", yaml=TEMPLATE_YAML, tenant_id=world["a"].id)
    db.add(tmpl)
    db.flush()
    act_as(world["instructor"])
    resp = client.post("/ranges", json={"name": "Rot", "template_id": str(tmpl.id)})
    assert resp.status_code == 201, resp.text
    rng = db.get(Range, uuid.UUID(resp.json()["id"]))
    if state is not None:
        rng.state, rng.provisioner_backend = state, "vsphere_api"  # a backend that changes guest passwords
        rng.provisioner_output = json.dumps({"vms": [{"vm_id": "vm-1", "name": f"{str(rng.id)[:8]}-web01",
                                                      "node_id": "web01", "family": "linux"}]})
    for row in db.query(RangeCredential).filter(RangeCredential.range_id == rng.id):
        row.applied = True
    db.commit()
    return rng


def _passwords(db, rng) -> dict[tuple[str, str], str]:
    db.expire_all()
    return {(r.machine, r.username): r.password
            for r in db.query(RangeCredential).filter(RangeCredential.range_id == rng.id)}


def _row(db, rng, machine, username) -> RangeCredential:
    db.expire_all()
    return db.query(RangeCredential).filter_by(range_id=rng.id, machine=machine, username=username).one()


# ── Rebuild ─────────────────────────────────────────────────────────────
class TestRebuild:
    def test_a_rebuild_rotates_student_passwords_and_keeps_staff_ones(self, client, db_session, world):
        rng = _range(client, db_session, world)
        first = _passwords(db_session, rng)
        act_as(world["instructor"])
        assert client.post(f"/ranges/{rng.id}/provision").status_code == 202
        assert _passwords(db_session, rng) == first  # the first build rotates nothing
        rng = db_session.get(Range, rng.id)
        rng.state, rng.provisioner_output = RangeState.failed, None  # a build that failed: built again
        db_session.query(RangeOperation).filter_by(range_id=rng.id).update({"status": "succeeded"})
        db_session.commit()
        resp = client.post(f"/ranges/{rng.id}/provision")
        assert resp.status_code == 202, resp.text
        second = _passwords(db_session, rng)
        for key in ALL_ACCOUNTS:
            assert (second[key] != first[key]) == (key in STUDENT_ROWS), key
        assert second[("dc01", "jsmith")] == DC_PW  # a scenario account: never rotated
        assert _row(db_session, rng, "web01", "analyst").rotated_at is not None
        assert {r.applied for r in db_session.query(RangeCredential).filter_by(range_id=rng.id)} == {False}


# ── Snapshots and restore ───────────────────────────────────────────────
class TestRestore:
    def test_a_snapshot_records_the_passwords_and_a_restore_rotates_student_ones(self, client, db_session, world,
                                                                                 monkeypatch):
        from app.routers import ranges as ranges_router

        sent = []
        monkeypatch.setattr(ranges_router, "_dispatch_task", lambda name, *a: sent.append((name, *a)) or "t")
        rng = _range(client, db_session, world, RangeState.ready)
        before = _passwords(db_session, rng)
        act_as(world["instructor"])
        snap = client.post(f"/ranges/{rng.id}/snapshots", json={"name": "base"})
        assert snap.status_code == 202, snap.text
        by_id = {r.id: r for r in db_session.query(RangeCredential)}
        captured = {str(c.credential_id): c.password_for(by_id[c.credential_id])
                    for c in db_session.query(RangeCredentialSnapshot)}
        assert sorted(captured.values()) == sorted(before.values())
        db_session.get(RangeSnapshot, uuid.UUID(snap.json()["id"])).snapshot_state = "ready"
        db_session.commit()
        resp = client.post(f"/ranges/{rng.id}/snapshots/{snap.json()['id']}/restore")
        assert resp.status_code == 202, resp.text
        assert sent[-1][0] == "restore_snapshot"
        after = _passwords(db_session, rng)
        for key in ALL_ACCOUNTS:
            assert (after[key] != before[key]) == (key in STUDENT_ROWS), key
        analyst = _row(db_session, rng, "web01", "analyst")
        assert analyst.applied is False and analyst.previous_password == before[("web01", "analyst")]
        assert _row(db_session, rng, "web01", "ubuntu").applied is True  # staff: untouched

    def test_a_restore_that_was_not_sent_puts_the_passwords_back(self, client, db_session, world, monkeypatch):
        from app.routers import ranges as ranges_router

        rng = _range(client, db_session, world, RangeState.ready)
        snap = RangeSnapshot(range_id=rng.id, name="s", tenant_id=rng.tenant_id, snapshot_state="ready",
                             range_state_at_snapshot="ready")
        db_session.add(snap)
        db_session.commit()
        before = _passwords(db_session, rng)
        monkeypatch.setattr(ranges_router, "_dispatch_task", lambda *a: None)  # the broker is down
        act_as(world["instructor"])
        assert client.post(f"/ranges/{rng.id}/snapshots/{snap.id}/restore").status_code == 503
        assert _passwords(db_session, rng) == before
        assert _row(db_session, rng, "web01", "analyst").applied is True


# ── Admin rotation ──────────────────────────────────────────────────────
class TestAdminRotate:
    def _rotate(self, client, rng, row, reason="suspected exposure"):
        return client.post(f"/ranges/{rng.id}/credentials/{row.id}/rotate", json={"reason": reason})

    @pytest.mark.parametrize("who", ["instructor", "range_ops"])
    def test_only_an_admin_rotates(self, client, db_session, world, who):
        rng = _range(client, db_session, world, RangeState.ready)
        act_as(world[who])
        assert self._rotate(client, rng, _row(db_session, rng, "dc01", "Administrator")).status_code == 403

    def test_another_tenants_admin_gets_404(self, client, db_session, world):
        rng = _range(client, db_session, world, RangeState.ready)
        act_as(world["foreign_admin"])
        assert self._rotate(client, rng, _row(db_session, rng, "dc01", "Administrator")).status_code == 404

    def test_a_running_range_rotates_in_its_guests_audited_without_the_password(self, client, db_session, world,
                                                                               monkeypatch):
        from app import celery_client

        sent = []
        monkeypatch.setattr(celery_client, "dispatch", lambda name, *a: sent.append((name, *a)) or "task-1")
        rng = _range(client, db_session, world, RangeState.ready)
        row = _row(db_session, rng, "web01", "ubuntu")  # a staff account: changes only this way
        old = row.password
        act_as(world["admin"])
        resp = self._rotate(client, rng, row)
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["applies"] == "in_guest" and body["dispatched"] is True and "password" not in body["account"]
        assert sent == [("rotate_range_credentials", str(rng.id), [str(row.id)], body["operation_id"])]
        row = _row(db_session, rng, "web01", "ubuntu")
        assert row.password != old and row.previous_password == old and row.applied is False
        audit = db_session.query(AuditLog).filter(AuditLog.action == "range_credential_rotate").one()
        detail = json.loads(audit.detail)
        assert detail["reason"] == "suspected exposure" and detail["operation_id"] == body["operation_id"]
        assert (detail["machine"], detail["username"]) == ("web01", "ubuntu")
        assert old not in audit.detail and row.password not in audit.detail

    def test_an_unbuilt_range_takes_it_at_its_next_build(self, client, db_session, world, monkeypatch):
        from app import celery_client

        monkeypatch.setattr(celery_client, "dispatch", lambda *a: pytest.fail("nothing to send"))
        rng = _range(client, db_session, world)  # created
        act_as(world["admin"])
        resp = self._rotate(client, rng, _row(db_session, rng, "dc01", "Administrator"))
        assert resp.status_code == 202 and resp.json()["applies"] == "next_build"
        assert _row(db_session, rng, "dc01", "Administrator").previous_password is None

    def test_a_scenario_account_and_an_unreachable_range_are_refused(self, client, db_session, world):
        rng = _range(client, db_session, world, RangeState.ready)
        act_as(world["admin"])
        resp = self._rotate(client, rng, _row(db_session, rng, "dc01", "jsmith"))
        assert resp.status_code == 409 and "authored by design" in resp.text
        rng.state = RangeState.stopped
        db_session.commit()
        assert self._rotate(client, rng, _row(db_session, rng, "dc01", "Administrator")).status_code == 409
        assert self._rotate(client, rng, _row(db_session, rng, "dc01", "Administrator"), "x").status_code == 422


# ── Adversarial review of #250 ───────────────────────────────────────────
MIXED_YAML = """
name: mixed
nodes:
  - {id: web01, os: ubuntu-2404}
  - {id: so01, os: securityonion}
  - {id: fw01, os: pfsense, role: firewall}
accounts:
  - {machine: fw01, username: admin, generate: true, role: admin, student_visible: false}
"""


def _rotate(client, rng, row, reason="suspected exposure"):
    return client.post(f"/ranges/{rng.id}/credentials/{row.id}/rotate", json={"reason": reason})


def _settle(db, row_id, *, applied=True, revert_to_sealed=None):
    """What the worker does once a change is verified (applied) or failed (put back: the
    previous password, or after a revert the snapshot's seal)."""
    db.expire_all()
    row = db.get(RangeCredential, row_id)
    if not applied:
        row.sealed_password = revert_to_sealed or row.sealed_previous
    row.sealed_previous, row.pending_operation, row.applied = None, None, True
    db.commit()


def _snapshot(client, db, rng) -> RangeSnapshot:
    resp = client.post(f"/ranges/{rng.id}/snapshots", json={"name": "base"})
    assert resp.status_code == 202, resp.text
    snap = db.get(RangeSnapshot, uuid.UUID(resp.json()["id"]))
    snap.snapshot_state = "ready"
    db.commit()
    return snap


@pytest.fixture
def no_tasks(monkeypatch):
    from app import celery_client
    from app.routers import ranges as ranges_router

    sent = []
    monkeypatch.setattr(ranges_router, "_dispatch_task", lambda name, *a: sent.append((name, *a)) or "t")
    monkeypatch.setattr(celery_client, "dispatch", lambda name, *a: sent.append((name, *a)) or "t")
    return sent


class TestReviewFindings:
    def test_1_a_restore_reapplies_an_admin_rotation_made_after_the_snapshot(self, client, db_session, world,
                                                                             no_tasks):
        """Snapshot P0, admin rotates to P1 (applied), restore: the row is pending under the
        restore's operation; until the revert runs the guests still have P1 (reveal P1), once it
        has they have P0 (reveal P0), and the restore task sets P1 again. One audit row says
        what is re-applied."""
        rng = _range(client, db_session, world, RangeState.ready)
        p0 = _row(db_session, rng, "web01", "ubuntu").password
        act_as(world["admin"])
        snap = _snapshot(client, db_session, rng)
        assert _rotate(client, rng, _row(db_session, rng, "web01", "ubuntu")).status_code == 202
        row = _row(db_session, rng, "web01", "ubuntu")
        p1 = row.password
        _settle(db_session, row.id)  # the worker verified P1
        assert client.post(f"/ranges/{rng.id}/snapshots/{snap.id}/restore").status_code == 202
        row = _row(db_session, rng, "web01", "ubuntu")
        assert (row.password, row.previous_password, row.applied) == (p1, p1, False)
        assert row.pending_operation == f"restore:{snap.id}"
        assert _reveal_pw(client, rng, row) == p1  # the revert has not run: the guests still have P1
        db_session.get(RangeSnapshot, snap.id).snapshot_state = "ready"  # the revert ran
        db_session.commit()
        assert _reveal_pw(client, rng, _row(db_session, rng, "web01", "ubuntu")) == p0
        audit = [json.loads(a.detail) for a in db_session.query(AuditLog)
                 .filter(AuditLog.action == "range_credential_rotate") if "reapplied" in (a.detail or "")]
        assert audit and "web01/ubuntu" in audit[-1]["reapplied"] and audit[-1]["reason"] == "snapshot restore"
        assert p0 not in json.dumps(audit) and p1 not in json.dumps(audit)
        _settle(db_session, row.id)  # the restore task set P1 again
        assert _reveal_pw(client, rng, _row(db_session, rng, "web01", "ubuntu")) == p1

    def test_1_a_snapshot_records_what_the_guest_has_while_a_change_is_pending(self, client, db_session, world,
                                                                              no_tasks):
        rng = _range(client, db_session, world, RangeState.ready)
        p0 = _row(db_session, rng, "web01", "ubuntu").password
        act_as(world["admin"])
        assert _rotate(client, rng, _row(db_session, rng, "web01", "ubuntu")).status_code == 202
        snap = _snapshot(client, db_session, rng)
        row = _row(db_session, rng, "web01", "ubuntu")
        captured = db_session.query(RangeCredentialSnapshot).filter_by(snapshot_id=snap.id, credential_id=row.id).one()
        assert captured.password_for(row) == p0 != row.password

    def test_2_rotations_the_worker_cannot_carry_out_are_refused_up_front(self, client, db_session, world, no_tasks):
        tmpl = Template(id=uuid.uuid4(), name="mixed", version="1", yaml=MIXED_YAML, tenant_id=world["a"].id)
        db_session.add(tmpl)
        db_session.flush()
        act_as(world["instructor"])
        rng = db_session.get(Range, uuid.UUID(client.post("/ranges", json={"name": "M", "template_id": str(
            tmpl.id)}).json()["id"]))
        rng.state, rng.provisioner_backend = RangeState.ready, "vsphere_api"
        db_session.commit()
        act_as(world["admin"])
        for machine, username, why in (("so01", "soadmin", "Security Onion"), ("fw01", "admin", "appliance")):
            resp = _rotate(client, rng, _row(db_session, rng, machine, username))
            assert resp.status_code == 409 and why in resp.text, resp.text
        rng.provisioner_backend = "proxmox_api"
        db_session.commit()
        resp = _rotate(client, rng, _row(db_session, rng, "web01", "ubuntu"))
        assert resp.status_code == 409 and "cannot change passwords inside a guest" in resp.text
        assert no_tasks == [] and _row(db_session, rng, "web01", "ubuntu").previous_password is None

    def test_2_a_failed_change_puts_the_working_password_back(self, client, db_session, world, no_tasks):
        rng = _range(client, db_session, world, RangeState.ready)
        p0 = _row(db_session, rng, "web01", "ubuntu").password
        act_as(world["admin"])
        assert _rotate(client, rng, _row(db_session, rng, "web01", "ubuntu")).status_code == 202
        _settle(db_session, _row(db_session, rng, "web01", "ubuntu").id, applied=False)  # the worker failed
        row = _row(db_session, rng, "web01", "ubuntu")
        assert (row.password, row.previous_password, row.applied) == (p0, None, True)
        assert _reveal_pw(client, rng, row) == p0

    def test_4_rotations_of_one_account_are_serialised(self, client, db_session, world, no_tasks):
        """A double click: the second rotation waits for the first to settle (409)."""
        rng = _range(client, db_session, world, RangeState.ready)
        act_as(world["admin"])
        row = _row(db_session, rng, "web01", "ubuntu")
        assert _rotate(client, rng, row).status_code == 202
        second = _rotate(client, rng, row)
        assert second.status_code == 409 and "still being applied" in second.text
        assert len([s for s in no_tasks if s[0] == "rotate_range_credentials"]) == 1
        _settle(db_session, row.id)
        assert _rotate(client, rng, row).status_code == 202  # settled: rotatable again

    def test_the_reset_race_restore_is_refused_while_an_admin_rotation_is_pending(self, client, db_session,
                                                                                    world, no_tasks):
        """Re-review of #250: admin rotation P1 queued; a restore (a reset) would have tagged it
        and its task would have reverted to the wrong value. Now: the restore is 409, the admin
        task settles its own change (here: failed, so P0 back), and then the restore runs and
        tags only its own changes."""
        rng = _range(client, db_session, world, RangeState.ready)
        p0 = _row(db_session, rng, "web01", "analyst").password
        act_as(world["admin"])
        snap = _snapshot(client, db_session, rng)
        rotated = _rotate(client, rng, _row(db_session, rng, "web01", "analyst"))
        admin_op = rotated.json()["operation_id"]
        assert _row(db_session, rng, "web01", "analyst").pending_operation == admin_op
        refused = client.post(f"/ranges/{rng.id}/snapshots/{snap.id}/restore")
        assert refused.status_code == 409 and "still being applied" in refused.text
        assert db_session.get(RangeSnapshot, snap.id).snapshot_state == "ready"  # nothing changed
        _settle(db_session, _row(db_session, rng, "web01", "analyst").id, applied=False)  # the admin task: failed
        row = _row(db_session, rng, "web01", "analyst")
        assert (row.password, row.pending, row.applied) == (p0, False, True)
        assert _reveal_pw(client, rng, row) == p0  # consistent: what the guests have
        assert client.post(f"/ranges/{rng.id}/snapshots/{snap.id}/restore").status_code == 202
        row = _row(db_session, rng, "web01", "analyst")
        assert row.pending_operation == f"restore:{snap.id}" and row.password != p0

    def test_2_an_admin_force_settles_a_stuck_change_to_the_in_guest_password(self, client, db_session, world,
                                                                               no_tasks):
        rng = _range(client, db_session, world, RangeState.ready)
        p0 = _row(db_session, rng, "web01", "ubuntu").password
        row_id = _row(db_session, rng, "web01", "ubuntu").id
        act_as(world["admin"])
        nothing = client.post(f"/ranges/{rng.id}/credentials/{row_id}/settle", json={"reason": "check"})
        assert nothing.status_code == 409
        assert _rotate(client, rng, _row(db_session, rng, "web01", "ubuntu")).status_code == 202
        act_as(world["range_ops"])
        assert client.post(f"/ranges/{rng.id}/credentials/{row_id}/settle",
                           json={"reason": "task died"}).status_code == 403
        act_as(world["admin"])
        settled = client.post(f"/ranges/{rng.id}/credentials/{row_id}/settle", json={"reason": "task died"})
        assert settled.status_code == 200 and settled.json()["pending_change"] is False
        row = _row(db_session, rng, "web01", "ubuntu")
        assert (row.password, row.pending, row.applied, row.pending_operation) == (p0, False, True, None)
        audit = db_session.query(AuditLog).filter(AuditLog.action == "range_credential_settle").one()
        assert json.loads(audit.detail)["reason"] == "task died" and p0 not in audit.detail

    def test_6_deleting_a_snapshot_deletes_the_passwords_it_recorded(self, client, db_session, world, no_tasks):
        rng = _range(client, db_session, world, RangeState.ready)
        act_as(world["admin"])
        snap = _snapshot(client, db_session, rng)
        assert db_session.query(RangeCredentialSnapshot).filter_by(snapshot_id=snap.id).count() == len(ALL_ACCOUNTS)
        assert client.delete(f"/ranges/{rng.id}/snapshots/{snap.id}").status_code == 204
        assert db_session.query(RangeCredentialSnapshot).filter_by(snapshot_id=snap.id).count() == 0

    def test_7_templates_adr_0013_no_longer_accepts_are_reported_before_a_booking_fails(self, client, db_session,
                                                                                        world, no_tasks):
        from datetime import UTC, datetime, timedelta

        from app.scheduler.models import EventState, ScheduledEvent

        legacy = ("nodes: [{id: ws01, os: ubuntu-2404}]\naccounts:\n"
                  "  - {machine: ws01, username: jdoe, password: Typed-In-1!, student_visible: false}\n")
        tmpl = Template(id=uuid.uuid4(), name="legacy-lab", version="3", yaml=legacy, tenant_id=world["a"].id)
        db_session.add(tmpl)  # stored before the rule existed: never validated against it
        rng = Range(name="Booked", template_id=tmpl.id, tenant_id=world["a"].id, state=RangeState.created)
        db_session.add(rng)
        db_session.flush()
        now = datetime.now(UTC)
        db_session.add(ScheduledEvent(name="class", tenant_id=world["a"].id, range_id=rng.id,
                                      state=EventState.scheduled, start_time=now + timedelta(days=1),
                                      end_time=now + timedelta(days=1, hours=2)))
        db_session.commit()
        act_as(world["admin"])
        issues = client.get("/range-credentials/template-issues")
        assert issues.status_code == 200, issues.text
        (issue,) = [i for i in issues.json() if i["template_id"] == str(tmpl.id)]
        assert issue["upcoming_bookings"] == 1 and "scenario" in issue["problems"][0]
        # Another tenant's booking of the same (global) template is never counted for this one.
        tmpl.tenant_id, tmpl.is_public = None, True
        other = Range(name="Theirs", template_id=tmpl.id, tenant_id=world["b"].id, state=RangeState.created)
        db_session.add(other)
        db_session.flush()
        db_session.add(ScheduledEvent(name="theirs", tenant_id=world["b"].id, range_id=other.id,
                                      state=EventState.scheduled, start_time=now + timedelta(days=1),
                                      end_time=now + timedelta(days=1, hours=2)))
        db_session.commit()
        (issue,) = [i for i in client.get("/range-credentials/template-issues").json()
                    if i["template_id"] == str(tmpl.id)]
        assert issue["upcoming_bookings"] == 1
        act_as(world["foreign_admin"])
        (theirs,) = [i for i in client.get("/range-credentials/template-issues").json()
                     if i["template_id"] == str(tmpl.id)]
        assert theirs["upcoming_bookings"] == 1
        act_as(world["admin"])
        tmpl.tenant_id = world["a"].id
        db_session.commit()
        assert "Typed-In-1!" not in issues.text
        act_as(world["instructor"])  # template:create, not template:update
        assert client.get("/range-credentials/template-issues").status_code == 403
        resp = client.post(f"/ranges/{rng.id}/provision")
        assert resp.status_code == 422
        assert "legacy-lab" in resp.text and "scenario: true" in resp.text and "template-issues" in resp.text


def _reveal_pw(client, rng, row) -> str:
    resp = client.post(f"/ranges/{rng.id}/credentials/{row.id}/reveal")
    assert resp.status_code == 200, resp.text
    return resp.json()["password"]


# ── Lab sessions ────────────────────────────────────────────────────────
class TestLabCredentials:
    def _ready_lab(self, lab, db_session):
        client, rid, _ = lab
        owner = student(db_session)
        session, _ = launch(db_session, owner, rid)
        until(db_session, session, "ready")
        return client, owner, session

    def test_a_lab_gets_its_own_generated_logins_for_its_student(self, lab, db_session, worker):
        client, owner, session = self._ready_lab(lab, db_session)
        rows = db_session.query(RangeCredential).filter_by(range_id=session.range_id).all()
        assert rows and all(r.generated and r.student_visible and r.source == "implicit" for r in rows)
        act_as(owner)
        listed = client.get(f"/lab-sessions/{session.id}/credentials")
        assert listed.status_code == 200 and listed.json()["accounts"] == []  # not set by a build yet
        for r in rows:
            r.applied = True
        db_session.commit()
        listed = client.get(f"/lab-sessions/{session.id}/credentials").json()["accounts"]
        assert {a["username"] for a in listed} == {r.username for r in rows}
        got = client.post(f"/lab-sessions/{session.id}/credentials/{listed[0]['id']}/reveal")
        assert got.status_code == 200 and got.headers["cache-control"] == "no-store"
        assert got.json()["password"] == next(r.password for r in rows if str(r.id) == listed[0]["id"])
        audit = db_session.query(AuditLog).filter(AuditLog.action == "range_credential_reveal").one()
        assert str(audit.user_id) == str(owner.id)
        assert json.loads(audit.detail)["lab_session_id"] == str(session.id)
        assert json.loads(audit.detail)["via"] == "lab_session"

    def test_only_the_owner_sees_them(self, lab, db_session, worker):
        client, owner, session = self._ready_lab(lab, db_session)
        rows = db_session.query(RangeCredential).filter_by(range_id=session.range_id).all()
        for r in rows:
            r.applied = True
        db_session.commit()
        other = student(db_session)
        act_as(other)
        assert client.get(f"/lab-sessions/{session.id}/credentials").status_code == 404
        assert client.post(f"/lab-sessions/{session.id}/credentials/{rows[0].id}/reveal").status_code == 404
        assert client.get(f"/ranges/{session.range_id}/credentials").status_code == 404
        act_as(owner)  # the owner also reaches them on the range itself (takes part through the lab)
        assert client.get(f"/ranges/{session.range_id}/credentials").json()["scope"] == "student"

    def test_the_lab_token_reveals_as_the_owner(self, lab, db_session, worker):
        from app.lab_sessions import tokens

        client, owner, session = self._ready_lab(lab, db_session)
        row = db_session.query(RangeCredential).filter_by(range_id=session.range_id).first()
        row.applied = True
        db_session.commit()
        token = tokens.mint(db_session, session.id, owner.id, None)
        headers = {"X-Lab-Token": token}
        assert client.post(f"/lab-access/{session.id}/credentials/{row.id}/reveal").status_code == 401
        got = client.post(f"/lab-access/{session.id}/credentials/{row.id}/reveal", headers=headers)
        assert got.status_code == 200 and got.json()["password"] == row.password
        audit = db_session.query(AuditLog).filter(AuditLog.action == "range_credential_reveal").one()
        assert str(audit.user_id) == str(owner.id) and json.loads(audit.detail)["via"] == "lab_token"

    def test_a_lab_reset_rotates_the_students_passwords(self, lab, db_session, worker):
        client, owner, session = self._ready_lab(lab, db_session)
        for r in db_session.query(RangeCredential).filter_by(range_id=session.range_id):
            r.applied = True  # as the vSphere build reports them set
        db_session.get(Range, session.range_id).provisioner_backend = "vsphere_api"
        db_session.commit()
        before = {r.id: r.password for r in db_session.query(RangeCredential).filter_by(range_id=session.range_id)}
        captured = db_session.query(RangeCredentialSnapshot).filter_by(snapshot_id=session.baseline_snapshot_id)
        rows = {r.id: r for r in db_session.query(RangeCredential).filter_by(range_id=session.range_id)}
        assert {c.credential_id: c.password_for(rows[c.credential_id]) for c in captured} == before
        act(db_session, lab_service.reset, session)
        assert ("restore_snapshot", str(session.range_id), str(session.baseline_snapshot_id)) in worker.calls
        db_session.expire_all()
        after = {r.id: r for r in db_session.query(RangeCredential).filter_by(range_id=session.range_id)}
        for rid, old in before.items():
            assert after[rid].password != old and after[rid].previous_password == old and not after[rid].applied

    def test_a_lab_reset_is_refused_while_a_password_change_is_pending(self, lab, db_session, worker):
        """Re-review of #250: never a reset over a pending change (an admin rotation queued, or
        the previous reset's own still being applied); the lab stays as it is."""
        client, owner, session = self._ready_lab(lab, db_session)
        row = db_session.query(RangeCredential).filter_by(range_id=session.range_id).first()
        row.applied = True
        db_session.get(Range, session.range_id).provisioner_backend = "vsphere_api"
        row.previous_password, row.pending_operation = row.password, "op-admin"
        db_session.commit()
        with pytest.raises(lab_service.LabRefusedError) as refused:
            act(db_session, lab_service.reset, session)
        assert refused.value.status == 409
        db_session.expire_all()
        assert db_session.get(RangeSnapshot, session.baseline_snapshot_id).snapshot_state == "ready"
        assert not [c for c in worker.calls if c[0] == "restore_snapshot"]
