"""Oracle tests for audit tracking issue #443 (Fable audit, 3-skeptic vote), card
TN10X-AUDIT-tests-provision: one failing test per confirmed provisioning / teardown /
drift finding still present at base 280a838b. The fixes themselves stay with the
supervisor; these tests are what the fixes must make pass. Every test is marked
``audit443`` and names its finding (file:line, title) in its docstring; each of them
FAILS at the base, so it carries ``xfail(strict=True, reason="audit #443: ...")`` and
turns green exactly when its finding is fixed. Companion tests that pass at the base
carry neither marker.

Findings already fixed at this base — no failing test here, by the card's acceptance:
- console sign-in token expiry (fixed in #442, "a console no longer closes when its
  sign-in token expires") — console area, outside provisioning; noted, not tested here.

Each finding below that is still present gets exactly one failing test; the two
``..._still_...`` tests are companions that pass at the base and pin the fix's shape
(staff keep reading the output; an unmarked port group on its own VLAN stays reusable).
"""

from __future__ import annotations

import json
import uuid

import pytest
from _shared import act_as, real_tenant, real_user
from app.models import Range, RangeState, Template, UserRole

tasks = pytest.importorskip("worker.tasks")
from test_vsphere_provision import R8, RANGE_ID, FakeDVS, FakePortgroup  # noqa: E402
from worker import lab_tasks  # noqa: E402
from worker.provisioners import vsphere_infra as infra  # noqa: E402
from worker.provisioners import vsphere_vlans as live  # noqa: E402
from worker.provisioners.results import DestroyResult, ProvisionResult  # noqa: E402


class _NoDb:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def task_env(monkeypatch, lease_always_free):
    """The ``task_env`` of tests/worker/test_range_alloc.py, mirrored here: that fixture
    lives in that file's module scope and a fixture cannot be imported by module path, so
    this file keeps its own copy — state writes recorded, no API notify, no database."""
    calls: dict = {"states": []}

    def state(range_id, new_state, **kw):
        calls["states"].append((new_state, kw))
        return 1

    monkeypatch.setattr(tasks, "_update_range_state", state)
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "_db_session", _NoDb)
    monkeypatch.setattr(tasks.db_ops, "range_template_and_backend", lambda db, rid: ("{}", "vsphere_api"))
    monkeypatch.setattr(tasks.db_ops, "range_output_and_backend", lambda db, rid: ("{}", "vsphere_api"))
    return calls


# -- high, tasks.py:230 — partial vSphere teardown recorded `destroyed` ----------------
# (the audit lists this root cause twice, with the same claim and scenario)


@pytest.mark.audit443
def test_a_partial_destroy_is_not_recorded_destroyed_and_releases_nothing(task_env, monkeypatch):
    """Audit #443, high, control-plane/worker/worker/tasks.py:230 — "Partial vSphere
    teardown is recorded as `destroyed`" (listed twice; the duplicate's title: "A partial
    destroy is recorded `destroyed`: reservations released, port groups kept, leftover VM
    keeps running under a destroyed range").

    ``destroy_range`` raises only on ``result.status == "failed"``; the vSphere provisioner
    grades a teardown where at least one VM went and anything else errored ``partial``
    (worker/provisioners/results.py outcome). ``partial`` therefore sails through: the
    range is recorded ``destroyed`` and ``release_after_destroy`` frees its VLAN, uplink
    and MAC reservations for the next range, while the port groups stay and the leftover
    VM keeps running. Correct behaviour: a destroy that did not fully verify its teardown
    fails the task, records no ``destroyed`` state, and releases nothing.
    """
    released: list[str] = []

    class Prov:
        async def destroy(self, range_id, output):
            return DestroyResult(status="partial", resources_removed=4,
                                 errors=["VM vm-105: power-off timed out"])

    monkeypatch.setattr(tasks.range_alloc, "release_after_destroy",
                        lambda *a: released.append("released") or 0)
    monkeypatch.setattr(tasks, "_get_backend", lambda b, range_id=None, **kw: Prov())
    monkeypatch.setattr(tasks, "_last_attempt", lambda task: True)  # final: no retry
    with pytest.raises(Exception, match="power-off timed out|partial"):
        tasks.destroy_range.run("r-1")
    assert not released, "a partial destroy freed the range's VLAN/uplink/MAC reservations"
    assert "destroyed" not in [s for s, _ in task_env["states"]]


# -- high, schemas.py:147 — provisioner_output readable by Student and Observer --------

