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
        rng.state = state
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
        captured = {str(c.credential_id): c.password for c in db_session.query(RangeCredentialSnapshot)}
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
        before = {r.id: r.password for r in db_session.query(RangeCredential).filter_by(range_id=session.range_id)}
        captured = db_session.query(RangeCredentialSnapshot).filter_by(snapshot_id=session.baseline_snapshot_id)
        assert {c.credential_id: c.password for c in captured} == before  # the baseline recorded them
        act(db_session, lab_service.reset, session)
        assert ("restore_snapshot", str(session.range_id), str(session.baseline_snapshot_id)) in worker.calls
        db_session.expire_all()
        after = {r.id: r for r in db_session.query(RangeCredential).filter_by(range_id=session.range_id)}
        for rid, old in before.items():
            assert after[rid].password != old and after[rid].previous_password == old and not after[rid].applied
