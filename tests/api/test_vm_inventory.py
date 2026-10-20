"""The Infrastructure page's VM list: a vCenter's VMs with TrueNorth's tn-* tags, grouped.

``GET /hypervisors/connections/{id}/vms`` reads live through the adapter
(hypervisor_backends/vsphere_inventory.py), in both REST dialects, against a fake vCenter
whose tagging service is the one the worker's tests use (tests/worker/vsphere_cis_fake.py).
Pinned here: tags decide the group (golden templates, host copies, range VMs, other); a
tagged template that the VM listing leaves out is still shown; an untagged VM is Other; a
tagging failure is a note, not a failed page; the hosts' VM counts are not touched; the
route is tenant-scoped like the rest of /hypervisors and needs infra:write.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import uuid
from pathlib import Path

import httpx
import pytest
import respx
from _shared import act_as, real_tenant, real_user
from app import vm_inventory
from app.hypervisor_backends import get_hypervisor_backend
from app.models import HypervisorConnection, HypervisorNode, UserRole
from app.secretbox import seal

_spec = importlib.util.spec_from_file_location(
    "vsphere_cis_fake", Path(__file__).resolve().parents[1] / "worker/vsphere_cis_fake.py")
_fake = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("vsphere_cis_fake", _fake)
_spec.loader.exec_module(_fake)
FakeCis = _fake.FakeCis

VC = "https://vcsa.inv.test"


class FakeVcenter:
    """Hosts, VMs per host (inventory templates left out of the list, as a vCenter may) and tags."""

    def __init__(self, dialect: str = "api"):
        self.dialect = dialect
        self.cis = FakeCis()
        self.hosts = [{"host": "host-1", "name": "esx02.truenorth.lab", "connection_state": "CONNECTED"},
                      {"host": "host-2", "name": "esx03.truenorth.lab", "connection_state": "CONNECTED"}]
        self.on_host = {
            "host-1": [{"vm": "vm-10", "name": "2fc8d04d-ubu01", "power_state": "POWERED_ON"},
                       {"vm": "vm-11", "name": "WIN11-TMPL", "power_state": "POWERED_OFF"}],
            "host-2": [{"vm": "vm-12", "name": "TN-DEPOT01", "power_state": "POWERED_ON"}],
        }
        self.templates = {"vm-1": "ubuntu-lts", "vm-2": "ubuntu-lts@esx02-20261020"}  # not in any listing
        self.calls: list[str] = []

    def tag(self, vm: str, **tags: str) -> None:
        for cat, name in tags.items():
            cat = cat.replace("_", "-")
            tid = next((t for t, v in self.cis.tags.items() if v["name"] == name
                        and self.cis.categories[v["category_id"]]["name"] == cat), None) or self.cis.add_tag(cat, name)
            self.cis.attached.setdefault(vm, set()).add(tid)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        legacy = self.dialect == "rest"
        wrap = (lambda v: {"value": v}) if legacy else (lambda v: v)
        if path == "/api/session" and request.method == "POST":
            return httpx.Response(404) if legacy else httpx.Response(201, json="tok")
        if path == "/rest/com/vmware/cis/session" and request.method == "POST":
            return httpx.Response(200, json={"value": "tok"})
        if path.endswith("/session") and request.method == "DELETE":
            return httpx.Response(204)
        assert request.headers.get("vmware-api-session-id") == "tok"
        if self.cis.handles(path):
            return self.cis(request)
        prefix = "/rest" if legacy else "/api"
        if path == f"{prefix}/vcenter/host":
            return httpx.Response(200, json=wrap(self.hosts))
        if path == f"{prefix}/vcenter/vm":
            host = request.url.params.get("filter.hosts" if legacy else "hosts")
            return httpx.Response(200, json=wrap(self.on_host.get(host, [])))
        if m := re.fullmatch(rf"{prefix}/vcenter/vm/([^/]+)", path):
            if m.group(1) in self.templates:
                return httpx.Response(200, json=wrap({"name": self.templates[m.group(1)], "power_state": "POWERED_OFF"}))
            return httpx.Response(404, json={"error_type": "NOT_FOUND"})
        return httpx.Response(404, json={"error_type": "NOT_FOUND", "path": path})


def _conn(db, tenant_id=None, kind="vsphere") -> HypervisorConnection:
    tenant_id = tenant_id or real_tenant(db, f"inv-{uuid.uuid4().hex[:6]}").id
    conn = HypervisorConnection(id=uuid.uuid4(), tenant_id=tenant_id, name="vc", hypervisor_type=kind,
                                host="vcsa.inv.test", port=443, username="svc-truenorth@vsphere.local",
                                password_encrypted=seal("row-secret"))
    db.add(conn)
    db.flush()
    return conn


def _seed(vc: FakeVcenter) -> None:
    vc.tag("vm-1", tn_type="golden-template", tn_template="ubuntu-lts", tn_os="ubuntu-2404")
    vc.tag("vm-2", tn_type="template-copy", tn_template="ubuntu-lts", tn_os="ubuntu-2404")
    vc.tag("vm-10", tn_type="range-vm", tn_range="ubuntu-lab-2fc8d04d", tn_os="ubuntu-2404")
    vc.tag("vm-12", tn_type="depot")
    vc.cis.add_tag("owner", "someone")  # not a TrueNorth category: never read as one


@pytest.mark.parametrize("dialect", ["api", "rest"])
def test_vms_are_grouped_by_their_tn_type_tag(db_session, dialect):
    vc = FakeVcenter(dialect)
    _seed(vc)
    conn = _conn(db_session)
    with respx.mock(base_url=VC) as mock:
        mock.route().mock(side_effect=vc)
        out = vm_inventory.inventory(get_hypervisor_backend("vsphere"), conn)
    assert out.supported and out.groups == {"golden": 1, "copies": 1, "ranges": 1, "other": 2}
    by = {v.name: v for v in out.vms}
    assert by["ubuntu-lts"].group == "golden" and by["ubuntu-lts"].tags == {
        "tn-type": "golden-template", "tn-template": "ubuntu-lts", "tn-os": "ubuntu-2404"}
    assert by["ubuntu-lts@esx02-20261020"].group == "copies"
    assert by["2fc8d04d-ubu01"].group == "ranges" and by["2fc8d04d-ubu01"].host == "esx02.truenorth.lab"
    assert by["2fc8d04d-ubu01"].tags["tn-range"] == "ubuntu-lab-2fc8d04d"
    assert by["TN-DEPOT01"].group == "other" and by["TN-DEPOT01"].tags == {"tn-type": "depot"}
    assert by["WIN11-TMPL"].group == "other" and by["WIN11-TMPL"].tags == {}  # not TrueNorth's: never guessed
    assert any(c.endswith("/session") and c.startswith("DELETE") for c in vc.calls)  # logged out


def test_a_vcenter_with_no_tags_yet_lists_everything_as_other(db_session):
    vc = FakeVcenter()
    with respx.mock(base_url=VC) as mock:
        mock.route().mock(side_effect=vc)
        out = vm_inventory.inventory(get_hypervisor_backend("vsphere"), _conn(db_session))
    assert out.groups == {"golden": 0, "copies": 0, "ranges": 0, "other": 3} and out.message == "3 VMs"


def test_tags_that_cannot_be_read_are_a_note_not_a_failure(db_session):
    vc = FakeVcenter()
    _seed(vc)
    vc.cis.fail = "*"
    with respx.mock(base_url=VC) as mock:
        mock.route().mock(side_effect=vc)
        out = vm_inventory.inventory(get_hypervisor_backend("vsphere"), _conn(db_session))
    assert out.supported and len(out.vms) == 3 and out.groups["other"] == 3
    assert "tags not read" in out.message


def test_an_unreachable_vcenter_is_reported(db_session):
    with respx.mock(base_url=VC) as mock:
        mock.route().mock(side_effect=httpx.ConnectError("refused"))
        out = vm_inventory.inventory(get_hypervisor_backend("vsphere"), _conn(db_session))
    assert out.supported and out.vms == [] and out.message.startswith("VM inventory failed")
    assert "row-secret" not in out.message


def test_listing_leaves_host_vm_counts_alone(db_session):
    vc = FakeVcenter()
    _seed(vc)
    conn = _conn(db_session)
    db_session.add(HypervisorNode(connection_id=str(conn.id), node_name="esx02.truenorth.lab", status="online",
                                  vm_count=7))
    db_session.flush()
    with respx.mock(base_url=VC) as mock:
        mock.route().mock(side_effect=vc)
        vm_inventory.inventory(get_hypervisor_backend("vsphere"), conn)
    assert db_session.query(HypervisorNode).filter_by(connection_id=str(conn.id)).one().vm_count == 7


# -- the route -------------------------------------------------------------------------------

def test_route_is_tenant_scoped_and_needs_infra_write(client, db_session, monkeypatch):
    a, b = real_tenant(db_session, "inv-a"), real_tenant(db_session, "inv-b")
    conn = _conn(db_session, a.id)
    seen = []
    monkeypatch.setattr(type(get_hypervisor_backend("vsphere")), "list_vms",
                        lambda self, c: seen.append(c.id) or ([{"vm_id": "vm-1", "name": "ubuntu-lts", "power_state": None,
                                                                "host": None, "tags": {"tn-type": "golden-template"}}], ""))
    act_as(real_user(db_session, UserRole.range_ops, b.id))
    assert client.get(f"/hypervisors/connections/{conn.id}/vms").status_code == 404
    act_as(real_user(db_session, UserRole.student, a.id))
    assert client.get(f"/hypervisors/connections/{conn.id}/vms").status_code == 403
    assert seen == []
    act_as(real_user(db_session, UserRole.range_ops, a.id))
    r = client.get(f"/hypervisors/connections/{conn.id}/vms")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["groups"] == {"golden": 1, "copies": 0, "ranges": 0, "other": 0}
    assert body["vms"][0] == {"vm_id": "vm-1", "name": "ubuntu-lts", "power_state": None, "host": None,
                              "group": "golden", "tags": {"tn-type": "golden-template"}}
    assert json.dumps(body).find("row-secret") == -1


def test_route_says_unsupported_for_other_hypervisors(client, db_session):
    a = real_tenant(db_session, "inv-c")
    conn = _conn(db_session, a.id, kind="proxmox")
    act_as(real_user(db_session, UserRole.range_ops, a.id))
    r = client.get(f"/hypervisors/connections/{conn.id}/vms")
    assert r.status_code == 200 and r.json()["supported"] is False and r.json()["vms"] == []