SECRETS = {
    "connection": {"id": "conn-1", "host": "https://vcenter.site/"},
    "uplink": {"ip": "10.30.32.101", "gateway": "10.30.32.1", "network": "dPG-TN-SVC"},
    "isolation": {"settings": {"targets": ["10.30.30.1", "10.30.30.20", "vcenter.site:443"],
                               "exceptions": ["10.30.32.10:8081"]}},
    "inventory": {"macs": ["00:50:56:3c:00:00"]},
    "vms": [{"name": "r8-dc01", "identity": {"disks": [{"path": "[esx03-local] r/r8-dc01.vmdk"}]}}],
}


def _range_with_output(db_session, tenant_id) -> Range:
    template = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=tenant_id)
    db_session.add(template)
    db_session.flush()
    rng = Range(id=uuid.uuid4(), name="team exercise range", template_id=template.id,
                tenant_id=tenant_id, state=RangeState.ready, provisioner_backend="vsphere_api",
                provisioner_output=json.dumps(SECRETS))
    db_session.add(rng)
    db_session.flush()
    return rng


@pytest.mark.audit443
def test_a_student_cannot_read_a_range_provisioner_output(client, db_session):
    """Audit #443, high, control-plane/api/app/schemas.py:147 — "Students can read the full
    ``provisioner_output`` of any non-lab range in their tenant": vCenter endpoint,
    isolation probe targets (management gateway/DNS/vCenter/depot), uplink IP and gateway,
    datastore paths, MoIDs, MACs. ``RangeOut.provisioner_output`` is the raw column and
    ``GET /ranges/{range_id}`` applies only ``RANGE_READ`` plus the lab-range hiding, so a
    Student (``rbac.py`` grants students RANGE_READ) reads an exercise range's whole
    output — the exact target list for attacking the platform's isolation guarantee.
    Correct behaviour: a caller without infrastructure rights gets no provisioner_output.
    """
    tenant = real_tenant(db_session)
    student = real_user(db_session, UserRole.student, tenant.id)
    rng = _range_with_output(db_session, tenant.id)
    act_as(student)
    body = client.get(f"/ranges/{rng.id}")
    assert body.status_code == 200, body.text
    assert body.json()["provisioner_output"] in (None, ""), "raw provisioner_output reaches a Student"


@pytest.mark.audit443
def test_an_observer_cannot_read_a_range_provisioner_output(client, db_session):
    """Audit #443, high, control-plane/api/app/schemas.py:147 — same finding, second role:
    an Observer holds ``RANGE_READ`` too (rbac.py), so the raw column reaches it as well.
    Correct behaviour: no provisioner_output without infrastructure rights."""
    tenant = real_tenant(db_session)
    observer = real_user(db_session, UserRole.observer, tenant.id)
    rng = _range_with_output(db_session, tenant.id)
    act_as(observer)
    body = client.get(f"/ranges/{rng.id}")
    assert body.status_code == 200, body.text
    assert body.json()["provisioner_output"] in (None, ""), "raw provisioner_output reaches an observer"


def test_staff_still_sees_the_provisioner_output(client, db_session):
    """Companion to the schemas.py:147 finding (passes at the base): the fix must be a
    role filter, not a blanket deletion — staff of the range's tenant still read the
    output that destroy, teardown checks and drift work from."""
    tenant = real_tenant(db_session)
    instructor = real_user(db_session, UserRole.instructor, tenant.id)
    rng = _range_with_output(db_session, tenant.id)
    act_as(instructor)
    body = client.get(f"/ranges/{rng.id}")
    assert body.status_code == 200, body.text
    shown = body.json()["provisioner_output"] or ""
    assert all(s in shown for s in ("https://vcenter.site/", "10.30.32.101", "r8-dc01.vmdk"))


# -- high, tasks.py:168 — a partial build (a VM failed to clone) recorded `ready` -------


@pytest.mark.audit443
def test_a_partial_build_is_not_settled_ready_without_a_persisted_error(task_env, monkeypatch):
    """Audit #443, high, control-plane/worker/worker/tasks.py:168 — "A partial build (a VM
    failed to clone) is recorded `ready` with no error; the errors are never persisted".

    ``provision_range`` raises only for ``result.status == "failed"``; for ``partial`` it
    asks ``range_alloc.build_outcome``, which returns ``("ready", None)`` unless
    ``not_ready`` is non-empty or isolation blocks. A VM whose clone raised is in
    ``result.errors`` but in neither ``vms`` nor ``not_ready``, so the range is recorded
    ``ready`` with ``error=None``, the API settles the provision succeeded and issues the
    credentials page. Correct behaviour: a partly built range is not settled ready without
    a persisted reason, and greyspace does not run on it.
    """
    vms = [{"vm_id": "vm-1", "name": "r-ubu01"}]

    class Prov:
        async def provision(self, range_id, template, allocations):
            return ProvisionResult(status="partial", vms=vms,
                                   errors=["VM r-dc01: Cannot create clone: datastore is full"])

    monkeypatch.setattr(tasks.range_alloc, "reserve_for_build", lambda *a: {})
    monkeypatch.setattr(tasks, "_get_backend", lambda b, range_id=None, **kw: Prov())
    monkeypatch.setattr(tasks.greyspace, "after_provision",
                        lambda *a: pytest.fail("greyspace ran for a partly built range"))
    out = tasks.provision_range.run("r-1")
    assert out["status"] != "ready" and out.get("error"), f"partial build settled {out}"
    assert "ready" not in [s for s, _ in task_env["states"]]


