"""Hypervisor control is tenant-scoped, and TLS is verified by default (security sweep M6).

/proxmox/vms/{node}/{vmid}/* needed only infra:control: a range_ops user of one tenant
could stop, snapshot, destroy or open a console on another tenant's VMs on the shared
cluster. New hypervisor connections defaulted to verify_ssl=False.
"""

from __future__ import annotations

import json
import uuid

import pytest
from _shared import act_as, real_tenant, real_user
from app.models import HypervisorConnection, Range, Template, UserRole
from app.routers import proxmox


@pytest.fixture
def cluster(db_session, monkeypatch):
    a, b, op = (real_tenant(db_session, s) for s in ("pve-a", "pve-b", "pve-op"))
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(op.id))
    t = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=a.id)
    db_session.add(t)
    db_session.flush()
    db_session.add(Range(id=uuid.uuid4(), name="r", template_id=t.id, tenant_id=a.id,
                         provisioner_output=json.dumps({"vms": [{"name": "dc01", "vmid": 9001}]})))
    db_session.flush()
    calls: list = []

    class _Px:
        def __getattr__(self, name):
            return self

        def __call__(self, *a, **k):
            return self

    async def fake_run(func, *args, **kwargs):
        calls.append(func)
        return {"status": "running", "name": "dc01"}

    monkeypatch.setattr(proxmox, "_get_client", lambda: _Px())
    monkeypatch.setattr(proxmox, "_run", fake_run)
    return {"a": a, "b": b, "op": op, "calls": calls}


def test_another_tenants_range_ops_cannot_touch_the_vm(client, db_session, cluster):
    act_as(real_user(db_session, UserRole.range_ops, cluster["b"].id))
    for method, path in (("get", "/proxmox/vms/pve1/9001"), ("post", "/proxmox/vms/pve1/9001/stop"),
                         ("delete", "/proxmox/vms/pve1/9001"), ("post", "/proxmox/vms/pve1/9001/vnc")):
        assert getattr(client, method)(path).status_code == 404, path
    assert cluster["calls"] == []  # Proxmox was never called


def test_the_owning_tenant_can(client, db_session, cluster):
    act_as(real_user(db_session, UserRole.range_ops, cluster["a"].id))
    assert client.post("/proxmox/vms/pve1/9001/stop").status_code == 200
    assert client.post("/proxmox/vms/pve1/4242/stop").status_code == 404  # not one of its ranges' VMs


def test_cluster_wide_routes_are_for_the_platform_admin(client, db_session, cluster):
    act_as(real_user(db_session, UserRole.range_ops, cluster["a"].id))
    assert client.get("/proxmox/cluster/status").status_code == 403
    act_as(real_user(db_session, UserRole.admin, cluster["a"].id))  # a tenant's admin is not the operator
    assert client.get("/proxmox/cluster/status").status_code == 403
    act_as(real_user(db_session, UserRole.admin, cluster["op"].id))
    assert client.get("/proxmox/cluster/status").status_code == 200
    assert client.post("/proxmox/vms/pve1/9001/stop").status_code == 200  # any VM


def test_cluster_wide_routes_fail_closed_when_platform_tenant_unset(client, db_session, cluster, monkeypatch):
    """Several tenants and no PLATFORM_TENANT_ID: nobody is the operator, nobody crosses."""
    monkeypatch.delenv("PLATFORM_TENANT_ID")
    for role in (UserRole.range_ops, UserRole.admin):
        act_as(real_user(db_session, role, cluster["b"].id))
        assert client.get("/proxmox/cluster/status").status_code == 403, role
        assert client.post("/proxmox/vms/pve1/9001/stop").status_code == 404, role
    assert cluster["calls"] == []


def test_a_connection_of_another_tenant_cannot_be_tested(client, db_session, cluster):
    conn = HypervisorConnection(id=uuid.uuid4(), name="vc-a", hypervisor_type="vsphere", host="vc-a", port=443,
                                username="svc", tenant_id=cluster["a"].id)
    db_session.add(conn)
    db_session.flush()
    act_as(real_user(db_session, UserRole.range_ops, cluster["b"].id))
    assert client.post(f"/hypervisors/connections/{conn.id}/test").status_code == 404


def test_new_connections_verify_tls_unless_told_not_to(client, db_session, cluster):
    act_as(real_user(db_session, UserRole.range_ops, cluster["a"].id))
    r = client.post("/hypervisors/connections", json={"name": "vc", "hypervisor_type": "vsphere", "host": "vc.example",
                                                      "port": 443, "username": "svc", "password": "pw"})
    assert r.status_code == 201, r.text
    assert r.json()["verify_ssl"] is True
    row = HypervisorConnection(name="x", hypervisor_type="vsphere", host="h", port=443, username="u",
                               tenant_id=cluster["a"].id)
    db_session.add(row)
    db_session.flush()
    assert row.verify_ssl is True
