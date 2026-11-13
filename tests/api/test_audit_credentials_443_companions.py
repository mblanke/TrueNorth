"""Companion oracles for audit issue #443 (range credentials and rotation).

One companion per REAL oracle in ``test_audit_credentials_443.py``: the same secure
behaviour pinned from a second input, so a fix that satisfies only that oracle's exact
scenario cannot slip past. Each fails at the base commit (280a838b) for the same reason as
its oracle and becomes the fix's second acceptance check. No app code is changed here.

Companions (oracle -> the second input):

* :test:`test_the_same_never_applied_rotation_lies_in_both_tenants` — #443-1
  (``service.py:450``): a two-tenant matrix — the identical never-applied rotation and
  failed settle carried out by each tenant's own admin on its own range; the lie must be
  absent in both tenants.
* :test:`test_an_instructor_cannot_delete_a_snapshot_over_a_pending_restore_rotation` —
  #443-2 (``ranges.py:964``): a second role — the instructor holds ``range:destroy``
  (app/rbac.py ROLE_PERMISSIONS) and so reaches the same delete; the refusal of a snapshot
  whose restore rotation is pending must live on the endpoint, not on the admin's path
  alone.
* :test:`test_force_settling_over_a_pending_restore_rotation_leaves_a_lie_behind` — #443-3
  (``service.py:414``): the other kind of pending change at that boundary — the
  restore-tagged rotation (``restore:<id>``), whose in-guest password (and so what
  ``force_settle`` writes) is the snapshot's seal, not the previous password; the worker's
  settle of the version it set must still win.
* :test:`test_a_failed_post_restore_rotation_send_must_not_strand_either_student_row` —
  #443-4 (``credential_rotation.py:258``): the first non-trivial value of "every Student
  row of the class" — a template with two student-visible accounts; the fix must unstrand
  both rows, not just one.

Fixtures and helpers come from the neighbouring files by module path (never ``from
conftest import``): ``client``/``db_session`` live in tests/conftest.py, the
``_rotate``/``_row``/``_settle``/``_snapshot`` helpers and ``TEMPLATE_YAML`` in the shared
building blocks of ``test_range_credentials`` and ``test_range_credentials_hardening``. The
``world`` and ``no_tasks`` fixtures are the audit suite's, under fresh tenant slugs.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from _shared import act_as, real_tenant, real_user
from app.models import Range, RangeSnapshot, RangeState, Template, UserRole
from app.range_credentials import RangeCredential
from app.range_credentials import service as rc
from test_range_credentials import TEMPLATE_YAML
from test_range_credentials_hardening import _rotate, _row, _settle, _snapshot

# A second student-visible account: the template of test_a_failed_post_restore_...
# with one more Student login, on the same guest the worker can change.
TWO_STUDENT_YAML = """
name: two-students
nodes:
  - {id: web01, os: ubuntu-2404}
accounts:
  - {machine: web01, username: analyst, generate: true, student_visible: true}
  - {machine: web01, username: trainee, generate: true, student_visible: true}
"""


@pytest.fixture
def world(db_session):
    """The audit suite's two-tenant world, under fresh slugs (tenant ``a`` with its admin,
    instructor and range_ops, tenant ``b`` with a foreign admin)."""
    a, b = real_tenant(db_session, "audit443c-a"), real_tenant(db_session, "audit443c-b")
    w = {"a": a, "b": b}
    for name, role, tenant in (("admin", UserRole.admin, a), ("instructor", UserRole.instructor, a),
                               ("range_ops", UserRole.range_ops, a), ("foreign_admin", UserRole.admin, b)):
        w[name] = real_user(db_session, role, tenant.id)
    return w


@pytest.fixture
def no_tasks(monkeypatch):
    """The hardening suite's dispatch recorder: every Celery send is recorded, none leaves."""
    from app import celery_client
    from app.routers import ranges as ranges_router

    sent = []
    monkeypatch.setattr(ranges_router, "_dispatch_task", lambda name, *a: sent.append((name, *a)) or "t")
    monkeypatch.setattr(celery_client, "dispatch", lambda name, *a: sent.append((name, *a)) or "t")
    return sent


