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

    monkeypatch.setenv("LAB_PORT_GROUPS", "pg-lab-06,pg-lab-07")
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


def test_a_port_group_outside_the_lab_pool_is_refused(monkeypatch):
    from worker.provisioners.vsphere_api import VsphereAPIProvisioner

    monkeypatch.setenv("LAB_PORT_GROUPS", "pg-lab-01")
    prov = VsphereAPIProvisioner()

    async def item(client, name):
        return "lib-item-1"

    monkeypatch.setattr(prov, "_find_library_item", item)
    vm = {"name": "r-x", "template_name": "t", "port_group": "Management Network"}
    with pytest.raises(RuntimeError, match="not a lab network"):
        asyncio.run(prov._provision_one_vm(None, vm, "rid-r-x", "t", "f", "rp", "ds", "dc"))


def test_vms_built_for_a_range_torn_down_mid_build_are_destroyed(monkeypatch):
    from types import SimpleNamespace

    from worker import tasks

    built = SimpleNamespace(status="ok", vms=[{"vm_id": "vm-9", "name": "r-host"}], networks=[], errors=[])
    destroyed = {}

    class Prov:
        async def provision(self, range_id, template, allocations):
            return built

        async def destroy(self, range_id, output):
            destroyed[range_id] = output["vms"]
            return DestroyResult(status="ok", resources_removed=1)

    calls = []

    def state(range_id, new_state, **kw):
        calls.append((new_state, kw.get("only_from")))
        return 0 if new_state == "ready" else 1  # the range left `provisioning` meanwhile

    class Db:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            return SimpleNamespace(first=lambda: ("{}", "mock"))

    monkeypatch.setattr(tasks, "_update_range_state", state)
    monkeypatch.setattr(tasks, "_notify_api", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "_db_session", lambda: Db())
    monkeypatch.setattr(tasks, "_get_backend", lambda b: Prov())
    result = tasks.provision_range.run("r-1")
    assert result == {"status": "discarded", "range_id": "r-1", "vm_count": 1}
    assert destroyed == {"r-1": [{"vm_id": "vm-9", "name": "r-host"}]}
    assert ("ready", ("provisioning",)) in calls


def test_destroying_a_destroyed_range_is_a_no_op(monkeypatch):
    from worker import tasks

    monkeypatch.setattr(tasks, "_update_range_state", lambda *a, **k: 0)
    monkeypatch.setattr(tasks, "_get_backend", lambda b: pytest.fail("must not touch the hypervisor"))
    assert tasks.destroy_range.run("r-1") == {"status": "skipped", "range_id": "r-1"}


def test_a_vm_already_gone_counts_as_deleted(monkeypatch):
    import httpx
    from worker.provisioners.vsphere_api import VsphereAPIProvisioner

    prov = VsphereAPIProvisioner()

    async def power(client, path):
        return {"state": "POWERED_OFF"}

    async def delete(client, path):
        request = httpx.Request("DELETE", "https://vc/api" + path)
        raise httpx.HTTPStatusError("gone", request=request, response=httpx.Response(404, request=request))

    monkeypatch.setattr(prov, "_api_get", power)
    monkeypatch.setattr(prov, "_api_delete", delete)
    asyncio.run(prov._delete_vm(None, "vm-1"))  # no exception
