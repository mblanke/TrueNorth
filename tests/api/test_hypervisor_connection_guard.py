"""A hypervisor connection live ranges are on cannot be deleted, deactivated or repointed;
a vSphere connection cannot exist without a password (review of #131, 2026-10-11).

The worker records which connection built a range and reaches only that one afterwards
(worker/connection_binding.py). Before that, deleting or deactivating the connection sent
the range's next destroy to another vCenter, where it deleted VMs by bare id; and a
connection saved without a password made the worker send the platform's own
VSPHERE_PASSWORD to the host the tenant had typed in.
"""

import json
import uuid

import pytest
from app import models as m
from app.models import HypervisorConnection

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _conn(db, *, tenant=DEV_TENANT, host="vc-a.test") -> HypervisorConnection:
    conn = HypervisorConnection(id=uuid.uuid4(), tenant_id=tenant, name=host, hypervisor_type="vsphere", host=host,
                                port=443, username="svc", password_encrypted="x", is_active=True)
    db.add(conn)
    db.flush()
    return conn


def _range(db, *, state=m.RangeState.ready, output=None, tenant=DEV_TENANT) -> m.Range:
    tmpl = m.Template(name=f"t-{uuid.uuid4().hex[:6]}", yaml="vms: []", tenant_id=tenant)
    db.add(tmpl)
    db.flush()
    rng = m.Range(name=f"r-{uuid.uuid4().hex[:6]}", template_id=tmpl.id, tenant_id=tenant, state=state,
                  provisioner_backend="vsphere_api", provisioner_output=json.dumps(output) if output else None)
    db.add(rng)
    db.flush()
    return rng


def _built_on(conn) -> dict:
    return {"vms": [{"name": "a", "vm_id": "vm-1"}], "connection": {"id": str(conn.id), "host": f"https://{conn.host}"}}


# -- password ---------------------------------------------------------------
@pytest.mark.parametrize("password", [None, "", "   "])
def test_a_vsphere_connection_without_a_password_is_refused(client, password):
    body = {"name": "x", "hypervisor_type": "vsphere", "host": "attacker.example", "port": 443, "username": "u"}
    if password is not None:
        body["password"] = password
    r = client.post("/hypervisors/connections", json=body)
    assert r.status_code == 422, r.text
    assert "password" in r.json()["detail"]


def test_a_vsphere_connection_with_a_password_is_created(client):
    body = {"name": "x", "hypervisor_type": "vsphere", "host": "vc.test", "port": 443, "username": "u",
            "password": "pw"}
    assert client.post("/hypervisors/connections", json=body).status_code == 201


def test_a_password_can_be_rotated_but_not_cleared(client, db_session):
    conn = _conn(db_session)
    _range(db_session, output=_built_on(conn))  # rotating is fine even with live ranges on it
    db_session.commit()
    assert client.patch(f"/hypervisors/connections/{conn.id}", json={"password": ""}).status_code == 422
    assert client.patch(f"/hypervisors/connections/{conn.id}", json={"password": None}).status_code == 422
    assert client.patch(f"/hypervisors/connections/{conn.id}", json={"password": "new"}).status_code == 200


# -- live ranges --------------------------------------------------------------
@pytest.mark.parametrize("state", [m.RangeState.ready, m.RangeState.stopped, m.RangeState.failed,
                                   m.RangeState.destroying])
def test_a_connection_with_a_live_range_on_it_cannot_be_deleted_deactivated_or_repointed(client, db_session, state):
    conn = _conn(db_session)
    rng = _range(db_session, state=state, output=_built_on(conn))
    db_session.commit()

    r = client.delete(f"/hypervisors/connections/{conn.id}")
    assert r.status_code == 409 and str(rng.id) in r.json()["detail"]
    assert client.patch(f"/hypervisors/connections/{conn.id}", json={"is_active": False}).status_code == 409
    assert client.patch(f"/hypervisors/connections/{conn.id}", json={"host": "vc-other.test"}).status_code == 409
    db_session.refresh(conn)
    assert (conn.is_active, conn.host) == (True, "vc-a.test")
    # Anything that does not move the range: allowed.
    assert client.patch(f"/hypervisors/connections/{conn.id}", json={"name": "renamed"}).status_code == 200
    assert client.patch(f"/hypervisors/connections/{conn.id}", json={"host": "vc-a.test"}).status_code == 200


def test_destroyed_ranges_and_ranges_on_another_connection_do_not_block(client, db_session):
    conn, other = _conn(db_session), _conn(db_session, host="vc-b.test")
    _range(db_session, state=m.RangeState.destroyed, output=_built_on(conn))
    _range(db_session, output=_built_on(other))
    _range(db_session, state=m.RangeState.created)  # not built yet: it will record what it gets
    db_session.commit()
    assert client.patch(f"/hypervisors/connections/{conn.id}", json={"is_active": False}).status_code == 200
    assert client.delete(f"/hypervisors/connections/{conn.id}").status_code == 204


def test_a_range_built_before_the_record_blocks_conservatively(client, db_session):
    """Nothing recorded: the worker resolves its connection afresh, so it may be on this one."""
    conn = _conn(db_session)
    _range(db_session, output={"vms": [{"name": "a", "vm_id": "vm-1"}]})
    db_session.commit()
    assert client.delete(f"/hypervisors/connections/{conn.id}").status_code == 409


def test_another_tenants_ranges_never_block_a_tenant_connection(client, db_session):
    other_tenant = uuid.UUID("00000000-0000-0000-0000-0000000000ff")
    db_session.add(m.Tenant(id=other_tenant, name="other", slug=f"other-{uuid.uuid4().hex[:6]}"))
    db_session.flush()
    conn = _conn(db_session)
    _range(db_session, output={"vms": [{"name": "a", "vm_id": "vm-1"}]}, tenant=other_tenant)
    db_session.commit()
    assert client.delete(f"/hypervisors/connections/{conn.id}").status_code == 204
