"""vSphere range builds, end to end against fakes.

The vsphere_api provisioner had never built a VM: it looked libraries up with a GET
vCenter does not serve, sent an OVF body vCenter does not accept, left every VM on the
template's network, ignored the rendered IPs, and named VMs with the range id twice.
No vCenter is reachable from CI, so these tests are the safety net. REST goes through
an httpx.MockTransport that behaves like vCenter's /api; pyVmomi goes through fake
managed objects, while every spec the code builds is a real pyVmomi data object (they
reject unknown properties, so a misspelt field fails here, not on site).
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import yaml
from pyVmomi import vim
from worker import render, vlan_pool
from worker.provisioners import vsphere_api as mod
from worker.provisioners import vsphere_guest as guest_mod
from worker.provisioners import vsphere_infra as infra

RANGE_ID = "abcdef12-3456-7890-abcd-ef1234567890"
R8 = RANGE_ID[:8]
GIB = 1024**3
MGMT = "dPG-TN-MGMT"


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Fake vSphere inventory
# --------------------------------------------------------------------------- #

_ids = itertools.count(1)


class FakeTask:
    def __init__(self, result=None, fail: str | None = None, label: str = ""):
        self.info = SimpleNamespace(result=result)
        self.fail = fail
        self.label = label


class FakeDatastore(vim.Datastore):
    """A real vim.Datastore reference (specs type-check it) carrying fake properties."""

    name = summary = None

    def __init__(self, name: str, free_gb: int):
        super().__init__(f"datastore-{next(_ids)}", None)
        self.name = name
        self.summary = SimpleNamespace(name=name, freeSpace=free_gb * GIB, accessible=True, maintenanceMode="normal")


class FakeHost(vim.HostSystem):
    name = runtime = hardware = summary = vm = datastore = network = configManager = None  # noqa: N815

    def __init__(self, name: str, *, free_gb_ram: int = 500, threads: int = 16, vcpus_on: int = 0,
                 maintenance: bool = False, ds_free_gb: int = 1600):
        super().__init__(f"host-{next(_ids)}", None)
        self.name = name
        self.runtime = SimpleNamespace(connectionState="connected", inMaintenanceMode=maintenance,
                                       powerState="poweredOn")
        self.hardware = SimpleNamespace(memorySize=512 * GIB, cpuInfo=SimpleNamespace(numCpuThreads=threads))
        self.summary = SimpleNamespace(quickStats=SimpleNamespace(overallMemoryUsage=(512 - free_gb_ram) * 1024))
        self.vm = [SimpleNamespace(runtime=SimpleNamespace(powerState="poweredOn"),
                                   summary=SimpleNamespace(config=SimpleNamespace(numCpu=vcpus_on, template=False)))]
        self.datastore = [FakeDatastore(f"{name.split('.')[0]}-local", ds_free_gb)]
        self.network: list = []
        self.configManager = SimpleNamespace(networkSystem=MagicMock())

        def add(portgrp):
            if any(n.name == portgrp.name for n in self.network):
                raise vim.fault.AlreadyExists()
            self.network.append(SimpleNamespace(name=portgrp.name, _moId=f"network-{next(_ids)}", config=None))

        def remove(pgName):  # noqa: N803 -- pyVmomi's keyword
            if not any(n.name == pgName for n in self.network):
                raise vim.fault.NotFound()
            self.network = [n for n in self.network if n.name != pgName]

        self.configManager.networkSystem.AddPortGroup.side_effect = add
        self.configManager.networkSystem.RemovePortGroup.side_effect = remove


class FakeFolder:
    def __init__(self, name: str, parent=None):
        self.name = name
        self._moId = f"group-v{next(_ids)}"
        self.childEntity: list = []
        self.parent = parent

    def CreateFolder(self, name):  # noqa: N802 -- pyVmomi method name
        folder = FakeFolder(name, self)
        self.childEntity.append(folder)
        return folder

    def Destroy_Task(self):  # noqa: N802
        self.parent.childEntity.remove(self)
        return FakeTask(label=f"destroy-folder:{self.name}")


class FakePortgroup:
    def __init__(self, dvs, spec):
        self.name = spec.name
        self.key = f"dvportgroup-key-{next(_ids)}"
        self._moId = f"dvportgroup-{next(_ids)}"
        self.spec = spec
        self.config = SimpleNamespace(distributedVirtualSwitch=dvs, defaultPortConfig=spec.defaultPortConfig)
        self._dvs = dvs

    def Destroy_Task(self):  # noqa: N802
        self._dvs.portgroup.remove(self)
        return FakeTask(label=f"destroy-pg:{self.name}")


class FakeDVS:
    def __init__(self, name="vDS-10G", existing_vlans=()):
        self.name = name
        self.uuid = "50 1a 2b 3c"
        self.portgroup: list = []
        for vlan in existing_vlans:
            self.portgroup.append(FakePortgroup(self, infra.dvs_portgroup_spec(f"dPG-VM-{vlan}", vlan, False)))

    def CreateDVPortgroup_Task(self, spec):  # noqa: N802
        self.portgroup.append(FakePortgroup(self, spec))
        return FakeTask(label=f"create-pg:{spec.name}")


def _apply(devices: list, changes) -> list:
    """What vCenter does with a deviceChange list: edit in place, add (with a MAC), remove."""
    out = list(devices)
    for change in changes or []:
        dev = change.device
        if change.operation == "add":
            dev.key = 4000 + len(out) + 100
            dev.macAddress = f"00:50:56:aa:00:{len(out):02x}"
            out.append(dev)
        elif change.operation == "remove":
            out = [d for d in out if d.key != dev.key]
    return out


def _card(key: int, network: str):
    card = vim.vm.device.VirtualVmxnet3(key=key, backing=vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(
        deviceName=network))
    card.macAddress = f"00:50:56:bb:00:{key % 256:02x}"
    return card


class FakeGuestOps:
    """guestOperationsManager: auth and process managers in one, with scripted outcomes.

    A program whose arguments contain a key of ``exit_codes`` ends with that code; one
    matching ``hang`` never ends. Logins are checked against the password the VM was
    customized with (Sysprep spec, or the cloud-init user in its guestinfo).
    """

    def __init__(self):
        self.processManager = self.authManager = self
        self.started: list[tuple[str, str, str, str]] = []  # (vm, user, program, arguments)
        self.passwords_seen: set[str] = set()
        self.exit_codes: dict[str, int] = {}
        self.hang: set[str] = set()
        self.terminated: list[int] = []
        self.procs: dict[int, object] = {}
        self._pid = itertools.count(100)

    def _check(self, vm, auth):
        self.passwords_seen.add(auth.password)
        expected = vm.guest_password(auth.username) or ""
        if expected.startswith("hashed:"):  # glibc: cloud-init got a SHA-512 crypt hash
            import crypt  # noqa: PLC0415 -- only where the worker itself produced a hash

            ok = crypt.crypt(auth.password, expected[7:]) == expected[7:]
        else:
            ok = bool(expected) and auth.password == expected
        if not ok:
            raise vim.fault.InvalidGuestLogin()

    def ValidateCredentialsInGuest(self, vm, auth):  # noqa: N802
        self._check(vm, auth)

    def StartProgramInGuest(self, vm, auth, spec):  # noqa: N802
        self._check(vm, auth)
        pid = next(self._pid)
        self.started.append((vm.name, auth.username, spec.programPath, spec.arguments))
        code = next((c for k, c in self.exit_codes.items() if k in spec.arguments), 0)
        hangs = any(k in spec.arguments for k in self.hang)
        self.procs[pid] = SimpleNamespace(pid=pid, endTime=None if hangs else "done", exitCode=None if hangs else code)
        return pid

    def ListProcessesInGuest(self, vm, auth, pids):  # noqa: N802
        self._check(vm, auth)
        return [self.procs[p] for p in pids]

    def TerminateProcessInGuest(self, vm, auth, pid):  # noqa: N802
        self.terminated.append(pid)


class FakeVM:
    def __init__(self, name: str, devices: list, template: bool = False):
        self.name = name
        self._moId = f"vm-{next(_ids)}"
        self.config = SimpleNamespace(template=template, hardware=SimpleNamespace(device=devices))
        self.guest = SimpleNamespace(guestOperationsReady=True, customizationInfo=SimpleNamespace(
            customizationStatus="TOOLSDEPLOYPKG_SUCCEEDED"))
        self.clone_specs: list = []
        self.reconfig_specs: list = []
        self.customize_specs: list = []
        self.clone_fail: str | None = None

    def CloneVM_Task(self, folder, name, spec):  # noqa: N802
        self.clone_specs.append((folder, name, spec))
        if self.clone_fail:
            return FakeTask(fail=self.clone_fail)
        vm = FakeVM(name, _apply([_card(d.key, "x") for d in self.config.hardware.device], spec.config.deviceChange))
        vm.placed = spec.location
        folder.childEntity.append(vm)
        FAKE.vms[vm._moId] = vm
        return FakeTask(result=vm, label=f"clone:{name}")

    def ReconfigVM_Task(self, spec):  # noqa: N802
        self.reconfig_specs.append(spec)
        if spec.deviceChange:
            self.config.hardware.device = _apply(self.config.hardware.device, spec.deviceChange)
        return FakeTask(label=f"reconfig:{self.name}")

    def CustomizeVM_Task(self, spec):  # noqa: N802
        self.customize_specs.append(spec)
        return FakeTask(label=f"customize:{self.name}")

    def guest_password(self, username: str) -> str | None:
        """What the guest would accept for ``username`` after customization."""
        if self.customize_specs and username == "Administrator":
            return self.customize_specs[-1].identity.guiUnattended.password.value
        for spec in self.reconfig_specs:
            extra = {o.key: o.value for o in spec.extraConfig or []}
            if "guestinfo.userdata" not in extra:
                continue
            doc = yaml.safe_load(base64.b64decode(extra["guestinfo.userdata"]))
            for user in doc.get("users") or []:
                if isinstance(user, dict) and user["name"] == username:
                    return user.get("plain_text_passwd") or ("hashed:" + user["hashed_passwd"])
        return None


class FakeVCenter:
    """Inventory plus a REST handler with vCenter's /api behaviour."""

    def __init__(self, hosts, *, dvs=None, templates=(), library: dict | None = None):
        self.hosts = hosts
        self.dvs = dvs or FakeDVS()
        self.dc = SimpleNamespace(name="DC-Lab", vmFolder=FakeFolder("vm"))
        self.cluster = SimpleNamespace(name="CL-Lab", host=hosts,
                                       resourcePool=vim.ResourcePool("resgroup-8", None))
        self.vms: dict[str, FakeVM] = {}
        self.templates = {}
        for name in templates:
            tmpl = FakeVM(name, [_card(4000, MGMT)], template=True)
            self.templates[name] = tmpl
            self.dc.vmFolder.childEntity.append(tmpl)
        self.library = library  # {"name": ..., "items": {template_name: item_id}} or None
        self.power: dict[str, str] = {}
        self.requests: list[tuple[str, str, dict | None]] = []
        self.logins = 0
        self.fail_401 = 0  # answer this many non-login calls with 401
        self.waited: list[str] = []
        self.fail_delete: set[str] = set()
        smoke = SimpleNamespace(name="dPG-SMOKE", _moId="network-77", config=None)
        mgmt = SimpleNamespace(name=MGMT, _moId="network-30", config=None)
        svc = SimpleNamespace(name="dPG-TN-SVC", _moId="network-32", config=None)
        self.networks = [smoke, mgmt, svc]
        self.guest_ops = FakeGuestOps()

    # pyVmomi ------------------------------------------------------------
    def si(self):
        def view(container, types, recursive):
            kind = types[0]
            objs = {
                vim.Datacenter: [self.dc], vim.ClusterComputeResource: [self.cluster],
                vim.DistributedVirtualSwitch: [self.dvs], vim.Network: self.networks,
                vim.Datastore: [d for h in self.hosts for d in h.datastore],
                vim.VirtualMachine: list(self.templates.values()),
            }[kind]
            return SimpleNamespace(view=objs, Destroy=lambda: None)

        return SimpleNamespace(_stub=object(), content=SimpleNamespace(
            rootFolder=object(), viewManager=SimpleNamespace(CreateContainerView=view),
            guestOperationsManager=self.guest_ops))

    def wait(self, task, si=None, maxWaitTime=None):  # noqa: N803 -- pyVim's keyword
        assert maxWaitTime, "an unbounded wait can outlive Celery's visibility timeout"
        self.waited.append(task.label)
        if task.fail:
            raise RuntimeError(task.fail)

    # REST ---------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        path, query = request.url.path, request.url.query.decode()
        body = json.loads(request.content) if request.content else None
        if path == "/api/session" and request.method == "POST":
            self.logins += 1
            return httpx.Response(201, json=f"token-{self.logins}")
        assert request.headers.get("vmware-api-session-id", "").startswith("token-")
        self.requests.append((request.method, f"{path}?{query}" if query else path, body))
        if self.fail_401:
            self.fail_401 -= 1
            return httpx.Response(401, json={"error_type": "UNAUTHENTICATED"})
        if path == "/api/content/library" and query == "action=find":
            return httpx.Response(200, json=["lib-1"] if self.library and body["name"] == self.library["name"] else [])
        if path == "/api/content/library/item" and query == "action=find":
            item = (self.library or {}).get("items", {}).get(body["name"])
            return httpx.Response(200, json=[item] if item else [])
        if m := re.fullmatch(r"/api/vcenter/ovf/library-item/([^/]+)", path):
            if query == "action=filter":
                return httpx.Response(200, json={"networks": ["VM Network"], "name": m.group(1)})
            name = body["deployment_spec"]["name"]
            vm = FakeVM(name, [_card(4000, "VM Network")])
            self.vms[vm._moId] = vm
            return httpx.Response(200, json={"succeeded": True, "resource_id": {"type": "VirtualMachine",
                                                                                "id": vm._moId}})
        if m := re.fullmatch(r"/api/vcenter/vm/([^/]+)/power", path):
            vm_id = m.group(1)
            if vm_id not in self.vms:
                return httpx.Response(404, json={"error_type": "NOT_FOUND"})
            if request.method == "GET":
                return httpx.Response(200, json={"state": self.power.get(vm_id, "POWERED_OFF")})
            self.power[vm_id] = "POWERED_ON" if query == "action=start" else "POWERED_OFF"
            return httpx.Response(204)
        if m := re.fullmatch(r"/api/vcenter/vm/([^/]+)", path):
            vm_id = m.group(1)
            if vm_id in self.fail_delete:
                return httpx.Response(500, json={"error_type": "ERROR"})
            vm = self.vms.pop(vm_id, None)
            if vm is None:
                return httpx.Response(404, json={"error_type": "NOT_FOUND"})
            for folder in _folders(self.dc.vmFolder):
                if vm in folder.childEntity:
                    folder.childEntity.remove(vm)
            return httpx.Response(204)
        return httpx.Response(404, json={"error_type": "NOT_FOUND", "path": path})


