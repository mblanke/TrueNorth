"""Worker tasks around vSphere ranges: what a build records, task routing, DB credentials.

VLAN and uplink reservation: tests/worker/test_range_allocation.py. Stop/start:
tests/worker/test_range_power_tasks.py (worker) and tests/api/test_range_power.py (API).
These run on SQLite with the API's own schema.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

import pytest
import sqlalchemy as sa
from app import models as m
from app.sections import Base
from sqlalchemy.orm import Session, sessionmaker

tasks = pytest.importorskip("worker.tasks")
from worker.provisioners.results import ProvisionResult  # noqa: E402
from worker.provisioners.vsphere_api import VsphereAPIProvisioner  # noqa: E402


@pytest.fixture
def make(tmp_path, monkeypatch):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'tn.db'}")
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, class_=Session, expire_on_commit=False)

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(tasks, "_db_session", _session)
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    yield factory
    eng.dispose()


def test_a_build_records_its_uplink_reservation_and_what_it_skipped(make, monkeypatch):
    monkeypatch.setenv("VSPHERE_URL", "https://vc.lab.test")
    prov = VsphereAPIProvisioner()
    prov._vlan_pool, prov._uplink_network, prov._uplink_pool = "100-199", "dPG-TN-SVC", "10.30.32.100-102"
    seen: list[dict] = []

    async def provision(range_id, template, allocations):
        seen.append(allocations)
        return ProvisionResult(status="ok", vms=[{"name": "fw", "vm_id": "vm-1"}], networks=[],
                               warnings=["software 'x' skipped"],
                               uplink={"network": "dPG-TN-SVC", "ip": allocations["uplink_ip"]})

    prov.provision = provision
    monkeypatch.setattr(tasks, "_get_backend", lambda backend=None: prov)
    with make() as s:
        t = m.Tenant(name="t", slug="t")
        s.add(t)
        s.flush()
        tmpl = m.Template(name="lab", tenant_id=t.id, yaml=json.dumps({
            "name": "lab", "network": {"vlans": [{"id": 200, "name": "lan", "cidr": "10.1.0.0/24"}]},
            "nodes": [{"id": "fw", "os": "pfsense", "role": "firewall", "vlan": "lan"}],
        }))
        s.add(tmpl)
        s.flush()
        r = m.Range(name="r", tenant_id=t.id, template_id=tmpl.id, provisioner_backend="vsphere_api",
                    state=m.RangeState.provisioning)
        s.add(r)
        s.commit()
        rid = str(r.id)

    tasks.provision_range.run(rid)

    assert seen[0]["uplink_ip"] == "10.30.32.100"
    with make() as s:
        out = json.loads(s.get(m.Range, uuid.UUID(rid)).provisioner_output)
    assert out["uplink"]["ip"] == "10.30.32.100" and out["warnings"] == ["software 'x' skipped"]


def test_tasks_are_registered_and_routed():
    from worker.celery_app import app

    assert {"worker.tasks.stop_range", "worker.tasks.start_range"} <= set(app.tasks)
    assert app.conf.task_routes["worker.tasks.stop_range"] == {"queue": "provision"}
    assert app.conf.task_routes["worker.tasks.start_range"] == {"queue": "provision"}


def test_vsphere_backend_takes_db_credentials_without_vsphere_url(make, monkeypatch):
    monkeypatch.delenv("VSPHERE_URL", raising=False)
    with make() as s:
        s.add(m.HypervisorConnection(name="lab", hypervisor_type="vsphere", host="vcsa.truenorth.lab", port=443,
                                     username="svc@vsphere.local", password_encrypted="pw", is_primary=True,
                                     is_active=True, datacenter="DC-Lab"))
        s.commit()
    prov = tasks._get_backend("vsphere_api")
    assert prov._base_url == "https://vcsa.truenorth.lab" and prov._username == "svc@vsphere.local"
