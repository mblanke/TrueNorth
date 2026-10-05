"""vSphere: the background-noise management NIC on agent VMs.

A fake vCenter stands in for the REST API and pyVmomi; what is checked is what the
provisioner asks vCenter to do, and the cloud-init network config it hands the guest.
"""

import asyncio
import base64
import json

from worker.provisioners.vsphere_api import VsphereAPIProvisioner, noise_guestinfo

TRAIN_MAC, MGMT_MAC = "00:50:56:aa:00:01", "00:50:56:bb:00:02"
MGMT = {"vlan_id": 4001, "ip": "10.255.0.14", "prefix": 24}


def _decode(options: dict) -> dict:
    assert options["guestinfo.metadata.encoding"] == "base64"
    return json.loads(base64.b64decode(options["guestinfo.metadata"]))


class FakeVcenter(VsphereAPIProvisioner):
    def __init__(self, *, portgroup=True):
        super().__init__()
        self.portgroup = portgroup
        self.nics_added: list[tuple[str, dict]] = []
        self.extra_config: dict[str, dict] = {}
        self.vm_seq = 0
        self.nics: dict[str, list[str]] = {}

    async def _get_session(self):
        return "session"

    async def _api_get(self, client, path):
        if path.startswith("/vcenter/datacenter"):
            return [{"datacenter": "dc-1"}]
        if path.startswith("/vcenter/cluster"):
            return [{"cluster": "c-1"}]
        if path.startswith("/vcenter/datastore"):
            return [{"datastore": "ds-1"}]
        if path.startswith("/vcenter/resource-pool"):
            return [{"resource_pool": "rp-1"}]
        if path.startswith("/vcenter/folder"):
            return [{"folder": "f-1"}]
        if path.startswith("/vcenter/network"):
            return [{"network": "dvportgroup-77", "type": "DISTRIBUTED_PORTGROUP"}] if self.portgroup else []
        if path.endswith("/guest/networking/interfaces"):
            # Tools reports the management address first; it must not be taken as the VM's IP.
            return [
                {"ip": {"ip_addresses": [{"ip_address": MGMT["ip"], "state": "PREFERRED"}]}},
                {"ip": {"ip_addresses": [{"ip_address": "10.10.0.50", "state": "PREFERRED"}]}},
            ]
        if "/hardware/ethernet/" in path:
            nic = path.rsplit("/", 1)[1]
            return {"mac_address": TRAIN_MAC if nic == "4000" else MGMT_MAC}
        if path.endswith("/hardware/ethernet"):
            vm = path.split("/")[3]
            return [{"nic": n} for n in self.nics.setdefault(vm, ["4000"])]
        raise AssertionError(f"unexpected GET {path}")

    async def _api_post(self, client, path, **kwargs):
        if path.endswith("/hardware/ethernet"):
            vm = path.split("/")[3]
            self.nics_added.append((vm, kwargs["json"]))
            self.nics.setdefault(vm, ["4000"]).append("4001")
            return "4001"
        return {}

    async def _api_patch(self, client, path, **kwargs):
        return None

    async def _find_library_item(self, client, template_name):
        return "lib-item"

    async def _deploy_ovf(
        self, client, library_item_id, name, folder_id, resource_pool_id, datastore_id, network_mappings=None
    ):
        self.vm_seq += 1
        return f"vm-{self.vm_seq}"

    async def _power_action(self, client, vm_id, action):
        return None

    async def _wait_tools(self, client, vm_id):
        return True

    def _set_extra_config_sync(self, vm_id, options):
        self.extra_config[vm_id] = options


def _template(*vms):
    return {"vms": list(vms), "networks": []}


LINUX = {
    "name": "abcdef01-lnx01",
    "os": "ubuntu-2404",
    "template_name": "ubuntu-2404",
    "ip": "10.10.0.50",
    "gateway": "10.10.0.1",
    "prefix": 24,
    "mgmt": MGMT,
}
WINDOWS = {"name": "abcdef01-ws01", "os": "windows-11", "template_name": "win11", "ip": "10.10.0.10", "mgmt": MGMT}
PLAIN = {"name": "abcdef01-dc01", "os": "windows-server-2022", "template_name": "ws2022", "ip": "10.30.0.10"}