# -- low, vsphere_infra.py:313 — ensure_dvs_portgroup reuses a same-named port group ----


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: ensure_dvs_portgroup reuses a same-named port "
                                       "group without re-checking its VLAN or owner mark "
                                       "(vsphere_infra.py:313)")
def test_ensure_dvs_portgroup_refuses_a_same_named_foreign_port_group():
    """Audit #443, low, control-plane/worker/worker/provisioners/vsphere_infra.py:313 —
    "``ensure_dvs_portgroup`` reuses a same-named port group without re-checking its VLAN
    or owner mark, so the S1 'recheck immediately before creating' guarantee has a gap".

    The step reads the live switch once and checks every planned port group against that
    snapshot (vsphere_vlans.check); the comment at vsphere_api.py claims a port group that
    appears while the step runs "is caught by ensure_dvs_portgroup's own check, the same
    way". But ``ensure_dvs_portgroup`` returns the first port group with the requested
    name unconditionally: a same-named port group on another VLAN (or with a foreign owner
    mark) that appeared after the read is reused, and the range's VMs are wired to it.
    The snapshot check refuses exactly this state (vsphere_vlans.conflict). Correct
    behaviour: the live recheck applies the same rule — a same-named port group is only
    reusable when it carries exactly this VLAN and is unmarked or this range's.
    """
    dvs = FakeDVS()
    squat = FakePortgroup(dvs, infra.dvs_portgroup_spec(f"tn-{R8}-v150", 999, False, "made by hand"))
    dvs.portgroup.append(squat)
    with pytest.raises(live.PortgroupConflictError):
        infra.ensure_dvs_portgroup(dvs, f"tn-{R8}-v150", 150, False, lambda t: t, live.owner_mark(RANGE_ID))
    assert dvs.portgroup == [squat]  # nothing was created beside it either


def test_ensure_dvs_portgroup_still_reuses_an_unmarked_port_group_on_its_own_vlan():
    """Companion to the vsphere_infra.py:313 finding (passes at the base): the fix must
    keep the legitimate retry reuse — a port group of this name on exactly this VLAN with
    no owner mark (an earlier attempt's, pre-S1) is reused, not refused."""
    dvs = FakeDVS()
    mine = FakePortgroup(dvs, infra.dvs_portgroup_spec(f"tn-{R8}-v150", 150, False))
    dvs.portgroup.append(mine)
    pg = infra.ensure_dvs_portgroup(dvs, f"tn-{R8}-v150", 150, False, lambda t: t, live.owner_mark(RANGE_ID))
    assert pg is mine


# -- low, lab_tasks.py:60 — reconcile_lab_vms deletes by annotation/name alone ----------


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: reconcile_lab_vms deletes VMs by annotation/"
                                       "name alone, outside the S5 recorded-ID rule (lab_tasks.py:60)")
def test_reconcile_does_not_delete_a_lab_vm_by_annotation_alone(monkeypatch, lease_always_free):
    """Audit #443, low, control-plane/worker/worker/lab_tasks.py:60 — "``reconcile_lab_vms``
    deletes VMs by annotation/name alone, outside the S5 recorded-ID rule".

    The leak cleanup builds its ``leftovers`` from ``find_vms("")`` by the range's
    annotation or name prefix and passes them to ``destroy`` with no identity, so the
    destroy's ownership guard falls to the legacy mark rule and deletes. ADR 0013 §5:
    "Annotations and tags are hints, never authority to delete." Correct behaviour: a VM
    that matches only by mark — no recorded id ties it to the range — is not deleted by
    the reconcile sweep.
    """
    destroyed: list[str] = []

    class Prov:
        async def find_vms(self, prefix):
            return [{"vm_id": "vm-1", "name": "aaaaaaaa-gateway",
                     "range_id": "aaaaaaaa-1111-2222-3333-444444444444"}]

        async def destroy(self, range_id, output):
            destroyed.extend(v["vm_id"] for v in output["vms"])
            return DestroyResult(status="ok", resources_removed=len(output["vms"]))

    monkeypatch.setattr(lab_tasks, "_get_backend", lambda backend, *a, **kw: Prov())
    out = lab_tasks.reconcile_lab_vms.run(["aaaaaaaa-1111-2222-3333-444444444444"], "vsphere_api")
    assert destroyed == [], f"deleted on the mark alone: {out}"
