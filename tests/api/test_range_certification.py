"""Range Certification Record (ADR 0013 §6, issue #244; slice S6 skeleton).

* every provision opens one record (its deployment), with the plan hash and who asked;
* a settled build appends guest readiness (#232), network allocation and template names;
  a destroy appends teardown; a force action appends an override with identity and reason;
* the record is append-only: the ORM refuses changes and deletes, bulk ones too, the
  service has no mutation function, and a correction is a new entry;
* the verdict: any failure -> not_safe; else any required section missing or incomplete
  -> ready_not_certified, with reasons; else certified;
* staff only (admin, instructor, range_ops); Students and observers 403; another tenant 404;
* the JSON export is signed, verifies, and fails verification once changed;
* a booking stores ``require_certified_range`` (enforcement waits for S4).

The database-level guards (triggers) are in test_range_certification_postgres.py.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from _shared import act_as, real_tenant, real_user
from app import secretbox
from app.models import AuditLog, Range, RangeState, Template, UserRole
from app.network_inventory import NetworkReservation
from app.range_certification import (
    RangeCertificationEntry,
    RangeCertificationRecord,
    RecordImmutableError,
    retention,
    service,
    signing,
)
from app.range_certification import sections as sec
from app.range_certification.service import CertificationError
from app.range_ops import RangeOperation
from sqlalchemy import delete, update

TEMPLATE_YAML = """
name: cert-lab
network:
  vlans:
    - {id: 100, name: lan, cidr: 10.80.0.0/24}
nodes:
  - {id: web01, name: Web, os: ubuntu-2404, vlan: lan, ip: 10.80.0.20}
  - {id: fw01, name: Edge, os: pfsense, vlan: lan, ip: 10.80.0.1}
