"""Range certification S5 (ADR 0013 §5, issue #243), API side.

* GET /ranges/{id}/inventory-checks: staff see the last teardown verification and drift;
  Students do not; another tenant's range is not found;
* POST /ranges/{id}/teardown/confirm-unidentified: an administrator confirms unidentified
  items only by typing each listed moid back exactly; anything else (a different case, a
  stray space, a moid that was not listed, a disk file) confirms nothing; a range that is
  not torn down, or is busy, refuses; every confirmation is audited with its reason and
  written to the certification record as an override, and only the accepted moids are
  sent to the worker;
* the certification record's teardown section carries the worker's verification:
  pass on zero remaining, fail with what remains, incomplete with what is unidentified;
  a re-verification after confirmed deletes is appended on read.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _shared import act_as, real_tenant, real_user
from app.models import AuditLog, Range, RangeState, Template, UserRole
from app.range_certification import RangeCertificationEntry
from app.range_inventory import RangeInventoryCheck

TEMPLATE_YAML = "name: s5\nnodes: []\n"
OUTPUT = {"provider": "vsphere_api", "vms": [{"name": "r-web", "vm_id": "vm-1", "identity": {"instance_uuid": "u1"}}],
          "networks": []}
STRAY = {"kind": "vm", "moid": "vm-42", "name": "stray-web", "instance_uuid": "4211-stray", "marks": ["annotation"]}
TAGGED = {"kind": "vm", "moid": "vm-43", "name": "tagged", "instance_uuid": "4211-tagged", "marks": ["tag"]}
DISK = {"kind": "disk", "moid": "[ds1] r-web/r-web.vmdk", "name": "r-web"}


@pytest.fixture
def world(db_session):
    a, b = real_tenant(db_session, "s5-a"), real_tenant(db_session, "s5-b")
    w = {"a": a, "b": b}
    for name, role, tenant in (("admin", UserRole.admin, a), ("instructor", UserRole.instructor, a),
                               ("range_ops", UserRole.range_ops, a), ("student", UserRole.student, a),
                               ("foreign_admin", UserRole.admin, b)):
        w[name] = real_user(db_session, role, tenant.id)
    return w


def _built(client, db, world) -> Range:
    tmpl = Template(id=uuid.uuid4(), name="s5", version="1", yaml=TEMPLATE_YAML, tenant_id=world["a"].id)
    db.add(tmpl)
    db.flush()
    act_as(world["instructor"])
    resp = client.post("/ranges", json={"name": "S5 Range", "template_id": str(tmpl.id)})
    assert resp.status_code == 201, resp.text
    rng = db.get(Range, uuid.UUID(resp.json()["id"]))
    assert client.post(f"/ranges/{rng.id}/provision").status_code == 202
    _worker(db, rng, "ready", OUTPUT)
    return rng


def _worker(db, rng, state: str, output: dict | None = None, error: str | None = None) -> None:
    rng = db.get(Range, rng.id)
    rng.state, rng.error_message = RangeState(state), error
    if output is not None:
        rng.provisioner_output = json.dumps(output)
    db.commit()


def _check(db, rng, kind: str, status: str, summary: str, report: dict, at: datetime | None = None) -> None:
    """What the worker writes (worker/range_inventory_db.save_check)."""
    row = db.query(RangeInventoryCheck).filter_by(range_id=rng.id, kind=kind).first()
    at = at or datetime.now(UTC)
    if row is None:
        db.add(RangeInventoryCheck(range_id=rng.id, tenant_id=rng.tenant_id, kind=kind, status=status,
                                   summary=summary, report=report, checked_at=at, changed_at=at))
    else:
        row.status, row.summary, row.report, row.checked_at, row.changed_at = status, summary, report, at, at
    db.commit()


def _destroyed_with(client, db, world, report: dict, status: str = "incomplete") -> Range:
    rng = _built(client, db, world)
    assert client.post(f"/ranges/{rng.id}/destroy").status_code == 202
    _check(db, rng, "teardown", status, f"zero recorded ids remain; {len(report.get('unidentified', []))} unidentified",
           report)
    _worker(db, rng, "destroyed")
    return rng


def _teardown_entries(db, rng) -> list[RangeCertificationEntry]:
    return (db.query(RangeCertificationEntry).filter_by(section="teardown", tenant_id=rng.tenant_id)
            .order_by(RangeCertificationEntry.seq).all())


def _confirm(client, rng, items, reason="stray clone left by a test, checked on vCenter"):
    return client.post(f"/ranges/{rng.id}/teardown/confirm-unidentified", json={"items": items, "reason": reason})


REPORT = {"verified_at": "2026-10-22T00:00:00+00:00", "deleted": [{"kind": "vm", "id": "vm-1", "name": "r-web"}],
          "remaining": [], "unidentified": [STRAY, TAGGED, DISK], "not_checked": []}


class TestRead:
    def test_staff_see_the_teardown_and_drift_students_do_not(self, client, db_session, world):
        rng = _destroyed_with(client, db_session, world, REPORT)
        _check(db_session, rng, "drift", "incomplete", "1 change(s) from the record: memory_changed",
               {"findings": [{"kind": "memory_changed", "object": "r-web", "expected": 4096, "actual": 8192}],
                "checked": {"vms": 1, "portgroups": 0}, "gaps": []})
        act_as(world["range_ops"])
        body = client.get(f"/ranges/{rng.id}/inventory-checks").json()
        assert [u["moid"] for u in body["teardown"]["unidentified"]] == ["vm-42", "vm-43", DISK["moid"]]
        assert body["drift"]["findings"][0] == {"kind": "memory_changed", "object": "r-web", "expected": 4096,
                                                "actual": 8192, "name": None, "moid": None}
        assert body["can_confirm"] is False and "range:admin" in body["confirm_blocked"]
        act_as(world["admin"])
        assert client.get(f"/ranges/{rng.id}/inventory-checks").json()["can_confirm"] is True
        act_as(world["student"])
        assert client.get(f"/ranges/{rng.id}/inventory-checks").status_code == 403
        act_as(world["foreign_admin"])
        assert client.get(f"/ranges/{rng.id}/inventory-checks").status_code == 404


class TestConfirm:
    def test_typed_back_listed_moids_are_audited_recorded_and_sent(self, client, db_session, world, no_real_broker):
        rng = _destroyed_with(client, db_session, world, REPORT)
        act_as(world["admin"])
        resp = _confirm(client, rng, [{"moid": "vm-42", "confirm_moid": "vm-42"}])
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert [a["moid"] for a in body["accepted"]] == ["vm-42"] and body["dispatched"] is True
        assert no_real_broker.sent[-1] == ("worker.tasks.delete_confirmed_teardown_items",
                                           [str(rng.id), ["vm-42"], world["admin"].id])
        audit = db_session.query(AuditLog).filter_by(action="confirm_unidentified_teardown_items").one()
        assert "vm-42" in audit.detail and "stray clone" in audit.detail and str(audit.user_id) == world["admin"].id
        [override] = db_session.query(RangeCertificationEntry).filter_by(section="overrides").all()
        assert override.reason.startswith("stray clone") and override.actor_id == uuid.UUID(world["admin"].id)
        assert override.detail["items"][0]["moid"] == "vm-42" and override.detail["kind"] == "override"

    @pytest.mark.parametrize("items", [
        [{"moid": "vm-42", "confirm_moid": "VM-42"}],  # typed in another case
        [{"moid": "vm-42", "confirm_moid": "vm-42 "}],  # a trailing space
        [{"moid": "vm-42", "confirm_moid": "vm-43"}],  # another listed item's id
        [{"moid": "vm-99", "confirm_moid": "vm-99"}],  # never listed (annotation alone is no authority)
        [{"moid": "vm-42", "confirm_moid": "vm-42"}, {"moid": "vm-42", "confirm_moid": "vm-42"}],  # twice
        [{"moid": "vm-42", "confirm_moid": "vm-42"}, {"moid": "vm-99", "confirm_moid": "vm-99"}],  # one bad: none
        [{"moid": DISK["moid"], "confirm_moid": DISK["moid"]}],  # a disk file: never deleted by TrueNorth
    ])
    def test_anything_but_an_exact_listed_moid_confirms_nothing(self, client, db_session, world, no_real_broker,
                                                                items):
        rng = _destroyed_with(client, db_session, world, REPORT)
        act_as(world["admin"])
        sent = len(no_real_broker.sent)
        resp = _confirm(client, rng, items)
        assert resp.status_code == 422, resp.text
        assert len(no_real_broker.sent) == sent
        assert db_session.query(AuditLog).filter_by(action="confirm_unidentified_teardown_items").count() == 0
        assert db_session.query(RangeCertificationEntry).filter_by(section="overrides").count() == 0

    def test_only_an_administrator_confirms(self, client, db_session, world):
        rng = _destroyed_with(client, db_session, world, REPORT)
        for who in ("instructor", "range_ops"):
            act_as(world[who])
            assert _confirm(client, rng, [{"moid": "vm-42", "confirm_moid": "vm-42"}]).status_code == 403
        act_as(world["foreign_admin"])
        assert _confirm(client, rng, [{"moid": "vm-42", "confirm_moid": "vm-42"}]).status_code == 404

    def test_a_reason_is_required(self, client, db_session, world):
        rng = _destroyed_with(client, db_session, world, REPORT)
        act_as(world["admin"])
        assert _confirm(client, rng, [{"moid": "vm-42", "confirm_moid": "vm-42"}], reason="").status_code == 422

    def test_a_range_not_torn_down_or_busy_refuses(self, client, db_session, world):
        rng = _built(client, db_session, world)
        _check(db_session, rng, "teardown", "incomplete", "earlier", REPORT)
        act_as(world["admin"])
        resp = _confirm(client, rng, [{"moid": "vm-42", "confirm_moid": "vm-42"}])
        assert resp.status_code == 409 and "ready" in resp.json()["detail"]
        act_as(world["instructor"])
        assert client.post(f"/ranges/{rng.id}/destroy").status_code == 202  # in flight now
        _worker(db_session, rng, "destroying")
        act_as(world["admin"])
        assert _confirm(client, rng, [{"moid": "vm-42", "confirm_moid": "vm-42"}]).status_code == 409

    def test_nothing_listed_refuses(self, client, db_session, world):
        rng = _destroyed_with(client, db_session, world, {**REPORT, "unidentified": []}, status="pass")
        act_as(world["admin"])
        resp = _confirm(client, rng, [{"moid": "vm-42", "confirm_moid": "vm-42"}])
        assert resp.status_code == 409 and "no unidentified" in resp.json()["detail"]


class TestCertificationTeardown:
    def _teardown(self, client, db_session, world, status, summary, report):
        rng = _built(client, db_session, world)
        assert client.post(f"/ranges/{rng.id}/destroy").status_code == 202
        _check(db_session, rng, "teardown", status, summary, report)
        _worker(db_session, rng, "destroyed")
        act_as(world["instructor"])
        body = client.get(f"/ranges/{rng.id}/certification").json()
        return rng, next(s for s in body["record"]["sections"] if s["name"] == "teardown")

    def test_zero_remaining_records_pass(self, client, db_session, world):
        _, sec = self._teardown(client, db_session, world, "pass", "zero recorded ids remain", {
            "deleted": [{"kind": "vm", "id": "vm-1"}], "remaining": [], "unidentified": [], "not_checked": []})
        assert sec["status"] == "pass" and "zero recorded ids remain" in sec["summary"]
        assert sec["entries"][0]["detail"]["inventory_verified"] is True

    def test_remaining_ids_fail_the_record(self, client, db_session, world):
        rng, sec = self._teardown(client, db_session, world, "fail", "1 recorded item(s) still present: disk x", {
            "remaining": [{"kind": "disk", "id": "[ds1] r-web/r-web.vmdk"}], "unidentified": []})
        assert sec["status"] == "fail"
        assert sec["entries"][0]["detail"]["inventory"]["remaining"] == [{"kind": "disk",
                                                                          "id": "[ds1] r-web/r-web.vmdk"}]
        act_as(world["instructor"])
        assert client.get(f"/ranges/{rng.id}/certification").json()["record"]["verdict"] == "not_safe"

    def test_a_reverification_after_confirmed_deletes_is_appended_on_read(self, client, db_session, world):
        rng, sec = self._teardown(client, db_session, world, "incomplete", "1 unidentified", {
            "remaining": [], "unidentified": [STRAY]})
        assert sec["status"] == "incomplete"
        _check(db_session, rng, "teardown", "pass", "zero recorded ids remain", {
            "remaining": [], "unidentified": [], "confirmed_deleted": [STRAY]},
            at=datetime.now(UTC) + timedelta(seconds=1))
        act_as(world["instructor"])
        client.get(f"/ranges/{rng.id}/certification")
        client.get(f"/ranges/{rng.id}/certification")  # a second read appends nothing more
        assert [e.status for e in _teardown_entries(db_session, rng)] == ["incomplete", "pass"]

    def test_a_backend_that_verifies_nothing_says_so(self, client, db_session, world):
        rng = _built(client, db_session, world)
        assert client.post(f"/ranges/{rng.id}/destroy").status_code == 202
        _worker(db_session, rng, "destroyed")
        body = client.get(f"/ranges/{rng.id}/certification").json()
        sec = next(s for s in body["record"]["sections"] if s["name"] == "teardown")
        assert sec["status"] == "incomplete" and "did not verify" in sec["summary"]