def _folders(root):
    yield root
    for child in root.childEntity:
        if isinstance(child, FakeFolder):
            yield from _folders(child)


FAKE: FakeVCenter = None  # type: ignore[assignment]  # the vCenter the current test talks to


@pytest.fixture
def vc(monkeypatch):
    """A 4-host lab like the target: esx01 runs the VCSA, local datastores, vDS-10G."""
    global FAKE
    hosts = [FakeHost(f"esx0{i}.truenorth.lab") for i in range(1, 5)]
    FAKE = FakeVCenter(hosts, templates=["tmpl-ubuntu-2404", "tmpl-win2022", "tmpl-pfsense"])
    monkeypatch.setattr(mod, "SmartConnect", MagicMock(side_effect=lambda **kw: FAKE.si()))
    monkeypatch.setattr(mod, "Disconnect", MagicMock())
    monkeypatch.setattr(mod, "WaitForTask", lambda task, **kw: FAKE.wait(task, **kw))
    for name, value in {
        "VSPHERE_URL": "https://vcsa.test", "VSPHERE_DATACENTER": "DC-Lab", "VSPHERE_CLUSTER": "CL-Lab",
        "VSPHERE_CONTENT_LIBRARY": "", "VSPHERE_NETWORK": "", "VSPHERE_MGMT_NETWORK": MGMT,
        "VSPHERE_RANGE_SWITCH_MODE": "vds", "VSPHERE_RANGE_DVS": "vDS-10G", "VSPHERE_VLAN_POOL": "100-199",
        "VSPHERE_PLACEMENT": "spread", "VSPHERE_EXCLUDE_HOSTS": "esx01.truenorth.lab",
        "VSPHERE_MAX_VCPU_PER_THREAD": 4.0, "VSPHERE_TOOLS_TIMEOUT": 0,
        "VSPHERE_RANGE_UPLINK_NETWORK": "", "VSPHERE_RANGE_UPLINK_POOL": "", "VSPHERE_RANGE_UPLINK_GATEWAY": "",
        "VSPHERE_RANGE_UPLINK_PREFIX": 24, "TN_DEPOT_URL": "", "TN_DEPOT_CHOCO_FEED": "", "TN_DEPOT_APT_PROXY": "",
        "TN_SOFTWARE_INSTALL_TIMEOUT": 1800, "VSPHERE_PROVISION_BUDGET": 3300,
    }.items():
        monkeypatch.setattr(mod, name, value)
    return FAKE