def _range_for(client, db, world, tenant: str, yaml: str, state: RangeState | None = None) -> Range:
    """The hardening suite's ``_range`` (a template's accounts, a ready range on a backend
    that changes guest passwords, every account applied as a build would report), built in
    ``tenant``'s own range by that tenant's admin."""
    tmpl = Template(id=uuid.uuid4(), name=f"companion-{tenant}", version="1", yaml=yaml,
                    tenant_id=world[tenant].id)
    db.add(tmpl)
    db.flush()
    act_as(world["admin" if tenant == "a" else "foreign_admin"])
    resp = client.post("/ranges", json={"name": "Companion", "template_id": str(tmpl.id)})
    assert resp.status_code == 201, resp.text
    rng = db.get(Range, uuid.UUID(resp.json()["id"]))
    if state is not None:
        rng.state, rng.provisioner_backend = state, "vsphere_api"
        rng.provisioner_output = json.dumps({"vms": [{"vm_id": "vm-1", "name": f"{str(rng.id)[:8]}-web01",
                                                      "node_id": "web01", "family": "linux"}]})
    for row in db.query(RangeCredential).filter(RangeCredential.range_id == rng.id):
        row.applied = True
    db.commit()
    return rng


# ── #443-1: a rotation of a never-applied account that fails in the guest ───────────────
@pytest.mark.audit443
def test_the_same_never_applied_rotation_lies_in_both_tenants(client, db_session, world, no_tasks):
    """#443-1 companion (high) — control-plane/api/app/range_credentials/service.py:450,
    pinned from a two-tenant matrix: each tenant's own admin rotates the never-applied
    analyst account of its own range and the worker's change fails. The lie (a row settled
    ``applied=True`` with a password no build or rotation ever verified) must be absent in
    both tenants — whatever guard or settle fix lands cannot be correct in one tenant and
    absent in the other, and neither tenant's row may be settled from the other's."""
    from worker import db_ops

    rows = {}
    for tenant, admin in (("a", "admin"), ("b", "foreign_admin")):
        rng = _range_for(client, db_session, world, tenant, TEMPLATE_YAML, RangeState.ready)
        row = _row(db_session, rng, "web01", "analyst")
        row.applied = False  # the build never set or verified this account in the guest
        db_session.commit()
        act_as(world[admin])
        resp = _rotate(client, rng, row)
        if resp.status_code != 202:  # a refusal of a not-applied row also fixes the finding
            assert resp.status_code == 409, resp.text
            rows[tenant] = _row(db_session, rng, "web01", "analyst")
            continue
        operation = resp.json()["operation_id"]
        db_session.expire_all()
        row = _row(db_session, rng, "web01", "analyst")
        # The worker's real failure path (credential_rotation._settle -> settle_credential_change):
        # the guest did not take the new password.
        assert db_ops.settle_credential_change(db_session, str(rng.id), str(row.id), row.sealed_password,
                                               applied=False, operation=operation)
        rows[tenant] = _row(db_session, rng, "web01", "analyst")
    for tenant, row in rows.items():
        assert not row.applied or row.pending, (
            f"tenant {tenant}: a failed rotation of an account the build never set settled as applied=True "
            "with the pre-rotation password, which the guest never had: the Student panel calls it verified "
            "and the certification's INCOMPLETE turns into a PASS no build ever earned")


