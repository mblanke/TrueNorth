"""Worker side of lab sessions: leased networks reach the hypervisor, leftovers are found
by range id and removed, nothing else is touched."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("celery")

from worker.provisioners.results import DestroyResult  # noqa: E402
from worker.render import render_topology  # noqa: E402

TEMPLATE = {
    "name": "lab",
    "network": {"vlans": [{"name": "lab", "cidr": "10.20.0.0/24", "port_group": "pg-lab-07"}]},
    "nodes": [{"id": "gateway", "os": "ubuntu-lts", "vlan": "lab", "specs": {"cores": 2, "memory_mb": 2048}}],
}


def test_the_leased_port_group_reaches_every_vm_definition():
    out = render_topology(TEMPLATE, "11111111-2222-3333-4444-555555555555", lambda alias: "ubuntu-2404")
    assert out["vm_definitions"][0]["port_group"] == "pg-lab-07"
    assert out["network_definitions"][0]["port_group"] == "pg-lab-07"


def test_a_range_without_a_lease_keeps_the_default_network():
    plain = {**TEMPLATE, "network": {"vlans": [{"name": "lab", "cidr": "10.20.0.0/24"}]}}
    out = render_topology(plain, "r", lambda alias: "ubuntu-2404")
    assert out["vm_definitions"][0]["port_group"] == ""


def test_vsphere_maps_every_ovf_network_onto_the_leased_port_group(monkeypatch):
    from worker.provisioners.vsphere_api import VsphereAPIProvisioner

    prov = VsphereAPIProvisioner()
    seen = {}

    async def item(client, name):
        return "lib-item-1"

    async def network(client, dc, name=None):
        seen["network"] = name
        return "dvportgroup-77"

    async def ovf(client, item_id, rp):
        return ["VM Network", "Lab Net"]

    async def deploy(client, item_id, name, folder, rp, ds, mappings=None):
        seen["mappings"] = mappings
        return "vm-501"

    async def noop(*a, **k):
        return None

    async def tools(*a, **k):
        return True

    async def ip(*a, **k):
        return "10.20.0.10"

    monkeypatch.setattr(prov, "_find_library_item", item)
    monkeypatch.setattr(prov, "_find_network", network)
    monkeypatch.setattr(prov, "_ovf_networks", ovf)
    monkeypatch.setattr(prov, "_deploy_ovf", deploy)
    monkeypatch.setattr(prov, "_api_patch", noop)
    monkeypatch.setattr(prov, "_power_action", noop)
    monkeypatch.setattr(prov, "_wait_tools", tools)
    monkeypatch.setattr(prov, "_get_vm_ip", ip)
    vm = {"name": "r-gateway", "template_name": "ubuntu-2404", "port_group": "pg-lab-07", "cores": 2, "memory": 2048}
    out = asyncio.run(prov._provision_one_vm(None, vm, "rid-r-gateway", "ubuntu-2404", "f", "rp", "ds", "dc"))
    assert seen == {"network": "pg-lab-07", "mappings": {"VM Network": "dvportgroup-77", "Lab Net": "dvportgroup-77"}}
    assert out["vm_id"] == "vm-501" and out["tools_ready"] is True


def test_reconcile_removes_only_vms_named_for_the_given_ranges(monkeypatch):
    from worker import lab_tasks

    removed = {}

    class Prov:
        async def find_vms(self, prefix):
            inventory = [
                {"vm_id": "vm-1", "name": "aaaa-gateway"},
                {"vm_id": "vm-2", "name": "aaaa-client"},
                {"vm_id": "vm-9", "name": "someone-elses-vm"},
            ]
            return [v for v in inventory if v["name"].startswith(prefix)]

        async def destroy(self, range_id, output):
            removed[range_id] = [v["vm_id"] for v in output["vms"]]
            return DestroyResult(status="ok", resources_removed=len(output["vms"]))

    monkeypatch.setattr(lab_tasks, "_get_backend", lambda backend: Prov())
    result = lab_tasks.reconcile_lab_vms.run(["aaaa", "bbbb"], "vsphere_api")
    assert removed == {"aaaa": ["vm-1", "vm-2"]}
    assert result == {"status": "ok", "removed": {"aaaa": 2}}