def _run(prov, *vms):
    return asyncio.run(prov.provision("abcdef0123456789", _template(*vms), {}))


def test_linux_agent_vm_gets_the_mgmt_nic_and_a_static_address():
    prov = FakeVcenter()
    result = _run(prov, LINUX)
    assert result.status == "ok", result.errors
    ((vm, spec),) = prov.nics_added
    assert spec["type"] == "VMXNET3" and spec["start_connected"] is True and spec["allow_guest_control"] is False
    assert spec["backing"] == {"type": "DISTRIBUTED_PORTGROUP", "network": "dvportgroup-77"}
    meta = _decode(prov.extra_config[vm])
    eth = meta["network"]["ethernets"]
    assert eth["noise0"] == {"match": {"macaddress": MGMT_MAC}, "dhcp4": False, "addresses": ["10.255.0.14/24"]}
    assert "routes" not in eth["noise0"]  # on-link to the controller, nowhere else
    assert eth["train0"]["match"] == {"macaddress": TRAIN_MAC}
    assert eth["train0"]["addresses"] == ["10.10.0.50/24"]
    assert meta["local-hostname"] == "lnx01"
    (out,) = result.vms
    assert out["ip"] == "10.10.0.50" and out["mgmt_ip"] == MGMT["ip"]


def test_windows_and_non_agent_vms_are_left_alone():
    prov = FakeVcenter()
    result = _run(prov, WINDOWS, PLAIN)
    assert result.status == "ok" and prov.nics_added == [] and prov.extra_config == {}
    assert all("mgmt_ip" not in v for v in result.vms)


def test_missing_portgroup_still_builds_the_range_but_says_so():
    prov = FakeVcenter(portgroup=False)
    result = _run(prov, LINUX, PLAIN)
    assert len(result.vms) == 2 and prov.nics_added == []
    assert any("TN-Noise-Mgmt" in e and "unreachable" in e for e in result.errors)


def test_training_nic_falls_back_to_dhcp_without_an_address():
    meta = _decode(noise_guestinfo({**LINUX, "ip": "", "gateway": ""}, "i-1", TRAIN_MAC, MGMT_MAC))
    assert meta["network"]["ethernets"]["train0"] == {"match": {"macaddress": TRAIN_MAC}, "dhcp4": True}


def test_render_output_feeds_the_provisioner():
    """End to end on the worker side: template -> render -> vSphere NIC."""
    import copy
    from pathlib import Path

    import yaml
    from worker.render import render_topology

    rvb = yaml.safe_load((Path(__file__).resolve().parents[2] / "content/ranges/red-vs-blue/template.yaml").read_text())
    t = copy.deepcopy(rvb)
    t["noise"] = {"enabled": True}
    # The API reserves these when it accepts the provision (app/noise/mgmt.py).
    reserved = {
        h: f"10.255.0.{40 + i}" for i, h in enumerate(("lnx01", "lnx02", "tgen01", "ws01", "ws02", "ws03", "ws04"))
    }
    rendered = render_topology(t, "abcdef0123456789", lambda alias: alias, noise_mgmt=reserved)
    agents = [v for v in rendered["vm_definitions"] if "mgmt" in v]
    prov = FakeVcenter()
    result = _run(prov, *agents)
    linux = [v for v in agents if not v["os"].startswith("win")]
    assert len(prov.nics_added) == len(linux) > 0
    assert sorted(v["mgmt_ip"] for v in result.vms if "mgmt_ip" in v) == sorted(v["mgmt"]["ip"] for v in linux)
    # Static training address, so the guest is told the range's resolvers (its DCs).
    meta = _decode(next(iter(prov.extra_config.values())))
    assert meta["network"]["ethernets"]["train0"]["nameservers"] == {
        "addresses": ["10.30.0.10", "10.30.0.11"],
        "search": ["corp.local"],
    }