def _prov(vc) -> mod.VsphereAPIProvisioner:
    prov = mod.VsphereAPIProvisioner()
    prov._transport = httpx.MockTransport(vc.handler)
    prov._vm = lambda si, vm_id: vc.vms[vm_id]
    prov._guest_poll = 0
    return prov


TEMPLATE = {
    "name": "lab",
    "network": {"vlans": [
        {"id": 200, "name": "attacker_infra", "cidr": "10.60.200.0/24"},
        {"id": 201, "name": "victim_network", "cidr": "10.60.201.0/24"},
        {"id": 203, "name": "network_monitoring", "cidr": "10.60.203.0/24"},
    ]},
    "nodes": [
        {"id": "fw", "role": "firewall", "os": "pfsense", "vlan": "attacker_infra",
         "specs": {"cores": 2, "memory_mb": 2048, "disk_gb": 20},
         "interfaces": [{"vlan": "attacker_infra"}, {"vlan": "victim_network"}, {"vlan": "network_monitoring"}]},
        {"id": "web01", "role": "server", "os": "ubuntu-2404", "vlan": "victim_network", "ip": "10.60.201.20"},
        {"id": "dc01", "role": "domain_controller", "os": "windows-server-2022", "vlan": "victim_network",
         "ip": "10.60.201.10"},
        {"id": "sensor", "role": "network_monitor", "os": "ubuntu-2404", "vlan": "network_monitoring"},
    ],
}
IMAGES = {"pfsense": "tmpl-pfsense", "ubuntu-2404": "tmpl-ubuntu-2404", "windows-server-2022": "tmpl-win2022"}


def _rendered(template=TEMPLATE) -> dict:
    out = render.render_topology(template, RANGE_ID, IMAGES.get)
    return {"name": "lab", "vms": out["vm_definitions"], "networks": out["network_definitions"]}


def _backing_keys(spec) -> list[str]:
    """Port group key of every NIC a ConfigSpec leaves (edit/add) on the VM."""
    return [c.device.backing.port.portgroupKey for c in spec.deviceChange if c.operation != "remove"]


# --------------------------------------------------------------------------- #
# Provision: inventory-template clone path (the lab has no Content Library)
# --------------------------------------------------------------------------- #