"""


def _vm(name: str, **extra) -> dict:
    return {"name": name, "vm_id": f"vm-{name}", "template": f"{name}-tmpl@host-a", "ip": "10.80.0.20",
            "planned_ip": "10.80.0.20", "guest_ips": ["10.80.0.20"], "tools_ready": True,
            "nics": [{"network": "lan", "ip": "10.80.0.20", "mac": f"00:50:56:00:00:{len(name):02x}"}], **extra}


GOOD_OUTPUT = {"provider": "vsphere_api", "vms": [_vm("web01", ip_verified=True), _vm("fw01", ip_verified=None, pfsense={"wan": "10.30.32.101"})],
               "networks": [{"name": "lan", "vlan_id": 3101, "cidr": "10.80.0.0/24"}]}


@pytest.fixture
def world(db_session):
    a, b = real_tenant(db_session, "cert-a"), real_tenant(db_session, "cert-b")
    w = {"a": a, "b": b}
    for name, role, tenant in (("admin", UserRole.admin, a), ("instructor", UserRole.instructor, a),
                               ("range_ops", UserRole.range_ops, a), ("observer", UserRole.observer, a),
                               ("student", UserRole.student, a), ("foreign_instructor", UserRole.instructor, b)):
        w[name] = real_user(db_session, role, tenant.id)
    return w


def _range(client, db, world) -> Range:
    tmpl = Template(id=uuid.uuid4(), name="cert-lab", version="3.1", yaml=TEMPLATE_YAML, tenant_id=world["a"].id)
    db.add(tmpl)
    db.flush()
    act_as(world["instructor"])
    resp = client.post("/ranges", json={"name": "Cert Range", "template_id": str(tmpl.id)})
    assert resp.status_code == 201, resp.text
    return db.get(Range, uuid.UUID(resp.json()["id"]))


def _provision(client, db, rng) -> str:
    resp = client.post(f"/ranges/{rng.id}/provision")
    assert resp.status_code == 202, resp.text
    return resp.headers["Operation-Id"]


def _worker_reports(db, rng, state: str, output: dict | None = None, error: str | None = None) -> None:
    """What the worker records at the end of a build or destroy."""
    rng = db.get(Range, rng.id)
    rng.state, rng.error_message = RangeState(state), error
    if output is not None:
        rng.provisioner_output = json.dumps(output)
    db.commit()


def _reserve(db, rng, *rows: tuple[str, str, str]) -> None:
    for kind, holder, value in rows:
        db.add(NetworkReservation(tenant_id=rng.tenant_id, range_id=rng.id, domain="vsphere:vc:dvs", kind=kind,
                                  holder=holder, value=value))
    db.commit()


def _built(client, db, world, output: dict = GOOD_OUTPUT) -> tuple[Range, str]:
    rng = _range(client, db, world)
    op_id = _provision(client, db, rng)
    _reserve(db, rng, ("vlan", "lan", "3101"), ("mac", "web01:0", "00:50:56:00:00:05"))
    _worker_reports(db, rng, "ready", output)
    return rng, op_id


def _cert(client, rng, **params):
    return client.get(f"/ranges/{rng.id}/certification", params=params)


def _sections(body: dict) -> dict[str, dict]:
    return {s["name"]: s for s in body["record"]["sections"]}


def _record(db, op_id) -> RangeCertificationRecord:
    return db.query(RangeCertificationRecord).filter_by(deployment_id=uuid.UUID(op_id)).one()


def _staff():
    return SimpleNamespace(id=str(uuid.uuid4()), display_name="Ops Lead", email="lead@example.test")


# ── Records and the evidence wired now ──────────────────────────────────────────
class TestRecord:
    def test_a_provision_opens_its_record_with_the_plan_hash_and_who_asked(self, client, db_session, world):
        rng = _range(client, db_session, world)
        op_id = _provision(client, db_session, rng)
        rec = _record(db_session, op_id)
        assert (rec.range_id, rec.tenant_id, rec.generation) == (rng.id, world["a"].id, 1)
        assert rec.plan_hash == hashlib.sha256(TEMPLATE_YAML.encode()).hexdigest()
        assert (rec.template_name, rec.template_version, rec.opened_by) == ("cert-lab", "3.1", "Test instructor")
        body = _cert(client, rng).json()
        assert body["record"]["deployment_id"] == op_id
        assert body["record"]["verdict"] == "ready_not_certified"
        assert {s["status"] for s in body["record"]["sections"] if s["name"] != "overrides"} == {"missing"}
        assert any("Isolation evidence missing" in r for r in body["record"]["reasons"])

    def test_a_range_never_built_has_no_record(self, client, db_session, world):
        rng = _range(client, db_session, world)
        body = _cert(client, rng).json()
        assert body["record"] is None and body["archived"] == [] and body["retention_months"] == 12

    def test_a_finished_build_appends_readiness_network_and_template_names(self, client, db_session, world):
        rng, op_id = _built(client, db_session, world)
        body = _cert(client, rng).json()
        sec = _sections(body)
        assert sec["guest_readiness"]["status"] == "pass"
        assert "1 of 2 VM(s) verified" in sec["guest_readiness"]["summary"]
        assert sec["network_allocation"]["status"] == "pass", sec["network_allocation"]["summary"]  # S1's checks
        held = sec["network_allocation"]["entries"][0]["detail"]["reservations"]
        assert {(r["kind"], r["value"]) for r in held} == {("vlan", "3101"), ("mac", "00:50:56:00:00:05")}
        assert sec["template_provenance"]["status"] == "incomplete"
        assert {r["template"] for r in sec["template_provenance"]["entries"][0]["detail"]["vms"]} == {
            "web01-tmpl@host-a", "fw01-tmpl@host-a"}
        assert body["record"]["verdict"] == "ready_not_certified"
        reasons = " | ".join(body["record"]["reasons"])
        assert "Template provenance incomplete" in reasons and "Credential state incomplete" in reasons
        assert "Isolation evidence missing" in reasons
        count = db_session.query(RangeCertificationEntry).count()
        _cert(client, rng)
        client.post(f"/ranges/{rng.id}/certification/capture")
        assert db_session.query(RangeCertificationEntry).count() == count, "unchanged evidence is not appended again"

    @pytest.mark.parametrize("vms,expected", [
        ([_vm("web01", ip_verified=None)], "guest network not verified by VMware Tools: web01"),  # DHCP or opted out
        ([_vm("fw01", ip_verified=None, vyos={})], "guest network not verified by VMware Tools: every VM is an appliance"),
        ([_vm("web01", ip_verified=True, ip_check={"status": "running"})], "guest network check still running on web01"),
    ])
    def test_readiness_passes_only_on_verified_guests(self, client, db_session, world, vms, expected):
        rng, _ = _built(client, db_session, world, {**GOOD_OUTPUT, "vms": vms})
        readiness = _sections(_cert(client, rng).json())["guest_readiness"]
        assert (readiness["status"], readiness["summary"]) == ("incomplete", expected)

    def test_a_check_settled_after_the_build_reaches_the_record_on_read(self, client, db_session, world):
        rng, _ = _built(client, db_session, world, {**GOOD_OUTPUT, "vms": [
            _vm("web01", ip_verified=None, ip_check={"status": "running"})]})
        assert _sections(_cert(client, rng).json())["guest_readiness"]["status"] == "incomplete"
        _worker_reports(db_session, rng, "ready", GOOD_OUTPUT)  # worker/setup_reconcile settled it
        readiness = _sections(_cert(client, rng).json())["guest_readiness"]
        assert readiness["status"] == "pass" and readiness["history_count"] == 2

    def test_a_failed_rebuild_is_not_scored_on_the_previous_builds_output(self, client, db_session, world):
        """Reproduced (review #252): a build that fails before writing its output leaves the
        previous build's provisioner_output in place, and it was scored as this build's."""
        rng = _range(client, db_session, world)
        first = _provision(client, db_session, rng)
        earlier = {"provider": "vsphere_api", "vms": [], "networks": [{"name": "lan", "vlan_id": 3101}]}
        _worker_reports(db_session, rng, "failed", earlier, error="1 VM(s) built but not ready")
        _cert(client, rng)
        second = _provision(client, db_session, rng)
        _worker_reports(db_session, rng, "failed", error="clone failed")  # output untouched: build 1's
        body = _cert(client, rng).json()
        assert body["record"]["deployment_id"] == second and body["record"]["verdict"] == "not_safe"
        sec_ = _sections(body)
        readiness = sec_["guest_readiness"]["entries"][0]
        assert (readiness["status"], readiness["summary"]) == ("fail", "the build failed: clone failed")
        assert sec_["network_allocation"]["entries"][0]["detail"]["networks"] == [], "build 1's, not build 2's"
        old = _sections(_cert(client, rng, deployment_id=first).json())["network_allocation"]
        assert old["entries"][0]["detail"]["networks"] == [{"name": "lan", "vlan_id": 3101}]

    def test_any_build_failure_fails_readiness_even_with_every_vm_verified(self, client, db_session, world):
        rng = _range(client, db_session, world)
        _provision(client, db_session, rng)
        _worker_reports(db_session, rng, "failed", GOOD_OUTPUT, error="greyspace block failed")
        readiness = _sections(_cert(client, rng).json())["guest_readiness"]
        assert (readiness["status"], readiness["summary"]) == ("fail", "the build failed: greyspace block failed")

    def test_a_read_writes_nothing_when_nothing_changed(self, client, db_session, world, monkeypatch):
        rng, _ = _built(client, db_session, world)
        _cert(client, rng)  # settles the build
        entries = db_session.query(RangeCertificationEntry).count()
        from app.range_ops import service as ops

        monkeypatch.setattr(ops, "reconcile", lambda *a: pytest.fail("reconcile with nothing in flight"))
        monkeypatch.setattr(service, "append", lambda *a, **k: pytest.fail("append with nothing changed"))
        assert _cert(client, rng).status_code == 200
        assert db_session.query(RangeCertificationEntry).count() == entries

    def test_archived_records_entries_are_read_in_one_query(self, client, db_session, world):
        from sqlalchemy import event

        rng = _range(client, db_session, world)
        for _ in range(3):
            _provision(client, db_session, rng)
            _worker_reports(db_session, rng, "failed", {"provider": "vsphere_api", "vms": []}, error="clone failed")
            _cert(client, rng)
        seen: list[str] = []
        listen = lambda conn, cur, stmt, *a: seen.append(stmt)  # noqa: E731
        event.listen(db_session.get_bind(), "before_cursor_execute", listen)
        try:
            body = _cert(client, rng).json()
        finally:
            event.remove(db_session.get_bind(), "before_cursor_execute", listen)
        assert len(body["archived"]) == 2
        reads = [q for q in seen if q.lstrip().upper().startswith("SELECT") and "FROM range_certification_entries" in q]
        assert len(reads) <= 3, reads  # the current record, the evidence check, the archived batch

    def test_a_vm_whose_guest_network_never_came_up_is_not_safe(self, client, db_session, world):
        bad = {**GOOD_OUTPUT, "vms": [_vm("web01", ip_verified=False, not_ready="planned address not reported")],
               "not_ready": ["web01"]}
        rng = _range(client, db_session, world)
        _provision(client, db_session, rng)
        _worker_reports(db_session, rng, "failed", bad, error="1 VM(s) built but not ready")
        body = _cert(client, rng).json()
        assert body["record"]["verdict"] == "not_safe"
        assert body["record"]["reasons"] == ["Guest readiness failed: the build failed: 1 VM(s) built but not ready"]

    def test_a_destroy_appends_teardown_and_a_failed_one_is_not_safe(self, client, db_session, world):
        rng, op_id = _built(client, db_session, world)
        _cert(client, rng)
        assert client.post(f"/ranges/{rng.id}/destroy").status_code == 202
        _worker_reports(db_session, rng, "failed", error="vCenter refused the delete")
        sec = _sections(_cert(client, rng).json())
        assert sec["teardown"]["status"] == "fail" and "vCenter refused" in sec["teardown"]["summary"]
        assert client.post(f"/ranges/{rng.id}/destroy").status_code == 202
        _worker_reports(db_session, rng, "destroyed")
        body = _cert(client, rng).json()
        teardown = _sections(body)["teardown"]
        assert teardown["status"] == "incomplete" and teardown["history_count"] == 2
        assert [e["status"] for e in teardown["entries"]] == ["incomplete"], "the card gets the latest entry only"
        full = _sections(_cert(client, rng, history=1).json())["teardown"]
        assert [e["status"] for e in full["entries"]] == ["fail", "incomplete"], "history=1: superseded ones too"
        assert body["record"]["deployment_id"] == op_id, "teardown goes into the deployment it tears down"

    def test_each_provision_is_its_own_deployment(self, client, db_session, world):
        rng = _range(client, db_session, world)
        first = _provision(client, db_session, rng)
        _worker_reports(db_session, rng, "failed", {"provider": "vsphere_api", "vms": []}, error="clone failed")
        _cert(client, rng)
        second = _provision(client, db_session, rng)
        _worker_reports(db_session, rng, "ready", GOOD_OUTPUT)
        body = _cert(client, rng).json()
        assert body["record"]["deployment_id"] == second and body["record"]["generation"] == 2
        [arch] = body["archived"]  # the current record is the newest build's; earlier ones are archived
        assert (arch["deployment_id"], arch["generation"], arch["verdict"]) == (first, 1, "not_safe")
        assert arch["purge_after"] is not None
        assert _sections(body)["guest_readiness"]["status"] == "pass"
        old = _cert(client, rng, deployment_id=first).json()["record"]
        assert old["deployment_id"] == first
        assert old["deployment_status"] == "failed"
        assert _sections({"record": old})["guest_readiness"]["summary"] == (
            "the build failed: clone failed")
        assert _cert(client, rng, deployment_id=str(uuid.uuid4())).status_code == 404

    def test_a_forced_action_is_recorded_with_who_and_why(self, client, db_session, world):
        rng = _range(client, db_session, world)
        op_id = _provision(client, db_session, rng)
        act_as(world["admin"])
        assert client.post(f"/ranges/{rng.id}/operations/{op_id}/abandon").status_code == 200
        overrides = _sections(_cert(client, rng).json())["overrides"]
        assert overrides["status"] == "info"
        [entry] = overrides["entries"]
        assert (entry["actor"], entry["detail"]["action"], entry["detail"]["kind"]) == (
            "Test admin", "abandon_operation", "force")
        assert entry["actor_id"] == world["admin"].id
        assert entry["reason"] is None and entry["detail"]["reason_not_requested"] is True, "no reason is made up"

    def test_capture_opens_the_record_of_a_build_accepted_before_records_began(self, client, db_session, world):
        rng = _range(client, db_session, world)
        assert client.post(f"/ranges/{rng.id}/certification/capture").status_code == 409  # never provisioned
        op = RangeOperation(id=uuid.uuid4(), tenant_id=rng.tenant_id, range_id=rng.id, action="provision", generation=1,
                            request_hash="0" * 64, status="succeeded", dispatch_attempts=1)
        db_session.add(op)
        _worker_reports(db_session, rng, "ready", GOOD_OUTPUT)
        act_as(world["range_ops"])
        resp = client.post(f"/ranges/{rng.id}/certification/capture")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"deployment_id": str(op.id), "written": 4}
        late = _record(db_session, str(op.id))
        assert late.opened_by == "Test range_ops"
        assert late.plan_hash_basis == "template_yaml_sha256_at_capture", "says the hash is not build-time"
        assert client.post(f"/ranges/{rng.id}/certification/capture").json()["written"] == 0


# ── Every section the merged slices fill, end to end ──────────────────────────────
CHECKSUMS = {"ubuntu-2404": "a" * 64, "pfsense": "b" * 64}


def _register_builds(db) -> None:
    """S2: a golden image per catalogue id with one promoted build (golden_image_builds)."""
    from app.golden_image_builds import GoldenImageBuild
    from app.models import GoldenImage

    for catalogue, checksum in CHECKSUMS.items():
        img = GoldenImage(id=uuid.uuid4(), catalogue_id=catalogue, hypervisor="vsphere", template_name=catalogue)
        db.add(img)
        db.flush()
        db.add(GoldenImageBuild(golden_image_id=img.id, build_id="20261021-1", template_name=catalogue,
                                checksum=checksum, promoted=True))
    db.flush()


def _pinned(vm: dict, catalogue: str, login: str) -> dict:
    return {**vm, "template_build": {"promoted": True, "catalogue_id": catalogue, "build_id": "20261021-1",
                                     "checksum": CHECKSUMS[catalogue]},
            "template_login": {"status": login, "username": "ubuntu", "detail": "..."}}


CERTIFIABLE = {**GOOD_OUTPUT, "vms": [_pinned(GOOD_OUTPUT["vms"][0], "ubuntu-2404", "rejected"),
                                      _pinned(GOOD_OUTPUT["vms"][1], "pfsense", "not_checked")]}


class TestMergedSlices:
    def _set_accounts(self, db, rng) -> None:
        from app.range_credentials import RangeCredential

        for row in db.query(RangeCredential).filter(RangeCredential.range_id == rng.id):
            row.applied = True
        db.commit()

    def test_a_build_with_every_slices_evidence_is_certified_once_isolation_is_proven(self, client, db_session, world):
        _register_builds(db_session)
        rng, op_id = _built(client, db_session, world, CERTIFIABLE)
        self._set_accounts(db_session, rng)
        body = _cert(client, rng).json()
        sec_ = _sections(body)
        for name in ("guest_readiness", "network_allocation", "template_provenance", "credentials"):
            assert sec_[name]["status"] == "pass", (name, sec_[name]["summary"])
        assert sec_["template_provenance"]["summary"] == (
            "2 VM(s) on registered promoted builds: pfsense 20261021-1, ubuntu-2404 20261021-1")
        assert body["record"]["verdict"] == "ready_not_certified"
        assert body["record"]["reasons"] == ["Isolation evidence missing (provided by S4 (#242))"]
        service.append(db_session, _record(db_session, op_id), "isolation", "pass", "every zone isolated", {},
                       source="test.s4")
        assert _cert(client, rng).json()["record"]["verdict"] == "certified"
        assert service.booking_gate(db_session, rng.tenant_id, rng.id) == (True, [])

    @pytest.mark.parametrize("change,section,expected", [
        (lambda o: o["vms"][0]["template_login"].update(status="accepted"), "credentials",
         "Credential state failed: the golden template's password still logs in on web01"),
        (lambda o: o["vms"][0]["template_build"].update(checksum="f" * 64), "template_provenance",
         "Template provenance failed: pinned build mismatch: web01: checksum differs from registered build "
         "20261021-1"),
    ])
    def test_a_failed_slice_check_makes_the_build_not_safe(self, client, db_session, world, change, section, expected):
        import copy

        _register_builds(db_session)
        output = copy.deepcopy(CERTIFIABLE)
        change(output)
        rng, _ = _built(client, db_session, world, output)
        self._set_accounts(db_session, rng)
        body = _cert(client, rng).json()
        assert body["record"]["verdict"] == "not_safe" and body["record"]["reasons"] == [expected]
        assert _sections(body)[section]["status"] == "fail"

    def test_vlans_used_outside_truenorth_on_the_switch_are_recorded(self, client, db_session, world):
        """S1's external reservations share the (domain, kind, value) unique constraint, so a
        range can never hold an external VLAN; the record lists what was seen outside."""
        db_session.add(NetworkReservation(tenant_id=None, range_id=None, domain="vsphere:vc:dvs", kind="vlan",
                                          value="200", holder="external:vc/dvs:corp-pg"))
        db_session.commit()
        rng, _ = _built(client, db_session, world, CERTIFIABLE)
        net = _sections(_cert(client, rng).json())["network_allocation"]
        check = net["entries"][0]["detail"]["external_collision_check"]
        assert net["status"] == "pass"
        assert check["external_vlans_in_domain"] == ["vsphere:vc:dvs:200"] and check["overlap"] == []
        assert check["last_live_read"] is not None


# ── Append-only ──────────────────────────────────────────────────────────────────
class TestAppendOnly:
    def _one(self, client, db, world) -> tuple[RangeCertificationRecord, RangeCertificationEntry]:
        rng, op_id = _built(client, db, world)
        _cert(client, rng)
        rec = _record(db, op_id)
        return rec, db.query(RangeCertificationEntry).filter_by(record_id=rec.id).first()

    @pytest.mark.parametrize("which", ["entry", "record"])
    @pytest.mark.parametrize("how", ["change", "delete"])
    def test_a_loaded_row_cannot_be_changed_or_deleted(self, client, db_session, world, which, how):
        rec, entry = self._one(client, db_session, world)
        row = entry if which == "entry" else rec
        with pytest.raises(RecordImmutableError), db_session.begin_nested():
            if how == "delete":
                db_session.delete(row)
            elif which == "entry":
                entry.status = "pass"
            else:
                rec.plan_hash = "0" * 64
            db_session.flush()

    @pytest.mark.parametrize("model", [RangeCertificationRecord, RangeCertificationEntry])
    def test_bulk_updates_and_deletes_are_refused(self, client, db_session, world, model):
        self._one(client, db_session, world)
        for attempt in (lambda: db_session.query(model).update({model.tenant_id: uuid.uuid4()}),
                        lambda: db_session.query(model).delete(),
                        lambda: db_session.execute(update(model).values(tenant_id=uuid.uuid4())),
                        lambda: db_session.execute(delete(model))):
            with pytest.raises(RecordImmutableError):
                attempt()

    def test_the_service_has_no_way_to_change_or_remove_evidence(self):
        names = [n for n, f in inspect.getmembers(service, inspect.isfunction) if f.__module__ == service.__name__]
        verbs = ("update", "delete", "remove", "edit", "replace", "purge", "set_", "clear", "drop")
        assert not [n for n in names if n.lstrip("_").startswith(verbs)], names
        source = inspect.getsource(service)
        for call in (".delete(", ".update(", "update(Range", "delete(Range", "session.merge", ".merge("):
            assert call not in source, call

    def test_a_correction_appends_and_keeps_what_it_corrects(self, client, db_session, world):
        rec, _ = self._one(client, db_session, world)
        before = service.entries_of(db_session, rec)
        target = next(e for e in before if e.section == "network_allocation")
        fix = service.append_correction(db_session, rec, target.seq, "incomplete", "VLAN 3101 confirmed by hand",
                                        {"checked": True}, user=_staff(), reason="vDS read failed, checked on site")
        after = service.entries_of(db_session, rec)
        assert len(after) == len(before) + 1 and target in after
        assert (fix.corrects_seq, fix.actor, fix.source) == (target.seq, "Ops Lead", "correction")
        assert sec.latest(after, "network_allocation").summary == "VLAN 3101 confirmed by hand"

    def test_corrections_and_overrides_need_a_person_and_a_reason(self, client, db_session, world):
        rec, entry = self._one(client, db_session, world)
        with pytest.raises(CertificationError):
            service.append_correction(db_session, rec, entry.seq, "pass", "x", {}, user=_staff(), reason="  ")
        with pytest.raises(CertificationError):
            service.append(db_session, rec, "overrides", "info", "force: x", {}, source="t", user=None, reason="why")
        with pytest.raises(CertificationError):
            service.append(db_session, rec, "isolation", "info", "x", {}, source="t")
        with pytest.raises(CertificationError):
            service.append(db_session, rec, "no_such_section", "pass", "x", {}, source="t")


# ── Verdict ──────────────────────────────────────────────────────────────────────
def _e(section: str, status: str, seq: int, summary: str = "") -> SimpleNamespace:
    return SimpleNamespace(section=section, status=status, seq=seq, summary=summary or f"{section} {status}")


REQUIRED = [s.name for s in sec.SECTIONS if s.required]


def _all_pass() -> list[SimpleNamespace]:
    return [_e(name, "pass", i + 1) for i, name in enumerate(REQUIRED)]


class TestVerdict:
    def test_every_required_section_passing_is_certified(self):
        assert sec.verdict(_all_pass()) == ("certified", [])
        assert sec.VERDICT_LABELS["certified"] == "Certified for Students"

    def test_a_missing_section_is_ready_not_certified_and_says_what_is_missing(self):
        verdict, reasons = sec.verdict([e for e in _all_pass() if e.section != "isolation"])
        assert verdict == "ready_not_certified"
        assert reasons == ["Isolation evidence missing (provided by S4 (#242))"]

    def test_incomplete_evidence_is_listed(self):
        entries = [*_all_pass(), _e("template_provenance", "incomplete", 99, "no build ids (S2)")]
        assert sec.verdict(entries) == ("ready_not_certified", ["Template provenance incomplete: no build ids (S2)"])

    @pytest.mark.parametrize("section,summary", [
        ("isolation", "zone dmz reachable outside its exceptions"),
        ("credentials", "the template password still works on web01"),
        ("template_provenance", "pinned build mismatch on dc01"),
        ("drift", "VLAN changed on lan"),  # optional, but a failure still counts
    ])
    def test_any_failed_check_is_not_safe(self, section, summary):
        entries = [e for e in _all_pass() if e.section != section] + [_e(section, "fail", 50, summary)]
        verdict, reasons = sec.verdict(entries)
        assert verdict == "not_safe" and reasons == [f"{sec.BY_NAME[section].title} failed: {summary}"]

    def test_a_failure_wins_over_missing_sections(self):
        assert sec.verdict([_e("isolation", "fail", 1, "reachable")])[0] == "not_safe"

    def test_the_latest_entry_decides_so_a_correction_can_clear_a_failure(self):
        entries = [*_all_pass(), _e("credentials", "fail", 20), _e("credentials", "pass", 21)]
        assert sec.verdict(entries)[0] == "certified"

    def test_optional_sections_and_overrides_do_not_hold_certification_back(self):
        entries = [*_all_pass(), _e("teardown", "incomplete", 30), _e("overrides", "info", 31)]
        assert sec.verdict(entries) == ("certified", [])


# ── Who may read ─────────────────────────────────────────────────────────────────
class TestAccess:
    PATHS = ("/certification", "/certification/export", "/certification/export/pdf")

    @pytest.mark.parametrize("role", ["admin", "instructor", "range_ops"])
    def test_staff_read_the_record_and_its_exports(self, client, db_session, world, role):
        rng, _ = _built(client, db_session, world)
        act_as(world[role])
        for path in self.PATHS:
            resp = client.get(f"/ranges/{rng.id}{path}")
            assert resp.status_code == 200, (path, resp.text)

    @pytest.mark.parametrize("role", ["student", "observer"])
    def test_students_and_observers_are_refused(self, client, db_session, world, role):
        rng, _ = _built(client, db_session, world)
        act_as(world[role])
        for path in self.PATHS:
            assert client.get(f"/ranges/{rng.id}{path}").status_code == 403, path
        assert client.post(f"/ranges/{rng.id}/certification/capture").status_code == 403

    def test_another_tenants_range_is_not_found(self, client, db_session, world):
        rng, op_id = _built(client, db_session, world)
        act_as(world["foreign_instructor"])
        for path in self.PATHS:
            assert client.get(f"/ranges/{rng.id}{path}").status_code == 404, path
        assert client.post(f"/ranges/{rng.id}/certification/capture").status_code == 404
        assert service.record_for_deployment(db_session, world["b"].id, uuid.UUID(op_id)) is None


# ── Signed export ────────────────────────────────────────────────────────────────
class TestExport:
    def _export(self, client, db_session, world) -> dict:
        rng, _ = _built(client, db_session, world)
        act_as(world["range_ops"])
        resp = client.get(f"/ranges/{rng.id}/certification/export")
        assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
        assert "attachment" in resp.headers["content-disposition"]
        return resp.json()

    def test_the_export_verifies(self, client, db_session, world):
        doc = self._export(client, db_session, world)
        assert doc["format"] == "truenorth.range-certification.v1" and doc["exported_by"] == "Test range_ops"
        assert doc["signature"]["alg"] == "Ed25519" and signing.verify(doc)
        body = signing.canonical({k: v for k, v in doc.items() if k != "signature"})
        assert hashlib.sha256(body).hexdigest() == doc["signature"]["signed_sha256"]

    @pytest.mark.parametrize("tamper", [
        lambda d: d["record"].update(verdict="certified"),
        lambda d: d["record"]["sections"][2]["entries"][0].update(status="pass", summary="all good"),
        lambda d: d["record"]["reasons"].pop(),
        lambda d: d.update(exported_by="Someone Else"),
        lambda d: d["signature"].update(value=d["signature"]["value"][:-4] + "AAA="),
    ])
    def test_any_change_fails_verification(self, client, db_session, world, tamper):
        doc = self._export(client, db_session, world)
        tamper(doc)
        assert not signing.verify(doc)

    def test_every_export_of_one_record_state_carries_the_same_record_hash(self, client, db_session, world):
        doc = self._export(client, db_session, world)
        rng_id = doc["record"]["range_id"]
        again = client.get(f"/ranges/{rng_id}/certification/export").json()
        assert again["record_sha256"] == doc["record_sha256"] == hashlib.sha256(
            signing.canonical(doc["record"])).hexdigest()
        assert doc["record"]["tenant_id"] == str(world["a"].id)
        assert client.get(f"/ranges/{rng_id}/certification/export/pdf").status_code == 200
        [pdf_row] = db_session.query(AuditLog).filter(AuditLog.action == "range_certification_export",
                                                      AuditLog.detail.startswith("pdf;")).all()
        assert f"record_sha256={doc['record_sha256']}" in pdf_row.detail, "the PDF names the hash JSON exports carry"
        from app.range_certification import pdf as cert_pdf

        assert doc["record_sha256"].encode() in cert_pdf.render(doc, compress=False)

    def test_a_document_signed_with_another_key_does_not_verify_here(self, client, db_session, world, monkeypatch):
        doc = self._export(client, db_session, world)
        monkeypatch.setenv(secretbox.KEY_ENV, "another-platform-key-0123456789abcdef-xyz")
        forged = signing.sign({k: v for k, v in doc.items() if k != "signature"} | {"exported_by": "Mallory"})
        monkeypatch.undo()
        assert not signing.verify(forged), "a valid signature by a key this platform does not hold"

    def test_rotation_still_verifies_what_the_previous_key_signed(self, client, db_session, world, monkeypatch):
        doc = self._export(client, db_session, world)
        old = secretbox.os.environ[secretbox.KEY_ENV]
        monkeypatch.setenv(secretbox.KEY_ENV, f"a-brand-new-platform-key-0123456789abcdef,{old}")
        assert signing.verify(doc)
        monkeypatch.setenv(secretbox.KEY_ENV, "a-brand-new-platform-key-0123456789abcdef")  # old key retired
        assert not signing.verify(doc), "no configured key signed it"
        assert service.verify_export(db_session, doc), "the recorded public key still verifies it"
        tampered = {**doc, "exported_by": "Mallory"}
        assert not service.verify_export(db_session, tampered)

    def test_the_signing_keys_are_published_retired_ones_too(self, client, db_session, world, monkeypatch):
        doc = self._export(client, db_session, world)
        [key] = client.get("/range-certification/signing-keys").json()
        assert (key["key_id"], key["public_key"], key["current"]) == (
            doc["signature"]["key_id"], doc["signature"]["public_key"], True)
        assert key["first_used_at"] is not None
        pub = signing.public_key_from_b64(key["public_key"])
        assert signing.verify_with_public_key(doc, pub), "an outside verifier needs only the published key"
        monkeypatch.setenv(secretbox.KEY_ENV, "a-brand-new-platform-key-0123456789abcdef")
        keys = client.get("/range-certification/signing-keys").json()
        assert [(k["current"], k["first_used_at"] is None) for k in keys] == [(True, True), (False, False)]
        assert keys[1]["key_id"] == doc["signature"]["key_id"]

    @pytest.mark.parametrize("role,code", [("student", 403), ("observer", 403), ("range_ops", 200)])
    def test_the_key_list_is_staff_only(self, client, world, role, code):
        act_as(world[role])
        assert client.get("/range-certification/signing-keys").status_code == code

    def test_exports_are_audited_and_the_pdf_names_the_signed_hash(self, client, db_session, world):
        rng, _ = _built(client, db_session, world)
        act_as(world["admin"])
        resp = client.get(f"/ranges/{rng.id}/certification/export/pdf")
        assert resp.status_code == 200 and resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")
        rows = db_session.query(AuditLog).filter(AuditLog.action == "range_certification_export").all()
        assert len(rows) == 1 and rows[0].detail.startswith("pdf; deployment=") and "signed_sha256=" in rows[0].detail


# ── Retention (owner decision 2026-10-21) ─────────────────────────────────────────
class TestRetention:
    def _two_builds(self, client, db, world) -> tuple[Range, str, str]:
        rng = _range(client, db, world)
        first = _provision(client, db, rng)
        _worker_reports(db, rng, "failed", {"provider": "vsphere_api", "vms": []}, error="clone failed")
        _cert(client, rng)
        second = _provision(client, db, rng)
        _worker_reports(db, rng, "ready", GOOD_OUTPUT)
        _cert(client, rng)
        return rng, first, second

    def test_archived_records_past_retention_are_purged_with_an_audit_row(self, client, db_session, world):
        rng, first, second = self._two_builds(client, db_session, world)
        old_id = _record(db_session, first).id
        kept = client.get(f"/ranges/{rng.id}/certification/export", params={"deployment_id": first}).json()
        later = datetime.now(UTC) + timedelta(days=400)
        assert retention.purge(db_session, now=datetime.now(UTC), months=12) == 0, "not old enough yet"
        assert retention.purge(db_session, now=later, months=0) == 0, "0 keeps everything forever"
        assert retention.purge(db_session, now=later, months=12) == 1
        assert db_session.query(RangeCertificationRecord).filter_by(id=old_id).count() == 0
        assert db_session.query(RangeCertificationEntry).filter_by(record_id=old_id).count() == 0
        assert _record(db_session, second) is not None, "the current record stays"
        [row] = db_session.query(AuditLog).filter(AuditLog.action == "range_certification.purge").all()
        assert f"deployment={first}" in row.detail and "retention_months=12" in row.detail
        assert row.resource_id == str(rng.id) and row.tenant_id == world["a"].id
        assert f"record_sha256={kept['record_sha256']}" in row.detail, "matches every export made before"

    def test_the_current_record_is_never_purged(self, client, db_session, world):
        rng, op_id = _built(client, db_session, world)
        _cert(client, rng)
        far = datetime.now(UTC) + timedelta(days=3650)
        assert retention.purge(db_session, now=far, months=1) == 0
        assert _record(db_session, op_id) is not None

    def test_an_archived_record_can_be_exported_before_it_is_purged(self, client, db_session, world):
        rng, first, _ = self._two_builds(client, db_session, world)
        doc = client.get(f"/ranges/{rng.id}/certification/export", params={"deployment_id": first}).json()
        assert doc["record"]["deployment_id"] == first and signing.verify(doc)

    def test_a_record_someone_else_purged_writes_no_audit_row(self, client, db_session, world):
        _, first, _ = self._two_builds(client, db_session, world)
        old = _record(db_session, first)
        assert retention._purge_one(db_session, old, 12) is True
        assert retention._purge_one(db_session, old, 12) is False, "the delete removed nothing"
        db_session.flush()
        assert db_session.query(AuditLog).filter(AuditLog.action == "range_certification.purge").count() == 1

    @pytest.mark.parametrize("when,months,expected", [
        (datetime(2026, 3, 31, tzinfo=UTC), -1, datetime(2026, 2, 28, tzinfo=UTC)),
        (datetime(2026, 1, 15, tzinfo=UTC), -12, datetime(2025, 1, 15, tzinfo=UTC)),
        (datetime(2025, 12, 1, tzinfo=UTC), 1, datetime(2026, 1, 1, tzinfo=UTC)),
    ])
    def test_months_are_calendar_months(self, when, months, expected):
        assert retention.shift_months(when, months) == expected

    def test_the_setting_reaches_the_api_from_the_installer_and_compose(self):
        root = Path(__file__).resolve().parents[2]
        assert "tn_range_cert_retention_months: 12" in (root / "install/inventory/group_vars/all/main.yml").read_text()
        assert "RANGE_CERT_RETENTION_MONTHS={{ tn_range_cert_retention_months | default(12) }}" in (
            root / "install/roles/tn_config/templates/env.production.j2").read_text()
        assert "RANGE_CERT_RETENTION_MONTHS: ${RANGE_CERT_RETENTION_MONTHS:-12}" in (
            root / "infra/platform/docker/compose.prod.yml").read_text()


# ── Booking option ───────────────────────────────────────────────────────────────
class TestBooking:
    def _book(self, client, rng, **extra):
        start = datetime.now(UTC) + timedelta(days=3)
        return client.post("/schedule/events", json={
            "name": "Certified class", "start_time": start.isoformat(),
            "end_time": (start + timedelta(hours=2)).isoformat(), "range_id": str(rng.id), "draft": True, **extra})

    def test_require_certified_range_is_stored_and_returned(self, client, db_session, world):
        rng = _range(client, db_session, world)
        resp = self._book(client, rng, require_certified_range=True)
        assert resp.status_code == 201, resp.text
        assert resp.json()["require_certified_range"] is True
        assert self._book(client, rng).json()["require_certified_range"] is False
        evt = resp.json()
        start = datetime.now(UTC) + timedelta(days=4)
        edited = client.put(f"/schedule/events/{evt['id']}", json={
            "name": "Renamed", "start_time": start.isoformat(), "end_time": (start + timedelta(hours=2)).isoformat(),
            "range_id": str(rng.id)})
        assert edited.status_code == 200, edited.text
        assert edited.json()["require_certified_range"] is True, "a form that does not send it keeps it"

    def test_the_gate_s4_will_enforce_refuses_an_uncertified_range(self, client, db_session, world):
        rng, op_id = _built(client, db_session, world)
        ok, reasons = service.booking_gate(db_session, rng.tenant_id, rng.id)
        assert not ok and len(reasons) == 5  # opened at provision; the build is not settled until read
        _cert(client, rng)
        ok, reasons = service.booking_gate(db_session, rng.tenant_id, rng.id)
        assert not ok and any("Isolation evidence missing" in r for r in reasons)
        rec = _record(db_session, op_id)
        for name in ("template_provenance", "network_allocation", "credentials", "isolation"):
            service.append(db_session, rec, name, "pass", f"{name} proven", {}, source="test")
        assert service.booking_gate(db_session, rng.tenant_id, rng.id) == (True, [])
