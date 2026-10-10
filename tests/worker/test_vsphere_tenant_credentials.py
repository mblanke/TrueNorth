"""vsphere_api logs in to the range's own tenant's vCenter, for every operation (H6).

v1.0.0 known limitation H6: the worker resolved the range's tenant's HypervisorConnection
(db_ops.hypervisor_creds, #112) but vsphere_api was built with no credentials and always
used the worker's VSPHERE_* environment, so an install had one vCenter. Now
base_tasks._get_backend(backend, range_id) hands the tenant's connection to the
provisioner for build, destroy, power, snapshots and health, and provision() also takes
template["credentials"]. The environment stays the fallback for a range whose tenant has
no connection (and no shared one).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import sqlalchemy as sa
import test_vsphere_provision as _vsp
from test_range_task_fencing import OUTPUT, _range, _state
from test_vsphere_provision import RANGE_ID, _prov, _rendered, _run
from worker import base_tasks, power_tasks, secretbox, tasks
from worker.provisioners import vsphere_api as mod
from worker.provisioners.results import DestroyResult, HealthResult, StopResult
from worker.tables import hypervisor_connections, ranges

vc, worker_reserves = _vsp.vc, _vsp.worker_reserves  # the vSphere fakes' fixtures
NOW = datetime(2026, 10, 9, tzinfo=UTC)
TENANT_A, TENANT_B, TENANT_C = (str(uuid.uuid4()) for _ in range(3))


@pytest.fixture
def conns(monkeypatch):
    """Tenants A and B each with their own vCenter; tenant C with none. The worker's
    _db_session is pointed at this database."""
    eng = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool, connect_args={"check_same_thread": False})
    meta = sa.MetaData()
    for table in (hypervisor_connections, ranges):
        cols = [sa.Column(c.name, c.type, primary_key=c.primary_key) for c in table.columns]  # NOT NULL relaxed
        sa.Table(table.name, meta, *cols)
    meta.create_all(eng)
    with eng.begin() as db:
        for tenant, host, user, pw in ((TENANT_A, "vc-a.test", "svc-a", "pw-a"), (TENANT_B, "vc-b.test", "svc-b", "pw-b")):
            db.execute(hypervisor_connections.insert().values(
                id=str(uuid.uuid4()), name=host, hypervisor_type="vsphere", host=host, port=443, username=user,
                password_encrypted=secretbox.seal(pw), api_token=None, verify_ssl=False, is_primary=True,
                is_active=True, datacenter=f"DC-{user}", tenant_id=tenant, created_at=NOW, updated_at=NOW))
        ids = {}
        for tenant in (TENANT_A, TENANT_B, TENANT_C):
            ids[tenant] = str(uuid.uuid4())
            db.execute(ranges.insert().values(id=ids[tenant], tenant_id=tenant, name="r"))

    @contextmanager
    def session():
        with eng.begin() as db:
            yield db

    monkeypatch.setattr(tasks, "_db_session", session)
    return ids


def test_each_tenants_range_gets_its_own_vcenter(conns):
    a = base_tasks._get_backend("vsphere_api", conns[TENANT_A])
    b = base_tasks._get_backend("vsphere_api", conns[TENANT_B])
    assert (a._base_url, a._username, a._password, a._datacenter) == ("https://vc-a.test", "svc-a", "pw-a", "DC-svc-a")
    assert (b._base_url, b._username, b._password, b._datacenter) == ("https://vc-b.test", "svc-b", "pw-b", "DC-svc-b")


def test_a_tenant_without_a_connection_falls_back_to_the_environment(conns, monkeypatch):
    monkeypatch.setattr(mod, "VSPHERE_URL", "https://vc-env.test")
    monkeypatch.setattr(mod, "VSPHERE_USERNAME", "svc-env")
    c = base_tasks._get_backend("vsphere_api", conns[TENANT_C])
    assert (c._base_url, c._username) == ("https://vc-env.test", "svc-env")
    shared = base_tasks._get_backend("vsphere_api")  # no range at all (a lab reconcile)
    assert shared._base_url == "https://vc-env.test"


def test_a_backend_without_credentials_never_reads_the_connection(monkeypatch):
    def boom():
        raise AssertionError("mock must not look up a hypervisor connection")

    monkeypatch.setattr(tasks, "_db_session", boom)
    base_tasks._get_backend("mock", str(uuid.uuid4()))


def test_provision_logs_in_to_the_vcenter_in_template_credentials(vc):
    """The two tenants' builds reach two vCenters: REST login and pyVmomi alike."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("authorization", "")))
        return vc.handler(request)

    for host, user in (("vc-a.test", "svc-a"), ("vc-b.test", "svc-b")):
        seen.clear()
        mod.SmartConnect.reset_mock()
        prov = _prov(vc)
        prov._transport = httpx.MockTransport(handler)
        template = {**_rendered(), "credentials": {"host": host, "username": user, "password": "pw",
                                                   "datacenter": "DC-Lab", "verify_ssl": False}}
        result = _run(prov.provision(RANGE_ID, template, {}))
        assert result.status == "ok", result.errors
        assert {h for h, _ in seen} == {host}
        assert {c.kwargs["host"] for c in mod.SmartConnect.call_args_list} == {host}
        assert {c.kwargs["user"] for c in mod.SmartConnect.call_args_list} == {user}
        _run(prov.destroy(RANGE_ID, {"vms": result.vms, "networks": result.networks}))


# -- every task hands the range id over, so the lookup above happens ---------
def test_destroy_uses_the_ranges_connection(db):
    rid = _range(db, "destroying", OUTPUT)
    backend = MagicMock(destroy=AsyncMock(return_value=DestroyResult(status="ok")))
    with patch.object(tasks, "_get_backend", return_value=backend) as get:
        assert tasks.destroy_range.run(rid)["status"] == "destroyed"
    get.assert_called_once_with("mock", rid)


def test_power_uses_the_ranges_connection(db):
    rid = _range(db, "stopping", OUTPUT)
    backend = MagicMock(stop=AsyncMock(return_value=StopResult(status="ok", vms_stopped=1)))
    with patch.object(tasks, "_get_backend", return_value=backend) as get:
        assert power_tasks.stop_range.run(rid)["status"] == "stopped"
    get.assert_called_once_with("mock", rid)
    assert _state(db, rid)[0] == "stopped"


def test_health_and_metrics_check_each_range_on_its_own_connection(monkeypatch):
    rids = [str(uuid.uuid4()), str(uuid.uuid4())]

    @contextmanager
    def session():
        yield None

    monkeypatch.setattr(tasks, "_db_session", session)
    monkeypatch.setattr(tasks.db_ops, "active_ranges", lambda db: [(r, "n", OUTPUT) for r in rids])
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    backend = MagicMock(health_check=AsyncMock(return_value=HealthResult()))
    with patch.object(tasks, "_get_backend", return_value=backend) as get:
        tasks.health_check_ranges.run()
    assert [c.args for c in get.call_args_list] == [(None, r) for r in rids]
