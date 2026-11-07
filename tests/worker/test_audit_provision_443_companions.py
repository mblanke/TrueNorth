"""Companion oracles for audit tracking issue #443 (card TN10X-AUDIT-tests-provision):
one companion per strict-xfail oracle in ``test_audit_provision_443.py`` — a second
input that pins the same secure behaviour of the same finding from another angle (a
second role, a state or shape boundary). Each of them FAILS at the base for the same
reason its oracle does, so it carries ``xfail(strict=True, ...)`` and ``audit443`` and
turns green exactly when the finding is fixed. Nothing here duplicates an oracle's
input; the oracles' own base-passing companions stay in that file.

Second inputs that were considered and dropped, because they pass at the base and so
pin no bug:
- two-tenant matrix for the schemas.py:147 finding: a Student or Observer of another
  tenant is refused already — ``_tenant_range`` scopes every by-id lookup to the
  caller's tenant (app/routers/ranges.py:122), 404.
- a second non-staff role for the same finding: ``ROLE_PERMISSIONS`` grants
  ``RANGE_READ`` to exactly admin, instructor, range_ops (staff, ``INFRA_READ``),
  student and observer (app/rbac.py); the staff half is the base-passing companion
  ``test_staff_still_sees_the_provisioner_output``.
- a ``failed``-status destroy for the tasks.py:230 finding: ``destroy_range`` already
  raises on ``status == "failed"`` (worker/tasks.py), so nothing is released at the
  base — the bug is only the ``partial`` grade between ``ok`` and ``failed``.
"""

from __future__ import annotations

import json
import uuid

import pytest
from _shared import act_as, real_tenant, real_user
from app.models import Range, RangeState, Template, UserRole

tasks = pytest.importorskip("worker.tasks")
from test_audit_provision_443 import SECRETS  # noqa: E402
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
    """The ``task_env`` of tests/worker/test_range_alloc.py, mirrored from
    test_audit_provision_443.py: that fixture lives in that file's module scope and a
    fixture cannot be imported by module path, so this file keeps its own copy — state
    writes recorded, no API notify, no database."""
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


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: Partial vSphere teardown is recorded as "
                                       "`destroyed` (control-plane/worker/worker/tasks.py:230) — companion")
def test_a_partial_destroy_that_keeps_the_port_group_is_not_recorded_destroyed_and_releases_nothing(
        task_env, monkeypatch):
    """Companion to the tasks.py:230 finding (the oracle's input: a VM whose power-off
    timed out — only free-text ``errors``, nothing else distinguishes its input). Second
    input, the other half of the duplicate's title — "port groups kept": every VM and the
    range folder went, the port group remove timed out, and the backend says so in its S5
    teardown report (vsphere_inventory.unidentified's shape: the port group left
    ``unidentified``, carrying the range's owner mark). That report is the evidence an
    administrator needs to delete the live port group afterwards
    (``delete_confirmed_teardown_items`` deletes only items listed in the range's stored
    report), so the secure fix must not discard it with the failure: the task fails,
    settles ``failed``, records no ``destroyed`` state, releases nothing — and still hands
    the report to ``record_teardown`` (worker/tasks.py:229, before the status gate)."""
    released: list[str] = []
    teardown_reports: list = []
    report = {"remaining": [],
              "unidentified": [{"kind": "portgroup", "moid": "dpg-9", "name": "dPG-TN-v150",
                                "key": "dvportgroup-9", "vlan": 150, "marks": ["owner_mark"]}],
              "not_checked": []}

    class Prov:
        async def destroy(self, range_id, output):
            return DestroyResult(status="partial", resources_removed=5, teardown=report,
                                 errors=["Port group dPG-TN-v150: destroy task timed out"])

    def record(session_factory, range_id, result):
        teardown_reports.append(result.teardown)
        return None  # the real record never raises; its storage is its own test's subject

    monkeypatch.setattr(tasks.range_alloc, "release_after_destroy",
                        lambda *a: released.append("released") or 0)
    monkeypatch.setattr(tasks, "record_teardown", record)
    monkeypatch.setattr(tasks, "_get_backend", lambda b, range_id=None, **kw: Prov())
    monkeypatch.setattr(tasks, "_last_attempt", lambda task: True)  # final: no retry
    with pytest.raises(Exception, match="destroy task timed out|partial"):
        tasks.destroy_range.run("r-1")
    assert not released, "a partial destroy freed the range's VLAN/uplink/MAC reservations"
    assert "destroyed" not in [s for s, _ in task_env["states"]]
    assert "failed" in [s for s, _ in task_env["states"]], "the partial destroy settled nowhere"
    assert teardown_reports == [report], "the surviving port group's teardown report was discarded"


