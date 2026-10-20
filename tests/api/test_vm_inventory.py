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
from app.models import HypervisorConnection, HypervisorNode, Range, Template, UserRole
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
    vc.tag("vm-10", tn_type="range-vm", tn_range="ubuntu-lab-2fc8d04d1111", tn_os="ubuntu-2404")
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
    assert by["2fc8d04d-ubu01"].tags["tn-range"] == "ubuntu-lab-2fc8d04d1111"
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


# -- tenancy, paging, batching ---------------------------------------------------------------

def _range(db, tenant_id, rid: str) -> None:
    t = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=tenant_id)
    db.add(t)
    db.flush()
    db.add(Range(id=uuid.UUID(rid), name="r", template_id=t.id, tenant_id=tenant_id))
    db.flush()


MINE, THEIRS = "2fc8d04d-1111-4222-8333-444455556666", "2fc8d04d-9999-4999-8999-999999999999"  # same id8


def _rows():
    return [
        {"vm_id": "vm-1", "name": "ubuntu-lts", "tags": {"tn-type": "golden-template"}},
        {"vm_id": "vm-2", "name": "mine-ubu01", "tags": {"tn-type": "range-vm", "tn-range": "lab-2fc8d04d1111"}},
        {"vm_id": "vm-3", "name": "theirs-ubu01", "tags": {"tn-type": "range-vm", "tn-range": "x-2fc8d04d9999"}},
        {"vm_id": "vm-4", "name": "untagged-range", "tags": {"tn-type": "range-vm"}},
        {"vm_id": "vm-5", "name": "WIN11-TMPL", "tags": {}},
    ]


@pytest.fixture
def two_tenants(client, db_session, monkeypatch):
    a, b, op = (real_tenant(db_session, s) for s in ("inv-ta", "inv-tb", "inv-op"))
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(op.id))
    _range(db_session, a.id, MINE)
    _range(db_session, b.id, THEIRS)
    monkeypatch.setattr(type(get_hypervisor_backend("vsphere")), "list_vms", lambda self, c: (_rows(), ""))
    return {"a": a, "b": b, "op": op}


def test_another_tenants_range_vms_are_hidden(client, db_session, two_tenants):
    conn = _conn(db_session, two_tenants["a"].id)
    act_as(real_user(db_session, UserRole.range_ops, two_tenants["a"].id))
    body = client.get(f"/hypervisors/connections/{conn.id}/vms").json()
    assert [v["name"] for v in body["vms"]] == ["ubuntu-lts", "mine-ubu01", "WIN11-TMPL"]
    assert body["groups"] == {"golden": 1, "copies": 0, "ranges": 1, "other": 1}  # counts see only what is shown
    assert "theirs" not in json.dumps(body) and "untagged-range" not in json.dumps(body)


def test_the_platform_admin_sees_every_range_vm(client, db_session, two_tenants):
    conn = _conn(db_session, two_tenants["op"].id)
    act_as(real_user(db_session, UserRole.admin, two_tenants["op"].id))
    body = client.get(f"/hypervisors/connections/{conn.id}/vms").json()
    assert len(body["vms"]) == 5
    act_as(real_user(db_session, UserRole.admin, two_tenants["a"].id))  # a tenant's admin is not the platform's
    conn_a = _conn(db_session, two_tenants["a"].id)
    assert len(client.get(f"/hypervisors/connections/{conn_a.id}/vms").json()["vms"]) == 3


def test_paging_and_the_group_filter(client, db_session, two_tenants):
    conn = _conn(db_session, two_tenants["op"].id)
    act_as(real_user(db_session, UserRole.admin, two_tenants["op"].id))
    page = client.get(f"/hypervisors/connections/{conn.id}/vms", params={"limit": 2, "offset": 2}).json()
    assert (page["total"], page["limit"], page["offset"]) == (5, 2, 2)
    assert [v["vm_id"] for v in page["vms"]] == ["vm-3", "vm-4"] and "showing 3-4 of 5" in page["message"]
    ranges = client.get(f"/hypervisors/connections/{conn.id}/vms", params={"group": "ranges"}).json()
    assert ranges["total"] == 3 and {v["group"] for v in ranges["vms"]} == {"ranges"}
    assert ranges["groups"]["golden"] == 1  # group counts stay the unfiltered ones
    for bad in ({"limit": 0}, {"limit": 5000}, {"offset": -1}, {"group": "nope"}):
        assert client.get(f"/hypervisors/connections/{conn.id}/vms", params=bad).status_code == 422, bad


@pytest.mark.parametrize("dialect", ["api", "rest"])
def test_tags_are_read_with_one_attached_objects_call_per_category(db_session, dialect):
    vc = FakeVcenter(dialect)
    _seed(vc)
    with respx.mock(base_url=VC) as mock:
        mock.route().mock(side_effect=vc)
        vm_inventory.inventory(get_hypervisor_backend("vsphere"), _conn(db_session))
    attached = [p for _, p in vc.cis.calls if "list-attached-objects-on-tags" in p]
    assert len(attached) == 4  # tn-type, tn-template, tn-os, tn-range; the "owner" category is never read


def test_too_many_tags_are_capped_and_said_so(db_session, monkeypatch):
    from app.hypervisor_backends import vsphere_inventory

    vc = FakeVcenter()
    _seed(vc)
    for i in range(5):  # five more ranges' tags: six in tn-range
        vc.cis.add_tag("tn-range", f"r{i}-00000000{i:04d}")
    monkeypatch.setattr(vsphere_inventory, "MAX_TAGS_PER_CATEGORY", 2)
    with respx.mock(base_url=VC) as mock:
        mock.route().mock(side_effect=vc)
        out = vm_inventory.inventory(get_hypervisor_backend("vsphere"), _conn(db_session))
    assert "tn-range: only the first 2 of 6 tags read" in out.message