class TestProvisionClone:
    def test_builds_an_isolated_range(self, vc):
        prov = _prov(vc)
        result = _run(prov.provision(RANGE_ID, _rendered(), {"used_vlans": [100, 101]}))
        assert result.status == "ok", result.errors

        # Physical VLANs from the pool, past the ones other ranges hold; never the template's 200s.
        pgs = {pg.name: pg for pg in vc.dvs.portgroup}
        assert sorted(pgs) == [f"tn-{R8}-v102", f"tn-{R8}-v103", f"tn-{R8}-v104"]
        vlans = {name: pg.spec.defaultPortConfig.vlan.vlanId for name, pg in pgs.items()}
        assert vlans == {f"tn-{R8}-v102": 102, f"tn-{R8}-v103": 103, f"tn-{R8}-v104": 104}
        promisc = {name: pg.spec.defaultPortConfig.securityPolicy.allowPromiscuous.value for name, pg in pgs.items()}
        assert promisc == {f"tn-{R8}-v102": False, f"tn-{R8}-v103": False, f"tn-{R8}-v104": True}  # monitoring
        for pg in pgs.values():
            sec = pg.spec.defaultPortConfig.securityPolicy
            assert sec.forgedTransmits.value is False and sec.macChanges.value is False
            assert pg.spec.type == "earlyBinding" and pg.spec.autoExpand is True

        by_net = {n["name"]: n for n in result.networks}
        assert by_net["victim_network"]["physical_vlan"] == 103
        assert by_net["victim_network"]["portgroup"] == f"tn-{R8}-v103"

        # Named <range8>-<node> exactly once, in the range's own folder.
        names = sorted(vm["name"] for vm in result.vms)
        assert names == sorted(f"{R8}-{n}" for n in ("fw", "web01", "dc01", "sensor"))
        folder = infra.find_folder(vc.dc, f"truenorth/ranges/{R8}")
        assert sorted(v.name for v in folder.childEntity) == names
        assert all(vc.power[vm["vm_id"]] == "POWERED_ON" for vm in result.vms)
        # Cloned straight onto the placed host's local datastore, never the VCSA host.
        for vm in result.vms:
            loc = vc.vms[vm["vm_id"]].placed
            assert loc.host.name == vm["host"] != "esx01.truenorth.lab"
            assert loc.datastore in loc.host.datastore and loc.pool._moId == "resgroup-8"

    def test_nics_are_on_range_portgroups_never_the_template_network(self, vc):
        prov = _prov(vc)
        result = _run(prov.provision(RANGE_ID, _rendered(), {}))
        assert result.status == "ok", result.errors
        key = {pg.name: pg.key for pg in vc.dvs.portgroup}
        fw_spec = vc.templates["tmpl-pfsense"].clone_specs[0][2]
        # The firewall spans three zones: its template NIC re-pointed, two vmxnet3 added.
        assert [c.operation for c in fw_spec.config.deviceChange] == ["edit", "add", "add"]
        assert _backing_keys(fw_spec.config) == [key[f"tn-{R8}-v100"], key[f"tn-{R8}-v101"], key[f"tn-{R8}-v102"]]
        assert all(c.device.backing.port.switchUuid == vc.dvs.uuid for c in fw_spec.config.deviceChange)
        for tmpl in vc.templates.values():
            for _, _, spec in tmpl.clone_specs:
                assert not isinstance(spec.config.deviceChange[0].device.backing,
                                      vim.vm.device.VirtualEthernetCard.NetworkBackingInfo)
        assert fw_spec.config.numCPUs == 2 and fw_spec.config.memoryMB == 2048
        assert fw_spec.powerOn is False and fw_spec.template is False
        fw = next(v for v in vc.vms.values() if v.name == f"{R8}-fw")
        assert fw.reconfig_specs == [] and fw.customize_specs == []  # appliance: left alone

    def test_linux_guestinfo_carries_the_rendered_ips(self, vc):
        prov = _prov(vc)
        _run(prov.provision(RANGE_ID, _rendered(), {}))
        web = next(v for v in vc.vms.values() if v.name == f"{R8}-web01")
        extra = {o.key: o.value for o in web.reconfig_specs[-1].extraConfig}
        assert extra["guestinfo.metadata.encoding"] == "base64"
        meta = yaml.safe_load(base64.b64decode(extra["guestinfo.metadata"]))
        assert meta["local-hostname"] == "web01"
        eth = meta["network"]["ethernets"]["nic0"]
        assert eth["match"]["macaddress"] == web.config.hardware.device[0].macAddress
        assert eth["addresses"] == ["10.60.201.20/24"]
        assert eth["routes"] == [{"to": "default", "via": "10.60.201.1"}]
        assert base64.b64decode(extra["guestinfo.userdata"]).decode().startswith("#cloud-config")

    def test_windows_gets_sysprep_with_static_ip(self, vc):
        prov = _prov(vc)
        _run(prov.provision(RANGE_ID, _rendered(), {}))
        dc = next(v for v in vc.vms.values() if v.name == f"{R8}-dc01")
        (spec,) = dc.customize_specs
        (adapter,) = spec.nicSettingMap
        assert adapter.adapter.ip.ipAddress == "10.60.201.10"
        assert adapter.adapter.subnetMask == "255.255.255.0"
        assert adapter.adapter.gateway == ["10.60.201.1"]
        assert spec.identity.userData.computerName.name == "dc01"
        assert spec.identity.identification.joinWorkgroup == "RANGE"
        assert spec.identity.guiUnattended.autoLogon is False
        assert dc.reconfig_specs == []  # no cloud-init on Windows

    def test_mgmt_network_is_refused_as_fallback(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_NETWORK", MGMT)
        prov = _prov(vc)
        tpl = {"vms": [{"name": "smoke", "template_name": "tmpl-ubuntu-2404", "ip": "10.0.0.5"}]}
        result = _run(prov.provision(RANGE_ID, tpl, {}))
        assert result.status == "failed"
        assert "management network" in result.errors[0]
        assert not vc.vms

    def test_vm_without_vlan_uses_the_smoke_fallback(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_NETWORK", "dPG-SMOKE")
        prov = _prov(vc)
        tpl = {"vms": [{"name": "smoke", "template_name": "tmpl-ubuntu-2404", "ip": "10.0.0.5"}]}
        result = _run(prov.provision(RANGE_ID, tpl, {}))
        assert result.status == "ok", result.errors
        assert result.vms[0]["name"] == f"{R8}-smoke"
        spec = vc.templates["tmpl-ubuntu-2404"].clone_specs[0][2]
        assert spec.config.deviceChange[0].device.backing.deviceName == "dPG-SMOKE"
        assert vc.dvs.portgroup == []

    def test_failed_build_removes_what_it_made(self, vc):
        for tmpl in vc.templates.values():
            tmpl.clone_fail = "disk full"
        prov = _prov(vc)
        result = _run(prov.provision(RANGE_ID, _rendered(), {}))
        assert result.status == "failed"
        assert vc.dvs.portgroup == []  # rolled back, so a retry starts clean
        assert infra.find_folder(vc.dc, f"truenorth/ranges/{R8}") is None
        # The reservation still names the port groups, for the destroy that follows.
        assert {n["portgroup"] for n in result.networks if "portgroup" in n} == {
            f"tn-{R8}-v100", f"tn-{R8}-v101", f"tn-{R8}-v102"}

    def test_unknown_template_is_a_clear_error(self, vc):
        prov = _prov(vc)
        tpl = _rendered({**TEMPLATE, "nodes": [{"id": "x", "os": "nonexistent", "vlan": "victim_network"}]})
        result = _run(prov.provision(RANGE_ID, tpl, {}))
        assert result.status == "failed"
        assert "neither a Content Library item nor an inventory VM template" in result.errors[0]

    def test_vlan_already_on_the_vds_is_refused(self, vc):
        vc.dvs = FakeDVS(existing_vlans=[100])
        prov = _prov(vc)
        result = _run(prov.provision(RANGE_ID, _rendered(), {"physical_vlans": {200: 100, 201: 120, 203: 121}}))
        assert result.status == "failed"
        assert "VLAN 100 is already used by port group 'dPG-VM-100'" in result.errors[0]
        assert [pg.name for pg in vc.dvs.portgroup] == ["dPG-VM-100"]

    def test_reserved_vlans_from_the_worker_are_used(self, vc):
        prov = _prov(vc)
        result = _run(prov.provision(RANGE_ID, _rendered(), {"physical_vlans": {200: 150, 201: 151, 203: 152}}))
        assert result.status == "ok", result.errors
        assert sorted(pg.name for pg in vc.dvs.portgroup) == [f"tn-{R8}-v150", f"tn-{R8}-v151", f"tn-{R8}-v152"]

    def test_vss_mode_creates_the_portgroup_on_every_host_with_a_vm(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_RANGE_SWITCH_MODE", "vss")
        prov = _prov(vc)
        result = _run(prov.provision(RANGE_ID, _rendered(), {}))
        assert result.status == "ok", result.errors
        used = {vm["host"] for vm in result.vms}
        for host in vc.hosts:
            names = {n.name for n in host.network}
            assert (f"tn-{R8}-v101" in names) == (host.name in used), host.name
        spec = vc.hosts[1].configManager.networkSystem.AddPortGroup.call_args_list[0].kwargs["portgrp"]
        assert spec.vswitchName == "vSwitch1" and spec.vlanId in (100, 101, 102)
        fw_spec = vc.templates["tmpl-pfsense"].clone_specs[0][2]
        assert [c.device.backing.deviceName for c in fw_spec.config.deviceChange] == [
            f"tn-{R8}-v100", f"tn-{R8}-v101", f"tn-{R8}-v102"]


# --------------------------------------------------------------------------- #
# Provision: Content Library OVF path
# --------------------------------------------------------------------------- #


class TestProvisionOvf:
    def test_deploy_body_targets_the_placed_host_and_maps_networks(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_CONTENT_LIBRARY", "TrueNorth-Templates")
        vc.library = {"name": "TrueNorth-Templates", "items": {"tmpl-ubuntu-2404": "item-ubuntu"}}
        prov = _prov(vc)
        tpl = _rendered({**TEMPLATE, "nodes": [TEMPLATE["nodes"][1]]})
        result = _run(prov.provision(RANGE_ID, tpl, {}))
        assert result.status == "ok", result.errors

        finds = [r for r in vc.requests if r[1].startswith("/api/content/library")]
        assert finds[0] == ("POST", "/api/content/library?action=find", {"name": "TrueNorth-Templates"})
        assert finds[1] == ("POST", "/api/content/library/item?action=find",
                            {"name": "tmpl-ubuntu-2404", "library_id": "lib-1"})
        (_, _, filt) = next(r for r in vc.requests if r[1].endswith("action=filter"))
        (_, url, body) = next(r for r in vc.requests if r[1].endswith("action=deploy"))
        assert url == "/api/vcenter/ovf/library-item/item-ubuntu?action=deploy"
        placed = next(h for h in vc.hosts if h.name == result.vms[0]["host"])
        assert body["target"] == filt["target"] == {
            "resource_pool_id": "resgroup-8",
            "folder_id": infra.find_folder(vc.dc, f"truenorth/ranges/{R8}")._moId,
            "host_id": placed._moId,
        }
        spec = body["deployment_spec"]
        assert spec["name"] == f"{R8}-web01"
        assert spec["accept_all_EULA"] is True and spec["storage_provisioning"] == "thin"
        assert spec["default_datastore_id"] == placed.datastore[0]._moId
        pg = next(p for p in vc.dvs.portgroup if p.name == f"tn-{R8}-v100")
        assert spec["network_mappings"] == {"VM Network": pg._moId}
        assert MGMT not in json.dumps(body)

        vm = vc.vms[result.vms[0]["vm_id"]]
        hw, meta = vm.reconfig_specs
        assert _backing_keys(hw) == [pg.key] and hw.numCPUs == 2
        assert {o.key for o in meta.extraConfig} >= {"guestinfo.metadata", "guestinfo.userdata"}

    def test_template_missing_from_the_library_falls_back_to_clone(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_CONTENT_LIBRARY", "TrueNorth-Templates")
        vc.library = {"name": "TrueNorth-Templates", "items": {}}
        prov = _prov(vc)
        result = _run(prov.provision(RANGE_ID, _rendered({**TEMPLATE, "nodes": [TEMPLATE["nodes"][1]]}), {}))
        assert result.status == "ok", result.errors
        assert vc.templates["tmpl-ubuntu-2404"].clone_specs
        assert not any(r[1].endswith("action=deploy") for r in vc.requests)


# --------------------------------------------------------------------------- #
# Destroy
# --------------------------------------------------------------------------- #


class TestDestroy:
    def _built(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {}))
        assert result.status == "ok", result.errors
        return {"vms": result.vms, "networks": result.networks}

    def test_removes_vms_then_portgroups_and_folder(self, vc):
        output = self._built(vc)
        vc.requests.clear()
        result = _run(_prov(vc).destroy(RANGE_ID, output))
        assert result.status == "ok", result.errors
        assert result.resources_removed == 4 + 3
        assert not vc.vms and vc.dvs.portgroup == []
        assert infra.find_folder(vc.dc, f"truenorth/ranges/{R8}") is None
        assert infra.find_folder(vc.dc, "truenorth/ranges") is not None  # shared parent stays
        assert sum(1 for r in vc.requests if r[0] == "DELETE") == 4

    def test_is_idempotent(self, vc):
        output = self._built(vc)
        _run(_prov(vc).destroy(RANGE_ID, output))
        again = _run(_prov(vc).destroy(RANGE_ID, output))
        assert again.status == "ok", again.errors

    def test_portgroups_stay_while_a_vm_is_left(self, vc):
        output = self._built(vc)
        vc.fail_delete.add(output["vms"][0]["vm_id"])
        result = _run(_prov(vc).destroy(RANGE_ID, output))
        assert result.status == "partial"
        assert len(vc.dvs.portgroup) == 3

    def test_reservation_only_output_removes_portgroups(self, vc):
        """After a failed build only the reservation is recorded; destroy still cleans up."""
        output = self._built(vc)
        nets = [{k: n[k] for k in ("vlan_id", "physical_vlan", "portgroup", "switch_mode")}
                for n in output["networks"]]
        for vm in output["vms"]:
            vc.vms.pop(vm["vm_id"])
        result = _run(_prov(vc).destroy(RANGE_ID, {"vms": [], "networks": nets}))
        assert result.status == "ok", result.errors
        assert vc.dvs.portgroup == []


# --------------------------------------------------------------------------- #
# Sessions, power, health
# --------------------------------------------------------------------------- #


class TestSession:
    OUT = {"vms": [{"name": "a", "vm_id": "vm-a"}]}

    def _vc(self, vc):
        vc.vms["vm-a"] = FakeVM("a", [])
        vc.power["vm-a"] = "POWERED_ON"
        return vc

    def test_401_logs_in_again_once(self, vc):
        prov = _prov(self._vc(vc))
        prov._session_token = "token-stale"
        vc.fail_401 = 1
        result = _run(prov.health_check(RANGE_ID, self.OUT))
        assert result.healthy is True
        assert vc.logins == 1  # the stale token failed; one fresh login
        assert result.vm_statuses == [{"vm_id": "vm-a", "name": "a", "status": "powered_on", "healthy": True}]

    def test_second_401_is_a_failure(self, vc):
        prov = _prov(self._vc(vc))
        vc.fail_401 = 2
        result = _run(prov.health_check(RANGE_ID, self.OUT))
        assert result.healthy is False and result.errors
        assert vc.logins == 2

    def test_stop_and_start_are_idempotent(self, vc):
        prov = _prov(self._vc(vc))
        assert _run(prov.stop(RANGE_ID, self.OUT)).status == "ok"
        assert vc.power["vm-a"] == "POWERED_OFF"
        assert _run(prov.stop(RANGE_ID, self.OUT)).vms_stopped == 1
        posts = [r for r in vc.requests if r[0] == "POST"]
        assert len(posts) == 1  # the second stop saw it was off already
        started = _run(prov.start(RANGE_ID, self.OUT))
        assert started.status == "ok" and started.vms_started == 1 and vc.power["vm-a"] == "POWERED_ON"

    def test_db_credentials_replace_the_env(self, vc):
        prov = mod.VsphereAPIProvisioner({"host": "vcsa.truenorth.lab", "username": "svc@vsphere.local",
                                          "password": "pw", "verify_ssl": True, "datacenter": "DC-X"})
        assert prov._base_url == "https://vcsa.truenorth.lab"
        assert (prov._username, prov._password, prov._datacenter, prov._verify_ssl) == (
            "svc@vsphere.local", "pw", "DC-X", True)


# --------------------------------------------------------------------------- #
# Placement and VLAN pool (pure)
# --------------------------------------------------------------------------- #


def _cap(name, free_mb=500_000, threads=16, vcpus=0, ds_free_gb=1600):
    return infra.HostCapacity(name=name, ref=SimpleNamespace(name=name), free_mem_mb=free_mb, threads=threads,
                              vcpus_allocated=vcpus, datastores={f"{name}-local": [ds_free_gb * GIB, name]})


def _vms(n, cores=4, mem=8192, disk=60):
    return [{"name": f"vm{i}", "cores": cores, "memory_mb": mem, "disk_gb": disk} for i in range(n)]


class TestPlacement:
    def test_per_range_host_picks_most_free_ram_that_fits(self):
        hosts = [_cap("a", free_mb=100_000), _cap("b", free_mb=300_000), _cap("c", free_mb=200_000, vcpus=60)]
        placed = infra.place(hosts, _vms(3), "per-range-host", 4)
        assert {h.name for h, _ in placed} == {"b"}
        assert placed[0][1] == "b"  # the host's own datastore

    def test_vcpu_cap_rules_a_host_out(self):
        hosts = [_cap("a", free_mb=400_000, vcpus=60), _cap("b", free_mb=100_000)]
        placed = infra.place(hosts, _vms(2), "per-range-host", 4)  # 8 vCPU: a has 4 left
        assert placed[0][0].name == "b"

    def test_nothing_fits_raises_with_reasons(self):
        with pytest.raises(infra.PlacementError, match="vCPU headroom"):
            infra.place([_cap("a", vcpus=64)], _vms(1), "per-range-host", 4)
        with pytest.raises(infra.PlacementError, match="no datastore"):
            infra.place([_cap("a", ds_free_gb=10)], _vms(1, disk=100), "spread", 4)
        with pytest.raises(infra.PlacementError, match="no eligible host"):
            infra.place([], _vms(1), "spread", 4)

    def test_spread_takes_a_range_bigger_than_one_host(self):
        """soc-training needs 69 vCPU; one host offers 16 threads x 4 = 64."""
        hosts = [_cap("esx02"), _cap("esx03"), _cap("esx04")]
        placed = infra.place(hosts, _vms(18, cores=4), "spread", 4)  # 72 vCPU
        counts = {n: sum(1 for h, _ in placed if h.name == n) for n in ("esx02", "esx03", "esx04")}
        assert counts == {"esx02": 6, "esx03": 6, "esx04": 6}
        with pytest.raises(infra.PlacementError):
            infra.place([_cap("esx02")], _vms(18, cores=4), "per-range-host", 4)

    def test_spread_counts_datastore_space_as_it_goes(self):
        hosts = [_cap("a", ds_free_gb=20), _cap("b", ds_free_gb=1000)]
        placed = infra.place(hosts, _vms(3, disk=60), "spread", 4)  # 15 GiB each counted
        assert [h.name for h, _ in placed].count("a") == 1

    def test_maintenance_and_disconnected_hosts_are_not_eligible(self):
        assert infra.host_capacity(FakeHost("m", maintenance=True)) is None
        host = FakeHost("d")
        host.runtime.connectionState = "disconnected"
        assert infra.host_capacity(host) is None
        cap = infra.host_capacity(FakeHost("ok", free_gb_ram=100, vcpus_on=6))
        assert cap.free_mem_mb == 100 * 1024 and cap.vcpus_allocated == 6 and cap.threads == 16

    def test_excluded_host_is_never_used(self, vc):
        prov = _prov(vc)
        tpl = _rendered({**TEMPLATE, "nodes": [{"id": "big", "os": "ubuntu-2404", "vlan": "victim_network",
                                                 "count": 15, "specs": {"cores": 12}}]})
        result = _run(prov.provision(RANGE_ID, tpl, {}))
        assert result.status == "ok", result.errors
        used = {vm["host"] for vm in result.vms}
        assert used == {"esx02.truenorth.lab", "esx03.truenorth.lab", "esx04.truenorth.lab"}

    def test_range_too_big_for_the_cluster_fails_cleanly(self, vc):
        prov = _prov(vc)
        tpl = _rendered({**TEMPLATE, "nodes": [{"id": "big", "os": "ubuntu-2404", "vlan": "victim_network",
                                                 "count": 40, "specs": {"cores": 8}}]})
        result = _run(prov.provision(RANGE_ID, tpl, {}))
        assert result.status == "failed"
        assert "no host fits" in result.errors[0]
        assert not vc.vms and vc.dvs.portgroup == []


class TestVlanPool:
    def test_parse(self):
        assert vlan_pool.parse_pool("100-103,110") == [100, 101, 102, 103, 110]
        with pytest.raises(ValueError):
            vlan_pool.parse_pool("0-5")

    def test_two_ranges_do_not_collide(self):
        pool = vlan_pool.parse_pool("100-199")
        first = vlan_pool.allocate([200, 201, 202], set(), pool)
        second = vlan_pool.allocate([200, 201], set(first.values()), pool)
        assert first == {200: 100, 201: 101, 202: 102}
        assert set(second.values()) == {103, 104}

    def test_exhaustion_raises(self):
        with pytest.raises(vlan_pool.VlanPoolExhaustedError, match="needs 3 VLANs but only 2"):
            vlan_pool.allocate([1, 2, 3], {100}, [100, 101, 102])


# --------------------------------------------------------------------------- #
# Render: multi-NIC output, backward-compatible fields
# --------------------------------------------------------------------------- #


class TestRenderNics:
    def test_firewall_gets_a_nic_per_zone_holding_each_gateway(self):
        vms = {v["node_id"]: v for v in _rendered()["vms"]}
        fw = vms["fw"]
        assert [(n["network"], n["vlan"], n["ip"]) for n in fw["nics"]] == [
            ("attacker_infra", 200, "10.60.200.1"), ("victim_network", 201, "10.60.201.1"),
            ("network_monitoring", 203, "10.60.203.1")]
        assert all(n["gateway"] == "" for n in fw["nics"])  # it is the gateway
        # The fields proxmox/terraform/hyperv read are unchanged.
        assert (fw["vlan_id"], fw["vlan_tag"], fw["ip"], fw["prefix"]) == (200, 200, "10.60.200.1", 24)

    def test_plain_vm_has_one_nic_matching_its_flat_fields(self):
        web = next(v for v in _rendered()["vms"] if v["node_id"] == "web01")
        assert web["nics"] == [{"vlan": 201, "network": "victim_network", "ip": "10.60.201.20", "prefix": 24,
                                "netmask": "255.255.255.0", "gateway": "10.60.201.1"}]
        assert web["name"] == f"{R8}-web01"

    def test_interface_on_an_undefined_vlan_is_dropped(self):
        tpl = {**TEMPLATE, "nodes": [{"id": "r", "role": "router", "os": "vyos", "vlan": "attacker_infra",
                                      "interfaces": [{"vlan": "nowhere"}, {"vlan": "victim_network"}]}]}
        (r,) = _rendered(tpl)["vms"]
        assert [n["network"] for n in r["nics"]] == ["attacker_infra", "victim_network"]


# --------------------------------------------------------------------------- #
# WAN uplink: the edge firewall's NIC 0 on the depot network
# --------------------------------------------------------------------------- #

SVC = "dPG-TN-SVC"


@pytest.fixture
def uplink(vc, monkeypatch):
    monkeypatch.setattr(mod, "VSPHERE_RANGE_UPLINK_NETWORK", SVC)
    monkeypatch.setattr(mod, "VSPHERE_RANGE_UPLINK_POOL", "10.30.32.100-10.30.32.102")
    monkeypatch.setattr(mod, "VSPHERE_RANGE_UPLINK_GATEWAY", "10.30.32.1")
    return vc


def _nic_networks(spec) -> list[str]:
    """Network of every NIC a ConfigSpec leaves on the VM: port group key or standard network name."""
    out = []
    for c in spec.deviceChange:
        if c.operation == "remove":
            continue
        b = c.device.backing
        out.append(b.deviceName if isinstance(b, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo)
                   else b.port.portgroupKey)
    return out


class TestUplink:
    def test_edge_firewall_gets_the_wan_nic_first(self, uplink):
        vc = uplink
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {"used_uplink_ips": ["10.30.32.100"]}))
        assert result.status == "ok", result.errors
        key = {pg.name: pg.key for pg in vc.dvs.portgroup}
        fw_spec = vc.templates["tmpl-pfsense"].clone_specs[0][2]
        assert _nic_networks(fw_spec.config) == [
            SVC, key[f"tn-{R8}-v100"], key[f"tn-{R8}-v101"], key[f"tn-{R8}-v102"]]
        # Nobody else is on the uplink.
        for name in ("tmpl-ubuntu-2404", "tmpl-win2022"):
            for _, _, spec in vc.templates[name].clone_specs:
                assert SVC not in _nic_networks(spec.config)
        assert result.uplink == {"network": SVC, "ip": "10.30.32.101", "prefix": 24, "gateway": "10.30.32.1",
                                 "vm": f"{R8}-fw", "node_id": "fw"}
        fw = next(v for v in result.vms if v["node_id"] == "fw")
        assert fw["nics"][0] == {"network": SVC, "ip": "10.30.32.101"}
        assert fw["ip"] == "10.60.200.1"  # still its first zone address, not the WAN
        # The uplink is not a range port group: destroy must never remove it.
        assert all(n.get("portgroup") != SVC for n in result.networks)
        destroyed = _run(_prov(vc).destroy(RANGE_ID, {"vms": result.vms, "networks": result.networks}))
        assert destroyed.status == "ok", destroyed.errors
        assert any(n.name == SVC for n in vc.networks)

    def test_reserved_uplink_ip_from_the_worker_is_used(self, uplink):
        result = _run(_prov(uplink).provision(RANGE_ID, _rendered(), {"uplink_ip": "10.30.32.102"}))
        assert result.status == "ok", result.errors
        assert result.uplink["ip"] == "10.30.32.102"

    def test_node_flagged_edge_wins_over_the_first_firewall(self, uplink):
        tpl = {**TEMPLATE, "nodes": [
            TEMPLATE["nodes"][0],
            {"id": "fw2", "role": "firewall", "os": "pfsense", "vlan": "victim_network", "edge": True},
        ]}
        result = _run(_prov(uplink).provision(RANGE_ID, _rendered(tpl), {}))
        assert result.status == "ok", result.errors
        assert result.uplink["vm"] == f"{R8}-fw2"
        nics = {v["node_id"]: [n["network"] for n in v["nics"]] for v in result.vms}
        assert nics["fw2"][0] == SVC and SVC not in nics["fw"]

    def test_uplink_on_the_management_network_is_refused(self, uplink, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_RANGE_UPLINK_NETWORK", MGMT)
        result = _run(_prov(uplink).provision(RANGE_ID, _rendered(), {}))
        assert result.status == "failed"
        assert "management network" in result.errors[0]
        assert not uplink.vms and uplink.dvs.portgroup == []

    def test_exhausted_pool_fails_before_building(self, uplink):
        used = ["10.30.32.100", "10.30.32.101", "10.30.32.102"]
        result = _run(_prov(uplink).provision(RANGE_ID, _rendered(), {"used_uplink_ips": used}))
        assert result.status == "failed" and "uplink pool" in result.errors[0]
        assert not uplink.vms

    def test_no_uplink_when_unset(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {}))
        assert result.status == "ok" and result.uplink is None
        fw_spec = vc.templates["tmpl-pfsense"].clone_specs[0][2]
        assert len(_nic_networks(fw_spec.config)) == 3


# --------------------------------------------------------------------------- #
# Deploy-time software over guest operations
# --------------------------------------------------------------------------- #

DEPOT = "http://10.30.32.10"
FEED = f'--source "{DEPOT}:8081/repository/chocolatey/"'
SW_TEMPLATE = {**TEMPLATE, "nodes": [
    TEMPLATE["nodes"][0],
    {**TEMPLATE["nodes"][1], "services": ["nginx", "dns", "frobnicator"]},
    {**TEMPLATE["nodes"][2], "services": ["7zip", "Chrome", "iis"]},
]}


@pytest.fixture
def depot(uplink, monkeypatch):
    monkeypatch.setattr(mod, "TN_DEPOT_URL", DEPOT)
    return uplink


def _vm_out(result, node):
    return next(v for v in result.vms if v["node_id"] == node)


def _userdata(vm) -> dict:
    extra = {o.key: o.value for o in vm.reconfig_specs[-1].extraConfig}
    return yaml.safe_load(base64.b64decode(extra["guestinfo.userdata"]))


class TestDeployTimeSoftware:
    def test_installs_from_the_depot_inside_each_guest(self, depot):
        vc = depot
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(SW_TEMPLATE), {}))
        assert result.status == "ok", result.errors

        win = [s for s in vc.guest_ops.started if s[0] == f"{R8}-dc01"]
        assert win == [
            (f"{R8}-dc01", "Administrator", r"C:\ProgramData\chocolatey\bin\choco.exe",
             f"install 7zip -y --no-progress {FEED}"),
            (f"{R8}-dc01", "Administrator", r"C:\ProgramData\chocolatey\bin\choco.exe",
             f"install googlechrome -y --no-progress {FEED}"),
        ]
        lin = [s for s in vc.guest_ops.started if s[0] == f"{R8}-web01"]
        assert {s[1] for s in lin} == {"tn-install"} and {s[2] for s in lin} == {"/usr/bin/sudo"}
        args = [s[3] for s in lin]
        assert "cloud-init status --wait" in args[0]
        assert "apt-get" in args[1] and " update" in args[1]
        assert "install --no-install-recommends nginx" in args[2]
        assert all(f"Acquire::http::Proxy={DEPOT}" in a for a in args[1:3])
        assert "userdel" in args[3] and "passwd -l" in args[3]  # the install user goes
        assert len(args) == 4
        # Nothing ran on the firewall or the sensor (no services).
        assert {s[0] for s in vc.guest_ops.started} == {f"{R8}-dc01", f"{R8}-web01"}

        assert _vm_out(result, "dc01")["software"] == {"status": "ok", "packages": [
            {"name": "7zip", "status": "ok", "exit_code": 0},
            {"name": "googlechrome", "status": "ok", "exit_code": 0}]}
        assert _vm_out(result, "web01")["software"]["packages"] == [{"name": "nginx", "status": "ok", "exit_code": 0}]
        assert any("frobnicator" in w for w in result.warnings)
        assert not any("'dns'" in w or "'iis'" in w for w in result.warnings)  # roles, not software

        # The Linux install user was in the first guestinfo and is gone from the last.
        web = vc.vms[_vm_out(result, "web01")["vm_id"]]
        first = yaml.safe_load(base64.b64decode(
            {o.key: o.value for o in web.reconfig_specs[0].extraConfig}["guestinfo.userdata"]))
        assert first["users"][0] == "default" and first["users"][1]["name"] == "tn-install"
        assert first["users"][1]["sudo"] == "ALL=(ALL) NOPASSWD:ALL"
        assert "users" not in _userdata(web)

    def test_passwords_never_reach_logs_results_or_errors(self, depot, caplog):
        caplog.set_level("DEBUG")
        vc = depot
        vc.guest_ops.exit_codes = {"googlechrome": 1}
        real_start = vc.guest_ops.StartProgramInGuest

        def leaky(vm, auth, spec):  # a fault message that quotes the password
            if "nginx" in spec.arguments:
                raise RuntimeError(f"login {auth.username}/{auth.password} refused")
            return real_start(vm, auth, spec)

        vc.guest_ops.StartProgramInGuest = leaky
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(SW_TEMPLATE), {}))
        assert result.status == "partial"
        passwords = {p for p in vc.guest_ops.passwords_seen if p}
        assert len(passwords) == 2  # one Windows Administrator, one Linux install user
        dumped = json.dumps(result.__dict__, default=str)
        for pw in passwords:
            assert pw not in caplog.text
            assert pw not in dumped
        assert any("***" in e for e in result.errors)
        assert "software googlechrome failed, exit 1" in " ".join(result.errors)

    def test_a_hung_install_times_out_and_the_range_is_partial(self, depot, monkeypatch):
        vc = depot
        vc.guest_ops.hang = {"googlechrome"}
        clock = [0.0]
        real = mod.time.monotonic
        monkeypatch.setattr(guest_mod, "time", SimpleNamespace(
            monotonic=lambda: real() + clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + 50)))
        monkeypatch.setattr(mod, "TN_SOFTWARE_INSTALL_TIMEOUT", 600)
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(SW_TEMPLATE), {}))
        assert result.status == "partial"
        sw = _vm_out(result, "dc01")["software"]
        assert sw["status"] == "partial"
        assert sw["packages"][1] == {"name": "googlechrome", "status": "timeout", "exit_code": None}
        assert vc.guest_ops.terminated  # the hung process was stopped
        assert any("googlechrome timeout" in e for e in result.errors)
        assert len(result.vms) == 3  # every VM is still there

    def test_skipped_with_a_warning_without_an_uplink(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "TN_DEPOT_URL", DEPOT)
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(SW_TEMPLATE), {}))
        assert result.status == "ok", result.errors
        assert any("VSPHERE_RANGE_UPLINK_NETWORK is not set" in w for w in result.warnings)
        assert vc.guest_ops.started == []
        assert _vm_out(result, "dc01")["software"]["status"] == "skipped"
        web = vc.vms[_vm_out(result, "web01")["vm_id"]]
        assert "users" not in _userdata(web)  # no install user was ever created

    def test_skipped_with_a_warning_without_a_depot(self, uplink):
        result = _run(_prov(uplink).provision(RANGE_ID, _rendered(SW_TEMPLATE), {}))
        assert result.status == "ok", result.errors
        assert any("TN_DEPOT_URL is not set" in w for w in result.warnings)
        assert uplink.guest_ops.started == []

    def test_windows_waits_for_sysprep_to_finish(self, depot, monkeypatch):
        vc = depot
        polls = {"n": 0}
        real_validate = vc.guest_ops.ValidateCredentialsInGuest

        def validate(vm, auth):
            polls["n"] += 1
            return real_validate(vm, auth)

        vc.guest_ops.ValidateCredentialsInGuest = validate
        states = iter(["TOOLSDEPLOYPKG_PENDING", "TOOLSDEPLOYPKG_RUNNING"])

        class Guest:
            guestOperationsReady = True  # noqa: N815

            @property
            def customizationInfo(self):  # noqa: N802
                return SimpleNamespace(customizationStatus=next(states, "TOOLSDEPLOYPKG_SUCCEEDED"))

        orig_init = FakeVM.__init__

        def init(self, *a, **kw):
            orig_init(self, *a, **kw)
            self.guest = Guest()

        monkeypatch.setattr(FakeVM, "__init__", init)
        tpl = {**SW_TEMPLATE, "nodes": [SW_TEMPLATE["nodes"][0], SW_TEMPLATE["nodes"][2]]}
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(tpl), {}))
        assert result.status == "ok", result.errors
        assert polls["n"] == 1  # no login attempt while customization was still running