# -- high, tasks.py:168 — a partial build (a VM failed to clone) recorded `ready` -------


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: A partial build (a VM failed to clone) is "
                                       "recorded `ready` with no error (tasks.py:168) — companion")
def test_a_partial_build_persists_its_error_and_is_never_settled_ready(task_env, monkeypatch):
    """Companion to the tasks.py:168 finding (the oracle's input pins the greyspace half:
    greyspace must not run on a partly built range). Second input, the recording half the
    audit titles: a clone that failed for another reason, observed through the task's
    return value and the persisted state — the range is not settled ``ready`` and an
    error is persisted, so the API cannot issue the credentials page over a half build."""
    vms = [{"vm_id": "vm-2", "name": "r-sql01"}]

    class Prov:
        async def provision(self, range_id, template, allocations):
            return ProvisionResult(status="partial", vms=vms,
                                   errors=["VM r-sql01: Cannot create clone: cannot migrate vm"])

    monkeypatch.setattr(tasks.range_alloc, "reserve_for_build", lambda *a: {})
    monkeypatch.setattr(tasks, "_get_backend", lambda b, range_id=None, **kw: Prov())
    monkeypatch.setattr(tasks.greyspace, "after_provision", lambda *a, **k: None)
    out = tasks.provision_range.run("r-1")
    assert out["status"] != "ready" and out.get("error"), f"partial build settled {out}"
    assert "ready" not in [s for s, _ in task_env["states"]]


# -- high, schemas.py:147 — provisioner_output readable by Student and Observer --------


def _range_in_state(db_session, tenant_id, state) -> Range:
    """``_range_with_output`` of test_audit_provision_443.py with the state free: a
    ``failed`` build keeps its stored output (destroy and the retry work from it)."""
    template = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=tenant_id)
    db_session.add(template)
    db_session.flush()
    rng = Range(id=uuid.uuid4(), name="team exercise range", template_id=template.id,
                tenant_id=tenant_id, state=state, provisioner_backend="vsphere_api",
                provisioner_output=json.dumps(SECRETS))
    db_session.add(rng)
    db_session.flush()
    return rng


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: Students can read the full provisioner_output "
                                       "of any non-lab range in their tenant (schemas.py:147) — companion")
def test_a_student_cannot_read_a_failed_range_provisioner_output(client, db_session):
    """Companion to the schemas.py:147 finding (the oracle's input: a ``ready`` range).
    Boundary value of the state: a ``failed`` build is exactly the range with leftovers —
    its stored output holds the connection, uplink and inventory the retry and destroy
    work from — and the reading path never consults the state, so the raw column reaches
    a Student here too. Correct behaviour: no provisioner_output without infrastructure
    rights, in every state."""
    tenant = real_tenant(db_session)
    student = real_user(db_session, UserRole.student, tenant.id)
    rng = _range_in_state(db_session, tenant.id, RangeState.failed)
    act_as(student)
    body = client.get(f"/ranges/{rng.id}")
    assert body.status_code == 200, body.text
    assert body.json()["provisioner_output"] in (None, ""), "raw provisioner_output reaches a Student"


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: Students can read the full provisioner_output "
                                       "of any non-lab range in their tenant (schemas.py:147) — companion")
