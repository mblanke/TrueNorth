"""scripts/register_vcenter_inprocess.py against the real /hypervisors router.

The contract tests (tests/contracts/test_installer_register_vcenter.py) use a fake API;
this runs ``register()`` through the API's own routes, schemas and guards, with vCenter
itself stubbed (no network): the payload is accepted, a re-run neither duplicates nor
changes anything, drift is corrected, the password is sealed and kept unless asked.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from _shared import act_as, real_tenant, real_user
from app.hypervisor_backends.vsphere import VSphereBackend
from app.models import HypervisorConnection, UserRole
from app.schemas import HypervisorTestResult
from app.secretbox import unseal

ROOT = Path(__file__).resolve().parents[2]
ARGS = dict(
    name="Range vCenter", host="vc.work.lab", port=443, username="svc-truenorth@vsphere.local",
    datacenter="DC-Work", verify_ssl=False, password="first-pw",
)


def _script():
    spec = importlib.util.spec_from_file_location("register_vcenter_inprocess", ROOT / "scripts/register_vcenter_inprocess.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Api:
    def __init__(self, client):
        self.client = client

    def _call(self, method, path, json=None):
        r = self.client.request(method, path, json=json)
        return r.status_code, (r.json() if r.content else None)

    def get(self, path, **kw):
        return self._call("GET", path)

    def post(self, path, json=None, **kw):
        return self._call("POST", path, json)

    def patch(self, path, json=None, **kw):
        return self._call("PATCH", path, json)


@pytest.fixture
def api(client, db_session, monkeypatch):
    tenant = real_tenant(db_session, "vc-install")
    act_as(real_user(db_session, UserRole.admin, tenant.id))
    monkeypatch.setattr(VSphereBackend, "check_connection", lambda self, conn, db: HypervisorTestResult(
        success=True, message="Connected to vCenter 8.0", version="8.0", nodes_found=2))
    monkeypatch.setattr(VSphereBackend, "discover", lambda self, cid, conn, db: {
        "message": "Discovered 2 new hosts (2 total)", "nodes_discovered": 2})
    return _Api(client), tenant


def _rows(db_session, tenant):
    return db_session.query(HypervisorConnection).filter(HypervisorConnection.tenant_id == tenant.id).all()


def test_registers_once_and_reconciles(api, db_session, capsys):
    mod = _script()
    fake, tenant = api
    assert mod.register(fake, **ARGS) == 0
    (row,) = _rows(db_session, tenant)
    assert (row.hypervisor_type, row.host, row.port, row.datacenter) == ("vsphere", "vc.work.lab", 443, "DC-Work")
    assert row.verify_ssl is False and row.is_primary is True
    assert row.password_encrypted != "first-pw" and unseal(row.password_encrypted) == "first-pw"
    out = capsys.readouterr().out
    assert "created 'Range vCenter'" in out and "vcenter test: ok (2 hosts" in out and "vcenter discover: ok" in out

    assert mod.register(fake, **{**ARGS, "password": "vault-rotated"}) == 0  # re-run
    (row,) = _rows(db_session, tenant)
    assert unseal(row.password_encrypted) == "first-pw"  # an operator's password is kept
    assert "unchanged 'Range vCenter'" in capsys.readouterr().out

    assert mod.register(fake, **{**ARGS, "datacenter": "DC-New", "verify_ssl": True, "password": "vault-rotated"},
                        update_password=True) == 0
    db_session.expire_all()
    (row,) = _rows(db_session, tenant)
    assert row.datacenter == "DC-New" and row.verify_ssl is True
    assert unseal(row.password_encrypted) == "vault-rotated"
    out = capsys.readouterr().out
    assert "updated 'Range vCenter' (datacenter, password, verify_ssl)" in out
    assert "vault-rotated" not in out and "first-pw" not in out
