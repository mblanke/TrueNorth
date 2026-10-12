"""POST /ranges/{id}/abandon: a platform administrator writes off a failed range without a
hypervisor call (#131 review 3, should-fix 1a), and a range whose destroy found none of its
VMs no longer blocks the tenant's connection changes (1b).
"""

import json
import uuid
from contextlib import contextmanager

import pytest
from app import models as m
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import AuditLog, HypervisorConnection, UserRole

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
REASON = "VMs deleted by hand when vc-old was decommissioned"


@contextmanager
def acting_as(role: UserRole, tenant: str = DEV_TENANT):
    # The seeded dev principal's id (tests/conftest.py): the audit log's user_id is a foreign key.
    who = CurrentUser(id=DEV_TENANT, email=f"{role.value}@example.test", display_name=role.value, role=role,
                      tenant_id=tenant, keycloak_id=f"kc-{uuid.uuid4().hex[:8]}")
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def platform_tenant(monkeypatch):
    monkeypatch.setenv("PLATFORM_TENANT_ID", DEV_TENANT)


def _range(db, state=m.RangeState.failed, output=None):
    tmpl = m.Template(name=f"t-{uuid.uuid4().hex[:6]}", yaml="vms: []", tenant_id=uuid.UUID(DEV_TENANT))
    db.add(tmpl)
    db.flush()
    rng = m.Range(name="r", template_id=tmpl.id, tenant_id=uuid.UUID(DEV_TENANT), state=state,
                  provisioner_backend="vsphere_api",
                  provisioner_output=json.dumps(output or {"vms": [{"name": "abcd-web", "vm_id": "vm-9"}],
                                                           "networks": [{"name": "lan", "portgroup": "tn-abcd-v100"}]}))
    db.add(rng)
    db.commit()
    return rng


def test_a_platform_admin_abandons_a_failed_range_with_a_record_and_an_audit(client, db_session):
    rng = _range(db_session)
    with acting_as(UserRole.admin) as admin:
        r = client.post(f"/ranges/{rng.id}/abandon", json={"reason": REASON})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "destroyed"
    db_session.refresh(rng)
    out = json.loads(rng.provisioner_output)
    assert out["abandoned"]["by"] == admin.id and out["abandoned"]["reason"] == REASON
    assert out["abandoned"]["vms"] == [{"name": "abcd-web", "vm_id": "vm-9"}]
    assert out["abandoned"]["networks"] == ["tn-abcd-v100"] and out["abandoned"]["at"]
    assert out["vms"]  # the rest of the record is kept
    log = db_session.query(AuditLog).filter(AuditLog.action == "abandon_range",
                                            AuditLog.resource_id == str(rng.id)).one()
    assert REASON in log.detail and "vm-9" in log.detail


@pytest.mark.parametrize("role", [UserRole.range_ops, UserRole.instructor])
def test_a_tenant_role_cannot_abandon(client, db_session, role):
    rng = _range(db_session)
    with acting_as(role):
        assert client.post(f"/ranges/{rng.id}/abandon", json={"reason": REASON}).status_code == 403
    db_session.refresh(rng)
    assert rng.state == m.RangeState.failed


def test_an_admin_of_another_tenant_is_not_the_platform_admin(client, db_session):
    rng = _range(db_session)
    with acting_as(UserRole.admin, tenant="00000000-0000-0000-0000-0000000000ff"):
        assert client.post(f"/ranges/{rng.id}/abandon", json={"reason": REASON}).status_code == 403


@pytest.mark.parametrize("state", [m.RangeState.ready, m.RangeState.destroying, m.RangeState.provisioning])
def test_only_a_failed_range_can_be_abandoned(client, db_session, state):
    rng = _range(db_session, state=state)
    with acting_as(UserRole.admin):
        assert client.post(f"/ranges/{rng.id}/abandon", json={"reason": REASON}).status_code == 409


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "short"}])
def test_a_reason_is_required(client, db_session, body):
    rng = _range(db_session)
    with acting_as(UserRole.admin):
        assert client.post(f"/ranges/{rng.id}/abandon", json=body).status_code == 422


def test_abandoning_unblocks_the_tenants_connection_changes(client, db_session):
    a = HypervisorConnection(tenant_id=uuid.UUID(DEV_TENANT), name="a", hypervisor_type="vsphere", host="vc-a",
                             port=443, username="u", password_encrypted="x", is_primary=True)
    b = HypervisorConnection(tenant_id=uuid.UUID(DEV_TENANT), name="b", hypervisor_type="vsphere", host="vc-b",
                             port=443, username="u", password_encrypted="x")
    db_session.add_all([a, b])
    rng = _range(db_session)  # unrecorded, VMs listed, failed
    with acting_as(UserRole.admin):
        assert client.post(f"/hypervisors/connections/{b.id}/set-primary").status_code == 409
        assert client.post(f"/ranges/{rng.id}/abandon", json={"reason": REASON}).status_code == 200
        assert client.post(f"/hypervisors/connections/{b.id}/set-primary").status_code == 200


def test_a_range_whose_destroy_found_no_vms_does_not_block_the_recommended_repoint(client, db_session):
    """1b: the worker marks it (unrecorded_vms_missing); moving the preferred connection is
    the fix its error recommends, so the guard lets it through."""
    a = HypervisorConnection(tenant_id=uuid.UUID(DEV_TENANT), name="a", hypervisor_type="vsphere", host="vc-a",
                             port=443, username="u", password_encrypted="x", is_primary=True)
    b = HypervisorConnection(tenant_id=uuid.UUID(DEV_TENANT), name="b", hypervisor_type="vsphere", host="vc-old",
                             port=443, username="u", password_encrypted="x")
    db_session.add_all([a, b])
    _range(db_session, output={"vms": [{"name": "abcd-web", "vm_id": "vm-9"}],
                               "unrecorded_vms_missing": {"host": "https://vc-a", "at": "2026-10-11T00:00:00Z"}})
    db_session.commit()
    with acting_as(UserRole.admin):
        assert client.post(f"/hypervisors/connections/{b.id}/set-primary").status_code == 200
        assert client.patch(f"/hypervisors/connections/{a.id}", json={"host": "vc-old-2"}).status_code == 200
    # Not failed (a retry of the destroy is still in flight): still counted.
    _range(db_session, state=m.RangeState.destroying,
           output={"vms": [{"name": "x", "vm_id": "vm-1"}], "unrecorded_vms_missing": {"host": "h", "at": "t"}})
    with acting_as(UserRole.admin):
        assert client.post(f"/hypervisors/connections/{a.id}/set-primary").status_code == 409