# ── #443-2: a snapshot deleted over its pending restore rotation ────────────────────────
@pytest.mark.audit443
def test_an_instructor_cannot_delete_a_snapshot_over_a_pending_restore_rotation(client, db_session, world, no_tasks):
    """#443-2 companion (high) — control-plane/api/app/routers/ranges.py:964, pinned from a
    second role: the instructor holds ``range:destroy`` (app/rbac.py ROLE_PERMISSIONS) and
    so reaches the same ``DELETE .../snapshots/{id}``. In the same window as the audit
    suite's oracle (the restore's revert ran, its ``restore:<id>`` rotation still pending),
    the delete must be refused with 409 for this role too — the guard has to live on the
    endpoint, because ``forget_snapshot`` destroys the recorded passwords the queued
    rotation logs in with and reverts to whoever asks."""
    rng = _range_for(client, db_session, world, "a", TEMPLATE_YAML, RangeState.ready)
    act_as(world["admin"])
    snap = _snapshot(client, db_session, rng)
    assert _rotate(client, rng, _row(db_session, rng, "web01", "analyst")).status_code == 202
    _settle(db_session, _row(db_session, rng, "web01", "analyst").id)  # the worker verified it
    assert client.post(f"/ranges/{rng.id}/snapshots/{snap.id}/restore").status_code == 202
    db_session.get(RangeSnapshot, snap.id).snapshot_state = "ready"  # the revert ran; rotation queued
    db_session.commit()
    assert rc.any_pending(db_session, rng)  # the restore:<id> rotation is still being applied
    act_as(world["instructor"])
    assert client.delete(f"/ranges/{rng.id}/snapshots/{snap.id}").status_code == 409, (
        "an instructor (range:destroy) can delete the snapshot while its restore rotation is "
        "pending, destroying the recorded passwords that rotation logs in with and reverts to")


# ── #443-3: force_settle racing a still-running worker task ─────────────────────────────
@pytest.mark.audit443
@pytest.mark.xfail(strict=True,
                   reason="audit #443: force_settle has no guard against a still-running worker task; the later "
                          "CAS miss is logged at INFO (control-plane/api/app/range_credentials/service.py:414) — "
                          "companion")
def test_force_settling_over_a_pending_restore_rotation_leaves_a_lie_behind(client, db_session, world, no_tasks):
    """#443-3 companion (medium) — control-plane/api/app/range_credentials/service.py:414,
    pinned from the other kind of pending change: the restore-tagged rotation
    (``restore:<id>``) still being applied once its revert has run. Its in-guest password —
    and so what ``force_settle`` writes — is the snapshot's seal, not the previous password
    (service.in_guest_password); the worker's task then succeeds in the guest anyway and its
    compare-and-set (worker/db_ops.py:336) misses, reported only at INFO. Correct behaviour:
    the worker's settle of the version it set wins, so the row ends with the password the
    guest actually has — not the snapshot's seal, which the guest no longer accepts and
    which reveal hands out."""
    from worker import db_ops

    rng = _range_for(client, db_session, world, "a", TEMPLATE_YAML, RangeState.ready)
    act_as(world["admin"])
    snap = _snapshot(client, db_session, rng)
    assert _rotate(client, rng, _row(db_session, rng, "web01", "analyst")).status_code == 202
    _settle(db_session, _row(db_session, rng, "web01", "analyst").id)  # the worker verified it
    assert client.post(f"/ranges/{rng.id}/snapshots/{snap.id}/restore").status_code == 202
    db_session.get(RangeSnapshot, snap.id).snapshot_state = "ready"  # the revert ran; rotation queued
    db_session.commit()
    assert rc.any_pending(db_session, rng)
    # What the restore's rotation task read and is about to set (credential_rotation.run):
    rows = {r["id"]: r for r in db_ops.account_rows(db_session, str(rng.id))}
    row = _row(db_session, rng, "web01", "analyst")
    task = rows[str(row.id)]
    sealed, new_pw, operation = task["sealed"], task["password"], task["operation"]
    assert operation == f"restore:{snap.id}" and new_pw != sealed  # a fresh rotation, not yet in the guest
    # The admin force-settles while that task is still in flight:
    resp = client.post(f"/ranges/{rng.id}/credentials/{row.id}/settle", json={"reason": "assumed stuck"})
    if resp.status_code != 200:  # a refusal while the task can still be in flight also fixes it
        assert resp.status_code == 409, resp.text
        return
    # ... and the task then succeeds in the guest and settles the version it set:
    landed = db_ops.settle_credential_change(db_session, str(rng.id), str(row.id), sealed,
                                             applied=True, operation=operation)
    db_session.expire_all()
    row = _row(db_session, rng, "web01", "analyst")
    assert landed and row.password == new_pw, (
        "the worker's settle of the restore rotation's password lost to the admin's force_settle (only an "
        "INFO line): the row now shows the snapshot's seal, which the guest no longer accepts, and reveal "
        "hands it out")