def test_an_observer_cannot_read_a_failed_range_provisioner_output(client, db_session):
    """Companion to the schemas.py:147 finding for the Observer (the oracle's input: a
    ``ready`` range). Same boundary value of the state — a ``failed`` range's raw output
    reaches a caller with only ``RANGE_READ``. The other second angles are closed at the
    base (see the module docstring): the remaining ``RANGE_READ`` roles are staff, and
    another tenant's range is 404 already, so the state boundary is the angle left."""
    tenant = real_tenant(db_session)
    observer = real_user(db_session, UserRole.observer, tenant.id)
    rng = _range_in_state(db_session, tenant.id, RangeState.failed)
    act_as(observer)
    body = client.get(f"/ranges/{rng.id}")
    assert body.status_code == 200, body.text
    assert body.json()["provisioner_output"] in (None, ""), "raw provisioner_output reaches an observer"


# -- low, vsphere_infra.py:313 — ensure_dvs_portgroup reuses a same-named port group ----


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: ensure_dvs_portgroup reuses a same-named port "
                                       "group without re-checking its VLAN or owner mark "
                                       "(vsphere_infra.py:313) — companion")
def test_ensure_dvs_portgroup_refuses_a_same_named_port_group_marked_by_another_range():
    """Companion to the vsphere_infra.py:313 finding (the oracle's input: the right name
    on the wrong VLAN). Boundary value the other way round: the port group carries
    exactly this VLAN but another range's owner mark — the reuse rule's second half
    ("unmarked or this range's"). The name match returns it before any check, so the
    range's VMs are wired to a port group another range owns. Same secure behaviour: the
    live recheck refuses it, the way the snapshot check does (vsphere_vlans.conflict)."""
    dvs = FakeDVS()
    foreign = FakePortgroup(dvs, infra.dvs_portgroup_spec(f"tn-{R8}-v150", 150, False,
                                                          live.owner_mark("99999999-1111-2222-3333-444444444444")))
    dvs.portgroup.append(foreign)
    with pytest.raises(live.PortgroupConflictError):
        infra.ensure_dvs_portgroup(dvs, f"tn-{R8}-v150", 150, False, lambda t: t, live.owner_mark(RANGE_ID))
    assert dvs.portgroup == [foreign]  # nothing was created beside it either


# -- low, lab_tasks.py:60 — reconcile_lab_vms deletes by annotation/name alone ----------


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: reconcile_lab_vms deletes VMs by annotation/"
                                       "name alone, outside the S5 recorded-ID rule (lab_tasks.py:60) — companion")
def test_reconcile_does_not_delete_a_lab_vm_by_name_prefix_alone(monkeypatch, lease_always_free):
    """Companion to the lab_tasks.py:60 finding (the oracle's input: a VM that matches by
    the range's annotation). Second input, the match's other leg: a VM built before the
    tag era, carrying no ``range_id`` at all, found only by its name prefix. Neither a
    recorded id nor anything else ties it to the range, so the S5 rule refuses it the
    same way — annotations and names are hints, never authority to delete (ADR 0013 §5)."""
    destroyed: list[str] = []

    class Prov:
        async def find_vms(self, prefix):
            return [{"vm_id": "vm-7", "name": "bbbbbbbb-1111-2222-3333-444444444444-spare"}]

        async def destroy(self, range_id, output):
            destroyed.extend(v["vm_id"] for v in output["vms"])
            return DestroyResult(status="ok", resources_removed=len(output["vms"]))

    monkeypatch.setattr(lab_tasks, "_get_backend", lambda backend, *a, **kw: Prov())
    out = lab_tasks.reconcile_lab_vms.run(["bbbbbbbb-1111-2222-3333-444444444444"], "vsphere_api")
    assert destroyed == [], f"deleted on the name prefix alone: {out}"