# ── #443-4: a failed post-restore rotation send strands the rows ────────────────────────
@pytest.mark.audit443
@pytest.mark.xfail(strict=True,
                   reason="audit #443: Post-restore rotation dispatched only from a task_success signal; a failed "
                          "send strands every pending row (control-plane/worker/worker/credential_rotation.py:258)"
                          " — companion")
def test_a_failed_post_restore_rotation_send_must_not_strand_either_student_row(client, db_session, world, no_tasks,
                                                                                monkeypatch):
    """#443-4 companion (low) — control-plane/worker/worker/credential_rotation.py:258,
    pinned from the first non-trivial value of the finding's "every Student row of the
    class": a template with two student-visible accounts, both rotated and both pending
    under the restore's rotation when the send fails. Correct behaviour: neither row is
    stranded — both go back to the snapshot seal the reverted guests actually have (the
    rollback direction the audit suite's oracle accepts) or are otherwise un-stranded. A fix
    that unstrands only one row, or puts the rows back to the pre-restore password the
    reverted guests no longer have, fails here."""
    import worker.tasks as tasks
    from worker import credential_rotation as cr

    @contextmanager
    def _api_db():  # a fix's rollback runs through the worker's session factory
        yield db_session

    monkeypatch.setattr(tasks, "_db_session", _api_db)

    def down(*a, **k):
        raise ConnectionError("broker down")

    # On the class, as conftest's no_real_broker does: patching send_task on the app
    # instance would outlive monkeypatch's undo (the trap the audit suite's oracle records).
    from celery import Celery

    monkeypatch.setattr(Celery, "send_task", down)  # the broker refuses the rotation send
    rng = _range_for(client, db_session, world, "a", TWO_STUDENT_YAML, RangeState.ready)
    act_as(world["admin"])
    snap = _snapshot(client, db_session, rng)
    at_snapshot = {name: _row(db_session, rng, "web01", name).password  # the passwords the snapshot sealed
                   for name in ("analyst", "trainee")}
    for name in ("analyst", "trainee"):
        assert _rotate(client, rng, _row(db_session, rng, "web01", name)).status_code == 202
        _settle(db_session, _row(db_session, rng, "web01", name).id)  # the worker verified it
    assert client.post(f"/ranges/{rng.id}/snapshots/{snap.id}/restore").status_code == 202
    db_session.get(RangeSnapshot, snap.id).snapshot_state = "ready"  # the revert ran
    db_session.commit()
    assert rc.any_pending(db_session, rng)
    cr._after_restore(sender=SimpleNamespace(name=cr.RESTORE_TASK,
                                             request=SimpleNamespace(args=[str(rng.id), str(snap.id)])),
                      result={"status": "restored", "range_id": str(rng.id)})
    assert not rc.any_pending(db_session, rng), (
        "the rotation send failed and both restore-pending student rows are stranded: nothing "
        "sweeps or retries them, every later restore of the range is refused 409 and the only "
        "recovery is one manual settle per account")
    for name, p0 in at_snapshot.items():
        row = _row(db_session, rng, "web01", name)
        assert row.pending or row.password == p0, (
            f"{name}: the row was put back to its pre-restore password, not to the snapshot seal the "
            "reverted guests actually have: reveal hands out a password that no longer logs in")
