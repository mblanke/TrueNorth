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
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import yaml
from pyVmomi import vim, vmodl
from worker import pfsense_config, render, vlan_pool
from worker.provisioners import vsphere_api as mod
from worker.provisioners import vsphere_guest as guest_mod
from worker.provisioners import vsphere_infra as infra
from worker.provisioners import vsphere_roles as roles_mod

RANGE_ID = "abcdef12-3456-7890-abcd-ef1234567890"
R8 = RANGE_ID[:8]
GIB = 1024**3
MGMT = "dPG-TN-MGMT"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def worker_reserves(monkeypatch):
    """Play the worker's part before every build: reserve what ``allocation_needs`` declares.

    The provisioner no longer picks VLANs or addresses itself (worker/range_alloc.py
    reserves them on network_reservations first). Here the lowest free pool values are
    given, skipping ``used_vlans`` / ``used_uplink_ips``, which stand for what other
    ranges hold; values a test passes in are kept. Going through allocation_needs also
    checks it declares every VLAN and address the build then uses.
    """
    real = mod.VsphereAPIProvisioner.provision

    async def provision(self, range_id, template, allocations):
        allocations = dict(allocations)
        taken = {"vlan": {str(v) for v in allocations.pop("used_vlans", ())},
                 "uplink_ip": {str(v) for v in allocations.pop("used_uplink_ips", ())}}
        for need in self.allocation_needs(range_id, template):
            if need.key in allocations:
                continue
            free = [v for v in need.pool if v not in taken[need.kind]]
            if len(free) < len(need.holders):
                raise AssertionError(f"test pool too small for {need}")
            got = dict(zip(need.holders, free, strict=False))
            allocations[need.key] = got[need.holders[0]] if need.single else got
        return await real(self, range_id, template, allocations)

    monkeypatch.setattr(mod.VsphereAPIProvisioner, "provision", provision)


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
        self.config = SimpleNamespace(
            configVersion="1", vspanSession=[],
            uplinkPortPolicy=SimpleNamespace(uplinkPortName=["dvUplink1", "dvUplink2"]))
        self.dvs_specs: list = []
        self.reconfig_fail: str | None = None

    def CreateDVPortgroup_Task(self, spec):  # noqa: N802
        self.portgroup.append(FakePortgroup(self, spec))
        return FakeTask(label=f"create-pg:{spec.name}")

    def ReconfigureDvs_Task(self, spec):  # noqa: N802
        """vCenter's vspanConfigSpec handling: add (new key), edit (by key), remove (by key)."""
        assert isinstance(spec, vim.dvs.VmwareDistributedVirtualSwitch.ConfigSpec)
        assert spec.configVersion == self.config.configVersion  # vCenter refuses a stale version
        self.dvs_specs.append(spec)
        if self.reconfig_fail:
            return FakeTask(fail=self.reconfig_fail)
        sessions = list(self.config.vspanSession)
        for op in spec.vspanConfigSpec:
            sess = op.vspanSession
            if op.operation == "add":
                assert sess.key is None and all(s.name != sess.name for s in sessions)
                sess.key = f"vspan-{next(_ids)}"
                sessions.append(sess)
            elif op.operation == "edit":
                sessions = [sess if s.key == sess.key else s for s in sessions]
            else:
                assert any(s.key == sess.key for s in sessions)
                sessions = [s for s in sessions if s.key != sess.key]
        self.config.vspanSession = sessions
        self.config.configVersion = str(int(self.config.configVersion) + 1)
        return FakeTask(label="reconfigure-dvs")


def _connect(card):
    """An earlyBinding port group: vCenter picks the NIC's dvPort and writes its key back."""
    port = getattr(card.backing, "port", None)
    if port is not None and not port.portKey:
        port.portKey = str(next(_ids))
    return card


def _apply(devices: list, changes) -> list:
    """What vCenter does with a deviceChange list: edit (new backing, same MAC), add (with a
    MAC), remove; NICs on a vDS port group get a dvPort."""
    out = list(devices)
    for change in changes or []:
        dev = change.device
        if change.operation == "add":
            dev.key = 4000 + len(out) + 100
            dev.macAddress = f"00:50:56:aa:00:{len(out):02x}"
            out.append(_connect(dev))
        elif change.operation == "edit":
            old = next(d for d in out if d.key == dev.key)
            card = vim.vm.device.VirtualVmxnet3(key=dev.key, backing=dev.backing, connectable=dev.connectable)
            card.macAddress = old.macAddress
            out = [_connect(card) if d.key == dev.key else d for d in out]
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
            customizationStatus="TOOLSDEPLOYPKG_SUCCEEDED"), toolsRunningStatus="guestToolsNotRunning", net=[],
            ipAddress=None)
        self.runtime = SimpleNamespace(powerState="poweredOff")  # the Web Services view (legacy dialect)
        self.clone_specs: list = []
        self.reconfig_specs: list = []
        self.customize_specs: list = []
        self.clone_fail: str | None = None

    def PowerOnVM_Task(self):  # noqa: N802
        self.runtime.powerState = "poweredOn"
        return FakeTask(label=f"power-on:{self.name}")

    def PowerOffVM_Task(self):  # noqa: N802
        self.runtime.powerState = "poweredOff"
        return FakeTask(label=f"power-off:{self.name}")

    def Destroy_Task(self):  # noqa: N802
        FAKE.vms.pop(self._moId, None)
        for folder in _folders(FAKE.dc.vmFolder):
            if self in folder.childEntity:
                folder.childEntity.remove(self)
        return FakeTask(label=f"destroy-vm:{self.name}")

    def CloneVM_Task(self, folder, name, spec):  # noqa: N802
        self.clone_specs.append((folder, name, spec))
        if self.clone_fail:
            return FakeTask(fail=self.clone_fail)
        vm = FakeVM(name, _apply([_card(d.key, "x") for d in self.config.hardware.device], spec.config.deviceChange))
        if not FAKE.clone_drops_annotation:  # vCenter applies the clone spec's annotation; vcsim does not
            vm.config.annotation = spec.config.annotation
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


class FreshHardware:
    """A template's hardware as pyVmomi serves it: new device objects on every read, so two
    clones of one template built side by side never share (and re-point) one NIC object."""

    def __init__(self, network: str):
        self.network = network

    @property
    def device(self) -> list:
        return [_card(4000, self.network)]


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
            tmpl = FakeVM(name, [], template=True)
            tmpl.config.hardware = FreshHardware(MGMT)
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
        self.clone_drops_annotation = False

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

        collector = SimpleNamespace(CreatePropertyCollector=lambda: SimpleNamespace(
            DestroyPropertyCollector=lambda: None))
        return SimpleNamespace(_stub=object(), content=SimpleNamespace(
            rootFolder=object(), viewManager=SimpleNamespace(CreateContainerView=view), propertyCollector=collector,
            guestOperationsManager=self.guest_ops))

    def wait(self, task, si, timeout):
        # mod._wait_task itself (private collector, bounded) is tested in TestWaitTask.
        assert timeout, "an unbounded wait can outlive Celery's visibility timeout"
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
    monkeypatch.setattr(mod, "_wait_task", lambda task, si, timeout: FAKE.wait(task, si, timeout))
    for name, value in {
        "VSPHERE_URL": "https://vcsa.test", "VSPHERE_DATACENTER": "DC-Lab", "VSPHERE_CLUSTER": "CL-Lab",
        "VSPHERE_CONTENT_LIBRARY": "", "VSPHERE_NETWORK": "", "VSPHERE_MGMT_NETWORK": MGMT,
        "VSPHERE_RANGE_SWITCH_MODE": "vds", "VSPHERE_RANGE_DVS": "vDS-10G", "VSPHERE_VLAN_POOL": "100-199",
        "VSPHERE_PLACEMENT": "spread", "VSPHERE_EXCLUDE_HOSTS": "esx01.truenorth.lab",
        "VSPHERE_MAX_VCPU_PER_THREAD": 4.0, "VSPHERE_TOOLS_TIMEOUT": 0,
        "VSPHERE_RANGE_UPLINK_NETWORK": "", "VSPHERE_RANGE_UPLINK_POOL": "", "VSPHERE_RANGE_UPLINK_GATEWAY": "",
        "VSPHERE_RANGE_UPLINK_PREFIX": 24, "TN_DEPOT_URL": "", "TN_DEPOT_CHOCO_FEED": "", "TN_DEPOT_APT_PROXY": "",
        "TN_SOFTWARE_INSTALL_TIMEOUT": 1800, "VSPHERE_PROVISION_BUDGET": 3300, "TN_DEPOT_PORTS": "8081,3142",
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
        assert vc.logins == 1  # four VMs reach REST together; one vCenter session between them
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
        # pfSense: no guest customization; one reconfigure that only sets its config guestinfo.
        assert fw.customize_specs == []
        (spec,) = fw.reconfig_specs
        assert not spec.deviceChange
        assert {o.key for o in spec.extraConfig} == {"guestinfo.tn.pfsense.config", "guestinfo.tn.pfsense.ifmap"}

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

    def test_linux_clones_drop_the_cloud_image_ova_vapp_config(self, vc):
        """tmpl-ubuntu-2404 came from Ubuntu's cloud-image OVA: its vApp ProductSection makes
        cloud-init take the OVF datasource and ignore the guestinfo static IPs."""
        for tmpl in vc.templates.values():
            tmpl.config.vAppConfig = vim.vApp.VmConfigInfo()
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {}))
        assert result.status == "ok", result.errors
        removed = {name: spec.config.vAppConfigRemoved
                   for tmpl in vc.templates.values() for _, name, spec in tmpl.clone_specs}
        assert removed == {f"{R8}-web01": True, f"{R8}-sensor": True,  # Linux: cloud-init
                           f"{R8}-fw": None, f"{R8}-dc01": None}  # appliance, Sysprep: left alone

    def test_template_without_vapp_config_is_cloned_as_is(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {}))
        assert result.status == "ok", result.errors
        assert all(spec.config.vAppConfigRemoved is None
                   for tmpl in vc.templates.values() for _, _, spec in tmpl.clone_specs)

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

    def test_ovf_deployed_linux_vm_drops_the_ova_vapp_config(self, vc, monkeypatch):
        """A VM deployed from the cloud-image OVA's library item carries its ProductSection too."""
        monkeypatch.setattr(mod, "VSPHERE_CONTENT_LIBRARY", "TrueNorth-Templates")
        vc.library = {"name": "TrueNorth-Templates", "items": {"tmpl-ubuntu-2404": "item-ubuntu"}}

        def handler(request):
            resp = vc.handler(request)
            if request.url.query.decode() == "action=deploy":
                vc.vms[resp.json()["resource_id"]["id"]].config.vAppConfig = vim.vApp.VmConfigInfo()
            return resp

        prov = _prov(vc)
        prov._transport = httpx.MockTransport(handler)
        result = _run(prov.provision(RANGE_ID, _rendered({**TEMPLATE, "nodes": [TEMPLATE["nodes"][1]]}), {}))
        assert result.status == "ok", result.errors
        hw, _meta = vc.vms[result.vms[0]["vm_id"]].reconfig_specs
        assert hw.vAppConfigRemoved is True  # in the reconfigure before the first power-on

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

    def test_a_build_declares_its_vlans_and_uplink_for_the_worker_to_reserve(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_RANGE_UPLINK_NETWORK", "dPG-TN-SVC")
        monkeypatch.setattr(mod, "VSPHERE_RANGE_UPLINK_POOL", "10.30.32.100-101")
        needs = {n.key: n for n in _prov(vc).allocation_needs(RANGE_ID, _rendered())}
        vlans, up = needs["physical_vlans"], needs["uplink_ip"]
        assert (vlans.kind, vlans.holders, vlans.pool[:2], vlans.single) == ("vlan", ["200", "201", "203"],
                                                                             ["100", "101"], False)
        assert vlans.domain == "vsphere:vlans"  # never derived from the vCenter's address (adversarial review)
        assert (up.kind, up.holders, up.pool, up.single) == ("uplink_ip", ["edge"], ["10.30.32.100", "10.30.32.101"],
                                                             True)
        assert up.domain == "vsphere:uplink"

    def test_the_domain_ignores_the_vcenter_address_and_switch(self, vc, monkeypatch):
        p = _prov(vc)
        before = p.allocation_needs(RANGE_ID, _rendered())[0].domain
        p._base_url, p._dvs_name = "https://10.0.0.5", "vDS-other"
        assert p.allocation_needs(RANGE_ID, _rendered())[0].domain == before
        monkeypatch.setenv("VSPHERE_ALLOCATION_DOMAIN", "site-b")  # sites that share no VLAN segment
        assert p.allocation_needs(RANGE_ID, _rendered())[0].domain == "site-b:vlans"

    def test_an_unreserved_vlan_is_refused_not_picked(self, vc):
        # 200 reserved, 201 and 203 not (the fixture fills only keys that are missing altogether)
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {"physical_vlans": {200: 100}}))
        assert result.status == "failed" and "no reservation" in result.errors[0]
        assert not vc.vms and vc.dvs.portgroup == []


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

    def test_an_unreserved_uplink_address_is_refused_not_picked(self, uplink):
        result = _run(_prov(uplink).provision(RANGE_ID, _rendered(), {"uplink_ip": ""}))
        assert result.status == "failed" and "no reservation" in result.errors[0]
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

        on_dc = [s for s in vc.guest_ops.started if s[0] == f"{R8}-dc01"]
        # `iis` is a Windows Server role: its feature install runs first (TestWindowsRoles).
        assert on_dc[0][2] == roles_mod.POWERSHELL
        win = [s for s in on_dc if s[2] != roles_mod.POWERSHELL]
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
        # Only the `iis` role's feature install (it needs no depot); no software.
        assert [s[2] for s in vc.guest_ops.started] == [roles_mod.POWERSHELL]
        assert _vm_out(result, "dc01")["software"]["status"] == "skipped"
        web = vc.vms[_vm_out(result, "web01")["vm_id"]]
        assert "users" not in _userdata(web)  # no install user was ever created

    def test_skipped_with_a_warning_without_a_depot(self, uplink):
        result = _run(_prov(uplink).provision(RANGE_ID, _rendered(SW_TEMPLATE), {}))
        assert result.status == "ok", result.errors
        assert any("TN_DEPOT_URL is not set" in w for w in result.warnings)
        assert [s[2] for s in uplink.guest_ops.started] == [roles_mod.POWERSHELL]  # the iis role only

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
        # No login attempt while customization was still running: one by the iis role
        # install once it had finished, one by the software install after it.
        assert polls["n"] == 2


# --------------------------------------------------------------------------- #
# Windows Server roles over guest operations (vsphere_roles.py)
# --------------------------------------------------------------------------- #

WS22 = "windows-server-2022"
ROLE_TEMPLATE = {**TEMPLATE, "nodes": [
    TEMPLATE["nodes"][0],
    {"id": "files", "os": WS22, "vlan": "victim_network", "services": ["smb", "dfs", "rdp"]},
    {"id": "dc01", "os": WS22, "vlan": "victim_network", "services": ["active_directory", "dns"],
     "ad_forest": "corp.range.local"},
    {"id": "exch", "os": WS22, "vlan": "victim_network", "services": ["exchange", "owa"]},
]}


def _scripts(vc, vm_name: str) -> list[str]:
    return [base64.b64decode(s[3].rsplit(" ", 1)[1]).decode("utf-16-le")
            for s in vc.guest_ops.started if s[0] == vm_name and s[2] == roles_mod.POWERSHELL]


class TestWindowsRoles:
    def test_roles_install_as_the_sysprep_administrator_with_no_depot(self, vc):
        vc.guest_ops.exit_codes = {}  # every script exits 0: no reboot is asked for
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ROLE_TEMPLATE), {}))
        files = _vm_out(result, "files")
        assert files["roles"] == {"file": {"status": "ok", "detail": "installed"}}
        (script,) = _scripts(vc, f"{R8}-files")
        assert "Install-WindowsFeature -Name FS-FileServer,FS-DFS-Namespace,FS-DFS-Replication" in script
        assert {s[1] for s in vc.guest_ops.started} == {"Administrator"}
        # The login worked, so it was the password Sysprep set; it is gone from the result.
        dumped = json.dumps(result.__dict__, default=str)
        assert all(p not in dumped for p in vc.guest_ops.passwords_seen if p)

    def test_a_forest_promotion_that_fails_makes_the_range_partial(self, vc):
        # Install-ADDSForest must exit 3010 (reboot pending); the fake guest exits 0.
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ROLE_TEMPLATE), {}))
        assert result.status == "partial"
        dc = _vm_out(result, "dc01")["roles"]
        assert dc["ad-ds"]["status"] == "failed" and dc["dns"]["status"] == "ok"
        features, forest = _scripts(vc, f"{R8}-dc01")
        assert "Install-WindowsFeature -Name AD-Domain-Services,DNS," in features
        assert "Install-ADDSForest -DomainName 'corp.range.local' -DomainNetbiosName 'CORP'" in forest
        assert f"VM {R8}-dc01: role ad-ds failed" in " ".join(result.errors)
        assert len(result.vms) == 4  # every VM is still there

    def test_an_unregistered_role_image_builds_the_bare_os_and_says_so(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ROLE_TEMPLATE), {}))
        exch = _vm_out(result, "exch")
        assert exch["roles"]["exchange"]["status"] == "skipped"
        assert "srv2022-exchange2019" in exch["roles"]["exchange"]["detail"]
        assert not _scripts(vc, f"{R8}-exch")  # an image role has no guest work
        clone = vc.templates["tmpl-win2022"].clone_specs
        assert any(name == f"{R8}-exch" for _, name, _ in clone)

    def test_role_sizing_floor_reaches_the_clone_spec(self, vc):
        _run(_prov(vc).provision(RANGE_ID, _rendered(ROLE_TEMPLATE), {}))
        spec = next(s for _, name, s in vc.templates["tmpl-win2022"].clone_specs if name == f"{R8}-exch")
        assert (spec.config.numCPUs, spec.config.memoryMB) == (4, 16384)


# --------------------------------------------------------------------------- #
# pfSense: the per-range config.xml in guestinfo
# --------------------------------------------------------------------------- #


def _pfsense_guestinfo(vm) -> tuple[ET.Element, str]:
    extra = {o.key: o.value for spec in vm.reconfig_specs for o in (spec.extraConfig or [])}
    root = ET.fromstring(pfsense_config.decode(extra["guestinfo.tn.pfsense.config"]))
    return root, extra["guestinfo.tn.pfsense.ifmap"]


class TestPfsenseConfigDelivery:
    def test_edge_firewall_config_has_the_wan_zones_and_depot_rules(self, depot):
        vc = depot
        tpl = {**TEMPLATE, "network": {**TEMPLATE["network"], "firewall_rules": [
            {"name": "attacker-to-victim", "src": "attacker_infra", "dst": "victim_network", "action": "allow"},
            {"name": "span", "src": "*", "dst": "network_monitoring", "action": "mirror"},
            {"name": "ghost", "src": "attacker_infra", "dst": "nowhere", "action": "allow"},
        ]}}
        # worker.tasks passes the template through with the rendered vms/networks added.
        built = {**_rendered(tpl), "network": tpl["network"]}
        result = _run(_prov(vc).provision(RANGE_ID, built, {"uplink_ip": "10.30.32.101"}))
        assert result.status == "ok", result.errors
        fw = next(v for v in vc.vms.values() if v.name == f"{R8}-fw")
        root, ifmap = _pfsense_guestinfo(fw)

        # NIC order: WAN on the uplink, then the zones; the ifmap names each by the clone's MAC.
        macs = [c.macAddress.lower() for c in infra.nic_cards(fw.config.hardware.device)]
        assert ifmap == " ".join(f"vmx{i}={m}" for i, m in enumerate(macs)) and len(macs) == 4
        ifs = {el.tag: (el.findtext("if"), el.findtext("ipaddr"), el.findtext("subnet"))
               for el in root.find("interfaces")}
        assert ifs == {"wan": ("vmx0", "10.30.32.101", "24"), "lan": ("vmx1", "10.60.200.1", "24"),
                       "opt1": ("vmx2", "10.60.201.1", "24"), "opt2": ("vmx3", "10.60.203.1", "24")}
        assert root.findtext("gateways/gateway_item/gateway") == "10.30.32.1"
        assert root.findtext("aliases/alias[name='TN_DEPOT']/address") == "10.30.32.10"
        assert root.findtext("aliases/alias[name='TN_DEPOT_PORTS']/address") == "8081 3142"
        descrs = [r.findtext("descr") for r in root.find("filter")]
        assert "attacker-to-victim" in descrs and not any(d.startswith("span") for d in descrs)
        assert root.find("system/user") is None  # the guest keeps the template's users

        out = _vm_out(result, "fw")["pfsense"]
        assert out["delivery"] == "guestinfo" and len(out["config_sha256"]) == 64
        assert [i["name"] for i in out["interfaces"]] == ["wan", "lan", "opt1", "opt2"]
        assert any("ghost" in w and "unknown destination zone" in w for w in result.warnings)
        assert not any("span" in w for w in result.warnings)

    def test_isolated_range_firewall_has_no_wan_uplink(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {}))
        assert result.status == "ok", result.errors
        fw = next(v for v in vc.vms.values() if v.name == f"{R8}-fw")
        root, _ = _pfsense_guestinfo(fw)
        assert root.findtext("interfaces/wan/ipaddr") == "10.60.200.1"
        assert root.find("gateways") is None
        assert root.findtext("nat/outbound/mode") == "disabled"
        assert not [r for r in root.find("filter") if r.findtext("floating")]

    def test_other_vms_get_no_pfsense_keys(self, vc):
        _run(_prov(vc).provision(RANGE_ID, _rendered(), {}))
        for vm in vc.vms.values():
            if vm.name.endswith("-fw"):
                continue
            keys = {o.key for spec in vm.reconfig_specs for o in (spec.extraConfig or [])}
            assert not any(k.startswith("guestinfo.tn.pfsense") for k in keys), vm.name


# --------------------------------------------------------------------------- #
# Bounded vCenter task waits
# --------------------------------------------------------------------------- #


def _update(version: str, **props):
    changes = [SimpleNamespace(name=f"info.{k}", val=v) for k, v in props.items()]
    return SimpleNamespace(version=version, filterSet=[SimpleNamespace(objectSet=[SimpleNamespace(changeSet=changes)])])


class FakeCollector:
    """A private PropertyCollector: scripted WaitForUpdatesEx results on a fake clock."""

    def __init__(self, updates, clock):
        self.updates = list(updates)
        self.clock = clock
        self.waits: list[tuple[str, int]] = []
        self.filters: list = []
        self.destroyed = False

    def CreateFilter(self, spec, partialUpdates):  # noqa: N802, N803
        self.filters.append(spec)

    def WaitForUpdatesEx(self, version, options):  # noqa: N802
        self.waits.append((version, options.maxWaitSeconds))
        nxt = self.updates.pop(0) if self.updates else None
        if nxt is None:  # nothing changed: vCenter returns after maxWaitSeconds
            self.clock[0] += options.maxWaitSeconds
        return nxt

    def DestroyPropertyCollector(self):  # noqa: N802
        self.destroyed = True


class TestWaitTask:
    def _si(self, *collectors):
        made = iter(collectors)
        return SimpleNamespace(content=SimpleNamespace(propertyCollector=SimpleNamespace(
            CreatePropertyCollector=lambda: next(made))))

    def test_returns_on_success_and_destroys_its_collector(self):
        clock = [0.0]
        pc = FakeCollector([_update("1", state="queued"), None, _update("2", state="running"),
                            _update("3", state="success")], clock)
        mod._wait_task(vim.Task("task-1", None), self._si(pc), 600, clock=lambda: clock[0])
        assert pc.destroyed
        (spec,) = pc.filters
        assert spec.objectSet[0].obj._moId == "task-1"
        assert sorted(spec.propSet[0].pathSet) == ["info.error", "info.state"]
        assert [v for v, _ in pc.waits] == ["", "1", "1", "2"]  # each call resumes from the last version

    def test_a_task_that_never_finishes_times_out(self):
        clock = [0.0]
        pc = FakeCollector([_update("1", state="running")], clock)
        with pytest.raises(mod.TaskTimeoutError, match="still running after 100s"):
            mod._wait_task(vim.Task("task-2", None), self._si(pc), 100, clock=lambda: clock[0], poll_seconds=30)
        # Every server-side wait is bounded, and the last one only as long as the time left.
        assert [s for _, s in pc.waits] == [30, 30, 30, 30, 10]
        assert clock[0] == 100 and pc.destroyed

    def test_a_failed_task_raises_its_fault(self):
        clock = [0.0]
        fault = vim.fault.DuplicateName(name="x")
        pc = FakeCollector([_update("1", state="error", error=fault)], clock)
        with pytest.raises(vim.fault.DuplicateName):
            mod._wait_task(vim.Task("task-3", None), self._si(pc), 600, clock=lambda: clock[0])
        assert pc.destroyed

    def test_each_wait_has_a_collector_of_its_own(self):
        clock = [0.0]
        a = FakeCollector([_update("1", state="success")], clock)
        b = FakeCollector([_update("1", state="success")], clock)
        si = self._si(a, b)
        mod._wait_task(vim.Task("task-4", None), si, 60, clock=lambda: clock[0])
        mod._wait_task(vim.Task("task-5", None), si, 60, clock=lambda: clock[0])
        assert a.filters[0].objectSet[0].obj._moId == "task-4" and b.filters[0].objectSet[0].obj._moId == "task-5"
        assert a.destroyed and b.destroyed


# --------------------------------------------------------------------------- #
# Port mirroring: the templates' ``action: mirror`` rules as vDS sessions
# --------------------------------------------------------------------------- #

CONTENT = __import__("pathlib").Path(__file__).resolve().parents[2] / "content" / "ranges"
MIRROR_TEMPLATE = {**TEMPLATE, "network": {**TEMPLATE["network"], "firewall_rules": [
    {"name": "attacker-to-victim", "src": "attacker_infra", "dst": "victim_network", "action": "allow"},
    {"name": "tap-victim", "src": "victim_network", "dst": "network_monitoring", "action": "mirror"},
]}}


def _mirror_build(template=MIRROR_TEMPLATE) -> dict:
    return {**_rendered(template), "network": template["network"]}


def _sessions(vc) -> dict:
    return {s.name: s for s in vc.dvs.config.vspanSession}


def _nic_ports(vc, node: str, zone_pg: str) -> list[str]:
    """dvPort keys the node's NICs hold on the port group named ``zone_pg``."""
    vm = next(v for v in vc.vms.values() if v.name == f"{R8}-{node}")
    pg_key = next(pg.key for pg in vc.dvs.portgroup if pg.name == zone_pg)
    return [p for g, p in infra.nic_port_keys(vm.config.hardware.device) if g == pg_key]


class TestMirrorPlan:
    NETS = [{"name": n, "vlan_id": v} for n, v in
            (("attacker_infra", 200), ("victim_network", 201), ("soc_tools", 202), ("network_monitoring", 203))]

    def test_one_plan_per_destination_zone(self):
        rules = [{"name": "a", "src": "victim_network", "dst": "network_monitoring", "action": "mirror"},
                 {"name": "b", "src": "attacker_infra", "dst": "network_monitoring", "action": "mirror"}]
        (plan,), notes = infra.plan_mirrors(rules, self.NETS)
        assert notes == []
        assert plan["dst"] == "network_monitoring" and plan["dst_vlan"] == 203 and plan["rules"] == ["a", "b"]
        assert plan["sources"] == [("victim_network", 201), ("attacker_infra", 200)]
        assert plan["rspan_key"] == infra.RSPAN_LOGICAL_BASE + 203

    def test_star_is_every_zone_but_the_mirror_destinations(self):
        (plan,), _ = infra.plan_mirrors([{"src": "*", "dst": "network_monitoring", "action": "mirror"}], self.NETS)
        assert [z for z, _ in plan["sources"]] == ["attacker_infra", "victim_network", "soc_tools"]

    def test_unknown_zones_are_skipped_with_a_note(self):
        rules = [{"name": "x", "src": "victim_network", "dst": "nowhere"},
                 {"name": "y", "src": "nowhere", "dst": "network_monitoring"},
                 {"name": "z", "src": "network_monitoring", "dst": "network_monitoring"}]
        plans, notes = infra.plan_mirrors(rules, self.NETS)
        assert plans == []
        assert [n.split(":")[0] for n in notes] == ["mirror rule 'x'", "mirror rule 'y'", "mirror rule 'z'"]

    def test_only_mirror_actions_are_mirror_rules(self):
        tpl = {"network": {"firewall_rules": [{"action": "allow"}, {"action": "MIRROR", "dst": "x"}, "junk"]}}
        assert infra.mirror_rules(tpl) == [{"action": "MIRROR", "dst": "x"}]
        assert infra.mirror_rules({}) == []

    # colosseum (span_domain, span_dmz) joins this list when its template lands in content/ranges.
    @pytest.mark.parametrize(("name", "expected"), [
        ("soc-training", {"network_monitoring": ["attacker_infra", "victim_network", "soc_tools", "management"]}),
    ])
    def test_shipped_templates(self, name, expected):
        tpl = yaml.safe_load((CONTENT / name / "template.yaml").read_text())
        nets = render.render_topology(tpl, RANGE_ID, lambda _: "tmpl")["network_definitions"]
        plans, notes = infra.plan_mirrors(infra.mirror_rules(tpl), nets)
        assert notes == []
        assert {p["dst"]: [z for z, _ in p["sources"]] for p in plans} == expected


class TestMirrorSessions:
    def test_build_creates_local_and_remote_sessions(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _mirror_build(), {"used_vlans": [100, 101]}))
        assert result.status == "ok", result.errors
        assert result.warnings == []

        # The RSPAN VLAN is reserved from the pool like a zone's, but gets no port group.
        rspan = next(n for n in result.networks if n.get("rspan"))
        assert rspan["vlan_id"] == infra.RSPAN_LOGICAL_BASE + 203 and "portgroup" not in rspan
        zone_vlans = {n["physical_vlan"] for n in result.networks if not n.get("rspan")}
        assert rspan["physical_vlan"] == 105 and rspan["physical_vlan"] not in zone_vlans
        assert sorted(pg.name for pg in vc.dvs.portgroup) == [f"tn-{R8}-v{v}" for v in (102, 103, 104)]

        victim_pg, monitor_pg = f"tn-{R8}-v103", f"tn-{R8}-v104"
        sources = sorted(_nic_ports(vc, "web01", victim_pg) + _nic_ports(vc, "dc01", victim_pg)
                         + _nic_ports(vc, "fw", victim_pg))
        sensor = _nic_ports(vc, "sensor", monitor_pg)
        assert len(sources) == 3 and len(sensor) == 1

        name = f"tn-{R8}-network_monitoring"
        sessions = _sessions(vc)
        assert sorted(sessions) == [name, f"{name}-rx"]
        local, remote = sessions[name], sessions[f"{name}-rx"]
        assert local.sessionType == "mixedDestMirror" and local.enabled
        assert sorted(local.sourcePortReceived.portKey) == sources  # each frame once: from its sender
        assert local.sourcePortTransmitted is None
        assert local.destinationPort.portKey == sensor
        assert local.destinationPort.uplinkPortName == ["dvUplink1"]
        assert local.encapsulationVlanId == 105 and local.stripOriginalVlan is True
        assert local.normalTrafficAllowed is True
        assert remote.sessionType == "remoteMirrorDest"
        assert remote.sourcePortReceived.vlans == [105] and remote.destinationPort.portKey == sensor
        assert remote.stripOriginalVlan is True
        # Both in one reconfigure, against the switch's current config version.
        (spec,) = vc.dvs.dvs_specs
        assert [op.operation for op in spec.vspanConfigSpec] == ["add", "add"]

        assert [(m["name"], m["type"]) for m in result.mirrors] == [
            (name, "mixedDestMirror"), (f"{name}-rx", "remoteMirrorDest")]
        assert result.mirrors[0]["source_zones"] == ["victim_network"]
        assert result.mirrors[0]["source_ports"] == sources and result.mirrors[0]["destination_ports"] == sensor
        assert result.mirrors[0]["rspan_vlan"] == 105 and result.mirrors[0]["uplink"] == "dvUplink1"
        json.dumps(result.mirrors)  # stored in provisioner_output as JSON

    def test_reserved_rspan_vlan_is_used(self, vc):
        reserved = {200: 150, 201: 151, 203: 152, infra.RSPAN_LOGICAL_BASE + 203: 160}
        result = _run(_prov(vc).provision(RANGE_ID, _mirror_build(), {"physical_vlans": reserved}))
        assert result.status == "ok", result.errors
        assert _sessions(vc)[f"tn-{R8}-network_monitoring"].encapsulationVlanId == 160

    def test_direction_and_uplink_are_configurable(self, vc):
        prov = _prov(vc)
        prov._mirror_direction, prov._mirror_uplink = "both", "dvUplink2"
        assert _run(prov.provision(RANGE_ID, _mirror_build(), {})).status == "ok"
        local = _sessions(vc)[f"tn-{R8}-network_monitoring"]
        assert local.sourcePortReceived.portKey == local.sourcePortTransmitted.portKey
        assert local.destinationPort.uplinkPortName == ["dvUplink2"]

    def test_bad_direction_falls_back_to_received(self, vc):
        prov = _prov(vc)
        prov._mirror_direction = "sideways"
        result = _run(prov.provision(RANGE_ID, _mirror_build(), {}))
        assert any("VSPHERE_MIRROR_DIRECTION" in w for w in result.warnings)
        assert _sessions(vc)[f"tn-{R8}-network_monitoring"].sourcePortReceived is not None

    def test_a_switch_without_uplinks_mirrors_locally_only(self, vc):
        vc.dvs.config.uplinkPortPolicy = None
        result = _run(_prov(vc).provision(RANGE_ID, _mirror_build(), {}))
        assert result.status == "ok", result.errors
        assert sorted(_sessions(vc)) == [f"tn-{R8}-network_monitoring"]
        local = _sessions(vc)[f"tn-{R8}-network_monitoring"]
        assert local.destinationPort.uplinkPortName == [] and local.encapsulationVlanId is None
        assert any("no uplinks" in w for w in result.warnings)

    def test_retry_replaces_the_sessions_in_place(self, vc):
        _run(_prov(vc).provision(RANGE_ID, _mirror_build(), {}))
        keys = {n: s.key for n, s in _sessions(vc).items()}
        _run(_prov(vc).provision(RANGE_ID, _mirror_build(), {}))
        assert {n: s.key for n, s in _sessions(vc).items()} == keys
        assert [op.operation for op in vc.dvs.dvs_specs[-1].vspanConfigSpec] == ["edit", "edit"]

    def test_no_sensor_nic_is_a_warning_not_a_session(self, vc):
        tpl = {**MIRROR_TEMPLATE, "nodes": [n for n in TEMPLATE["nodes"] if n["id"] != "sensor"]}
        tpl["nodes"][0] = {**tpl["nodes"][0], "interfaces": tpl["nodes"][0]["interfaces"][:2]}
        result = _run(_prov(vc).provision(RANGE_ID, _mirror_build(tpl), {}))
        assert result.status == "ok", result.errors
        assert _sessions(vc) == {} and result.mirrors == []
        assert any("no VM has a NIC on 'network_monitoring'" in w for w in result.warnings)

    def test_refused_session_makes_the_range_partial(self, vc):
        vc.dvs.reconfig_fail = "A specified parameter was not correct: destinationPort"
        result = _run(_prov(vc).provision(RANGE_ID, _mirror_build(), {}))
        assert result.status == "partial" and len(result.vms) == 4
        assert result.errors == [f"mirror tn-{R8}-network_monitoring: A specified parameter was not correct: "
                                 "destinationPort"]
        assert result.mirrors == []

    def test_standard_switches_skip_mirroring(self, vc):
        prov = _prov(vc)
        prov._switch_mode = "vss"
        result = _run(prov.provision(RANGE_ID, _mirror_build(), {}))
        assert result.status == "ok", result.errors
        assert not any(n.get("rspan") for n in result.networks) and result.mirrors == []
        assert any("port mirroring needs VSPHERE_RANGE_SWITCH_MODE=vds" in w for w in result.warnings)

    def test_destroy_removes_the_sessions_idempotently(self, vc):
        other = infra.vspan_sessions("ffffffff-other", {"dst": "x", "sources": [("y", 1)]}, ["9"], ["8"], None, 0)
        infra.ensure_vspan_sessions(vc.dvs, other, lambda t: None)
        result = _run(_prov(vc).provision(RANGE_ID, _mirror_build(), {}))
        output = {"vms": result.vms, "networks": result.networks, "mirrors": result.mirrors}
        destroyed = _run(_prov(vc).destroy(RANGE_ID, output))
        assert destroyed.status == "ok", destroyed.errors
        assert sorted(_sessions(vc)) == ["tn-ffffffff-x"]  # another range's session is not touched
        removal = vc.dvs.dvs_specs[-1]
        assert [op.operation for op in removal.vspanConfigSpec] == ["remove", "remove"]
        assert vc.dvs.portgroup == []  # the port groups went after the sessions
        again = _run(_prov(vc).destroy(RANGE_ID, output))
        assert again.status == "ok", again.errors
        assert vc.dvs.dvs_specs[-1] is removal  # nothing left to remove: no reconfigure

    def test_destroy_removes_unrecorded_sessions_too(self, vc):
        """A build that died after making its sessions recorded none; destroy still finds them."""
        result = _run(_prov(vc).provision(RANGE_ID, _mirror_build(), {}))
        output = {"vms": result.vms, "networks": result.networks}
        assert _run(_prov(vc).destroy(RANGE_ID, output)).status == "ok"
        assert _sessions(vc) == {}

    def test_session_names(self):
        assert infra.mirror_session_name(RANGE_ID, "span_domain") == f"tn-{R8}-span_domain"
        assert infra.mirror_session_name(RANGE_ID, "a b/c") == f"tn-{R8}-a-b-c"
        assert infra.mirror_session_prefix(RANGE_ID) == f"tn-{R8}-"


# --------------------------------------------------------------------------- #
# Lab sessions: NICs on a leased, pre-created port group (LAB_PORT_GROUPS)
# --------------------------------------------------------------------------- #

LAB = {
    "name": "lab",
    "network": {"vlans": [{"name": "lab", "cidr": "10.20.0.0/24", "port_group": "pg-lab-07"}]},
    "nodes": [{"id": "host", "os": "ubuntu-2404", "vlan": "lab", "specs": {"cores": 1, "memory_mb": 2048}}],
}


class TestLabLeasedPortGroups:
    @pytest.fixture
    def lab_vc(self, vc, monkeypatch):
        monkeypatch.setenv("LAB_PORT_GROUPS", "pg-lab-06,pg-lab-07")
        vc.networks.append(SimpleNamespace(name="pg-lab-07", _moId="network-707", config=None))
        return vc

    def test_needs_no_vlan_and_creates_no_port_group(self, lab_vc):
        prov = _prov(lab_vc)
        assert prov.allocation_needs(RANGE_ID, _rendered(LAB)) == []
        result = _run(prov.provision(RANGE_ID, _rendered(LAB), {}))
        assert result.status == "ok", result.errors
        assert lab_vc.dvs.portgroup == []
        ((_folder, _name, spec),) = lab_vc.templates["tmpl-ubuntu-2404"].clone_specs
        nics = [c.device for c in spec.config.deviceChange if c.operation != "remove"]
        assert [n.backing.deviceName for n in nics] == ["pg-lab-07"]
        assert result.vms[0]["nics"] == [{"network": "pg-lab-07", "ip": "10.20.0.10"}]
        assert not [n for n in result.networks if n.get("portgroup")]  # nothing for destroy to remove

    def test_no_distributed_switch_is_needed(self, lab_vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_RANGE_DVS", "no-such-switch")
        result = _run(_prov(lab_vc).provision(RANGE_ID, _rendered(LAB), {}))
        assert result.status == "ok", result.errors

    def test_ovf_networks_map_onto_the_leased_port_group(self, lab_vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_CONTENT_LIBRARY", "TrueNorth-Templates")
        lab_vc.library = {"name": "TrueNorth-Templates", "items": {"tmpl-ubuntu-2404": "item-ubuntu"}}
        result = _run(_prov(lab_vc).provision(RANGE_ID, _rendered(LAB), {}))
        assert result.status == "ok", result.errors
        (_, _, body) = next(r for r in lab_vc.requests if r[1].endswith("action=deploy"))
        assert body["deployment_spec"]["network_mappings"] == {"VM Network": "network-707"}

    def test_destroy_leaves_the_leased_port_group(self, lab_vc):
        built = _run(_prov(lab_vc).provision(RANGE_ID, _rendered(LAB), {}))
        result = _run(_prov(lab_vc).destroy(RANGE_ID, {"vms": built.vms, "networks": built.networks}))
        assert result.status == "ok", result.errors
        assert not lab_vc.vms
        assert any(n.name == "pg-lab-07" for n in lab_vc.networks)

    def test_a_leased_port_group_vcenter_does_not_have_builds_nothing(self, vc, monkeypatch):
        monkeypatch.setenv("LAB_PORT_GROUPS", "pg-lab-07")  # listed, but not created on site
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(LAB), {}))
        assert result.status == "failed"
        assert any("pg-lab-07" in e and "not found" in e for e in result.errors), result.errors
        assert not vc.vms and not vc.templates["tmpl-ubuntu-2404"].clone_specs

    def test_the_management_network_is_refused_even_when_listed(self, vc, monkeypatch):
        monkeypatch.setenv("LAB_PORT_GROUPS", MGMT)
        lab = {**LAB, "network": {"vlans": [{"name": "lab", "cidr": "10.20.0.0/24", "port_group": MGMT}]}}
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(lab), {}))
        assert result.status == "failed"
        assert any("management network" in e for e in result.errors), result.errors


# --------------------------------------------------------------------------- #
# The range id in the annotation, and find_vms
# --------------------------------------------------------------------------- #


class TestRangeTag:
    def test_every_built_vm_carries_its_full_range_id(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {}))
        assert result.status == "ok", result.errors
        specs = [s for t in vc.templates.values() for (_, _, s) in t.clone_specs]
        assert len(specs) == 4
        assert {mod.annotated_range(s.config.annotation) for s in specs} == {RANGE_ID}

    def test_a_clone_that_dropped_the_tag_is_tagged_again(self, vc):
        """govmomi's vcsim ignores CloneSpec.config.annotation (found against v0.56.0)."""
        vc.clone_drops_annotation = True
        result = _run(_prov(vc).provision(RANGE_ID, _rendered({**TEMPLATE, "nodes": [TEMPLATE["nodes"][1]]}), {}))
        assert result.status == "ok", result.errors
        tag, *_rest = vc.vms[result.vms[0]["vm_id"]].reconfig_specs
        assert mod.annotated_range(tag.annotation) == RANGE_ID and not tag.deviceChange

    def test_a_clone_that_kept_the_tag_is_not_reconfigured_for_it(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _rendered({**TEMPLATE, "nodes": [TEMPLATE["nodes"][1]]}), {}))
        specs = vc.vms[result.vms[0]["vm_id"]].reconfig_specs
        assert [s.annotation for s in specs] == [None]  # only the guestinfo reconfigure

    def test_ovf_deployed_vm_is_tagged_in_its_reconfigure(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_CONTENT_LIBRARY", "TrueNorth-Templates")
        vc.library = {"name": "TrueNorth-Templates", "items": {"tmpl-ubuntu-2404": "item-ubuntu"}}
        result = _run(_prov(vc).provision(RANGE_ID, _rendered({**TEMPLATE, "nodes": [TEMPLATE["nodes"][1]]}), {}))
        hw, _meta = vc.vms[result.vms[0]["vm_id"]].reconfig_specs
        assert mod.annotated_range(hw.annotation) == RANGE_ID

    def test_find_vms_matches_names_and_tagged_range_ids_never_templates(self, vc):
        other = R8 + "-ffff-4000-8000-000000000000"  # same first 8 hex digits, another range
        prov = _prov(vc)
        prov._vm_rows_sync = lambda si: [
            ("vm-1", f"{R8}-web01", mod.range_annotation(RANGE_ID), False),
            ("vm-2", f"{R8}-gold", mod.range_annotation(RANGE_ID), True),
            ("vm-3", f"{R8}-web01", mod.range_annotation(other), False),
            ("vm-4", "dc-old-style", "", False),
        ]
        assert _run(prov.find_vms(f"{RANGE_ID}-")) == [{"vm_id": "vm-1", "name": f"{R8}-web01", "range_id": RANGE_ID}]
        assert [v["vm_id"] for v in _run(prov.find_vms(f"{R8}-"))] == ["vm-1", "vm-3"]
        assert [v["vm_id"] for v in _run(prov.find_vms("dc-"))] == ["vm-4"]
        assert [v["vm_id"] for v in _run(prov.find_vms(""))] == ["vm-1", "vm-3", "vm-4"]
        assert mod.Disconnect.call_count == 4  # one listing session per call, always closed

    def test_annotation_parsing(self):
        assert mod.annotated_range(mod.range_annotation(RANGE_ID)) == RANGE_ID
        assert mod.annotated_range("built by hand") == ""
        assert mod.annotated_range(None) == ""


# --------------------------------------------------------------------------- #
# Older vCenters and vcsim: no /api/session (legacy /rest session + Web Services)
# --------------------------------------------------------------------------- #


def _legacy(vc, token=True):
    """vcsim's REST surface (govmomi v0.56.0): no /api at all, only the legacy session."""
    vc.legacy_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        vc.legacy_calls.append((request.method, path))
        if path == "/rest/com/vmware/cis/session" and request.method == "POST":
            vc.logins += 1
            return httpx.Response(200, json={"value": f"token-{vc.logins}" if token else None})
        if path == "/rest/com/vmware/cis/session" and request.method == "DELETE":
            return httpx.Response(200)
        return httpx.Response(404, text="404 page not found")

    prov = _prov(vc)
    prov._transport = httpx.MockTransport(handler)

    def vm(si, vm_id):
        if vm_id not in vc.vms:
            raise vmodl.fault.ManagedObjectNotFound(obj=vim.VirtualMachine(vm_id, None))
        return vc.vms[vm_id]

    prov._vm = vm
    return prov


def _only_sessions(vc) -> bool:
    return {p for _, p in vc.legacy_calls} <= {"/api/session", "/rest/com/vmware/cis/session"}


class TestLegacyDialect:
    def test_login_falls_back_to_the_legacy_session_endpoint(self, vc):
        prov = _legacy(vc)
        assert _run(prov._get_session()) == "token-1"
        assert prov._dialect == "rest"
        assert vc.legacy_calls == [("POST", "/api/session"), ("POST", "/rest/com/vmware/cis/session")]

    def test_bad_credentials_never_fall_back(self, vc):
        prov = _prov(vc)
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(401, json={"error_type": "UNAUTHENTICATED"})

        prov._transport = httpx.MockTransport(handler)
        with pytest.raises(httpx.HTTPStatusError):
            _run(prov._get_session())
        assert calls == ["/api/session"] and prov._dialect == "api"

    def test_a_legacy_answer_without_a_token_is_an_error(self, vc):
        with pytest.raises(RuntimeError, match="no token"):
            _run(_legacy(vc, token=False)._get_session())

    def test_power_goes_through_the_web_services_api(self, vc):
        vm = FakeVM("a", [])
        vm.runtime.powerState = "poweredOn"
        vc.vms["vm-a"] = vm
        out = {"vms": [{"name": "a", "vm_id": "vm-a"}]}
        prov = _legacy(vc)
        stopped = _run(prov.stop(RANGE_ID, out))
        assert stopped.status == "ok" and stopped.vms_stopped == 1 and vm.runtime.powerState == "poweredOff"
        assert _run(prov.stop(RANGE_ID, out)).status == "ok"  # already off: no second task
        assert vc.waited.count("power-off:a") == 1
        started = _run(prov.start(RANGE_ID, out))
        assert started.status == "ok" and vm.runtime.powerState == "poweredOn"
        health = _run(prov.health_check(RANGE_ID, out))
        assert health.healthy and health.vm_statuses[0]["status"] == "powered_on"
        assert _only_sessions(vc)
        assert mod.Disconnect.call_count == 4  # each call's Web Services session is closed

    def test_a_vm_vcenter_no_longer_has_is_a_power_error(self, vc):
        prov = _legacy(vc)
        result = _run(prov.stop(RANGE_ID, {"vms": [{"name": "ghost", "vm_id": "vm-gone"}]}))
        assert result.status == "failed" and result.errors[0].startswith("VM ghost:")

    def test_build_and_destroy_never_touch_the_missing_rest_api(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_CONTENT_LIBRARY", "TrueNorth-Templates")  # skipped: no /api
        prov = _legacy(vc)
        result = _run(prov.provision(RANGE_ID, _rendered(), {"used_vlans": [100]}))
        assert result.status == "ok", result.errors
        assert sorted(pg.name for pg in vc.dvs.portgroup) == [f"tn-{R8}-v101", f"tn-{R8}-v102", f"tn-{R8}-v103"]
        assert all(vc.vms[v["vm_id"]].runtime.powerState == "poweredOn" for v in result.vms)
        assert all(t.clone_specs for t in vc.templates.values())
        assert _only_sessions(vc)

        output = {"vms": result.vms, "networks": result.networks}
        gone = _run(_legacy(vc).destroy(RANGE_ID, output))
        assert gone.status == "ok", gone.errors
        assert gone.resources_removed == 4 + 3 and not vc.vms and vc.dvs.portgroup == []
        again = _run(_legacy(vc).destroy(RANGE_ID, output))  # every VM already gone: still ok
        assert again.status == "ok", again.errors
        assert _only_sessions(vc)

    def test_a_vm_that_fails_half_built_is_deleted_through_soap(self, vc):
        prov = _legacy(vc)
        real = prov._customize_sync

        def customize(si, vm, vm_def):
            if vm_def["name"].endswith("dc01"):
                raise RuntimeError("customization refused")
            return real(si, vm, vm_def)

        prov._customize_sync = customize
        result = _run(prov.provision(RANGE_ID, _rendered(), {}))
        assert result.status == "partial"
        assert not [v for v in result.vms if v["name"].endswith("dc01")]
        assert not any(v.name.endswith("dc01") for v in vc.vms.values())  # removed, not left behind
        assert any("customization refused" in e for e in result.errors)
        assert _only_sessions(vc)


# --------------------------------------------------------------------------- #
# Resource pool (VSPHERE_RESOURCE_POOL)
# --------------------------------------------------------------------------- #


class TestResourcePool:
    CLUSTER = SimpleNamespace(resourcePool=SimpleNamespace(name="Resources", resourcePool=[
        SimpleNamespace(name="Infra", resourcePool=[]),
        SimpleNamespace(name="Ranges", resourcePool=[SimpleNamespace(name="TN-Ranges", resourcePool=[])]),
    ]))

    def test_default_is_the_cluster_root_pool(self):
        assert mod.VsphereAPIProvisioner()._pool(self.CLUSTER).name == "Resources"

    def test_a_named_pool_anywhere_under_the_cluster(self, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_RESOURCE_POOL", "TN-Ranges")
        assert mod.VsphereAPIProvisioner()._pool(self.CLUSTER).name == "TN-Ranges"

    def test_a_missing_pool_is_an_error_not_the_root(self, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_RESOURCE_POOL", "Nope")
        with pytest.raises(RuntimeError, match="Resource pool not found"):
            mod.VsphereAPIProvisioner()._pool(self.CLUSTER)


def test_nothing_to_touch_means_no_login(vc):
    """A range with no recorded VM ids: health, power and destroy never reach vCenter."""
    prov = _prov(vc)
    prov._transport = httpx.MockTransport(lambda request: pytest.fail(f"unexpected {request.url}"))
    out = {"vms": [{"name": "never-built"}]}
    assert _run(prov.health_check(RANGE_ID, {"vms": []})).vm_statuses == []
    assert _run(prov.stop(RANGE_ID, out)).errors == ["VM never-built: no vm_id recorded"]
    assert _run(prov.destroy(RANGE_ID, {"vms": []})).status == "ok"
    assert vc.logins == 0


# --------------------------------------------------------------------------- #
# Background noise: the management NIC on agent VMs (from the noise slot,
# tests/worker/test_vsphere_noise.py, re-done against this build path)
# --------------------------------------------------------------------------- #

NOISE_MGMT = {"vlan_id": 4001, "ip": "10.255.0.14", "prefix": 24}
NOISE_PG = "TN-Noise-Mgmt"


def _guestinfo_meta(vm) -> dict:
    """The cloud-init metadata the customize reconfigure gave a Linux VM."""
    spec = next(s for s in vm.reconfig_specs if s.extraConfig)
    extra = {o.key: o.value for o in spec.extraConfig}
    return yaml.safe_load(base64.b64decode(extra["guestinfo.metadata"]))


def _noise_vm(**over) -> dict:
    vm = {"name": f"{R8}-lnx01", "node_id": "lnx01", "role": "workstation", "os": "ubuntu-2404",
          "template_name": "tmpl-ubuntu-2404", "vlan_id": 200, "ip": "10.60.200.50", "gateway": "10.60.200.1",
          "prefix": 24, "netmask": "255.255.255.0", "mgmt": NOISE_MGMT, "dns": ["10.60.200.10"],
          "dns_search": ["corp.local"]}
    return {**vm, **over}


class TestNoiseMgmtNic:
    @pytest.fixture
    def noise_vc(self, vc):
        vc.networks.append(SimpleNamespace(name=NOISE_PG, _moId="network-4001", config=None))
        return vc

    def test_linux_agent_gets_the_mgmt_nic_last_with_a_static_unrouted_address(self, noise_vc):
        result = _run(_prov(noise_vc).provision(RANGE_ID, {"vms": [_noise_vm()], "networks": []}, {}))
        assert result.status == "ok", result.errors
        (_f, _n, spec), = noise_vc.templates["tmpl-ubuntu-2404"].clone_specs
        nics = [c.device for c in spec.config.deviceChange if c.operation != "remove"]
        assert len(nics) == 2 and nics[1].backing.deviceName == NOISE_PG
        assert isinstance(nics[1], vim.vm.device.VirtualVmxnet3)  # added, not a template card re-pointed
        vm = noise_vc.vms[result.vms[0]["vm_id"]]
        eth = _guestinfo_meta(vm)["network"]["ethernets"]
        assert eth["nic1"] == {"match": {"macaddress": vm.config.hardware.device[1].macAddress.lower()},
                               "addresses": ["10.255.0.14/24"]}  # no route, no resolver: controller only
        assert eth["nic0"]["addresses"] == ["10.60.200.50/24"]
        assert eth["nic0"]["routes"] == [{"to": "default", "via": "10.60.200.1"}]
        assert eth["nic0"]["nameservers"] == {"addresses": ["10.60.200.10"], "search": ["corp.local"]}
        (out,) = result.vms
        assert out["ip"] == "10.60.200.50" and out["mgmt_ip"] == "10.255.0.14"
        assert out["nics"][1] == {"network": NOISE_PG, "ip": "10.255.0.14"}

    def test_the_mgmt_network_needs_no_vlan_reservation(self, noise_vc):
        prov = _prov(noise_vc)
        (need,) = prov.allocation_needs(RANGE_ID, {"vms": [_noise_vm()]})
        assert need.holders == ["200"]

    def test_windows_appliances_and_non_agents_are_left_alone(self, noise_vc):
        win = _noise_vm(name=f"{R8}-ws01", node_id="ws01", os="windows-11", template_name="tmpl-win2022")
        plain = _noise_vm(name=f"{R8}-web01", node_id="web01")
        plain.pop("mgmt")
        result = _run(_prov(noise_vc).provision(RANGE_ID, {"vms": [win, plain], "networks": []}, {}))
        assert result.status == "ok", result.errors
        specs = [s for t in noise_vc.templates.values() for (_, _, s) in t.clone_specs]
        assert all(len([c for c in s.config.deviceChange if c.operation != "remove"]) == 1 for s in specs)
        assert all("mgmt_ip" not in v for v in result.vms)

    def test_missing_noise_port_group_still_builds_the_range_but_says_so(self, vc):
        plain = _noise_vm(name=f"{R8}-web01", node_id="web01")
        plain.pop("mgmt")
        result = _run(_prov(vc).provision(RANGE_ID, {"vms": [_noise_vm(), plain], "networks": []}, {}))
        assert len(result.vms) == 2 and result.status == "partial"
        assert any(NOISE_PG in e and "unreachable" in e for e in result.errors), result.errors
        assert all("mgmt_ip" not in v for v in result.vms)
        specs = [s for (_, _, s) in vc.templates["tmpl-ubuntu-2404"].clone_specs]
        assert all(len([c for c in s.config.deviceChange if c.operation != "remove"]) == 1 for s in specs)

    def test_the_noise_network_may_not_be_the_management_network(self, noise_vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_NOISE_NETWORK", MGMT)
        result = _run(_prov(noise_vc).provision(RANGE_ID, {"vms": [_noise_vm()], "networks": []}, {}))
        assert result.status == "failed" and not noise_vc.vms
        assert any("management network" in e for e in result.errors), result.errors

    def test_the_vm_ip_is_never_the_mgmt_address(self, vc, monkeypatch):
        prov = _prov(vc)

        async def interfaces(client, path):
            return [{"ip": {"ip_addresses": [{"ip_address": "10.255.0.14", "state": "PREFERRED"}]}},
                    {"ip": {"ip_addresses": [{"ip_address": "10.60.200.50", "state": "PREFERRED"}]}}]

        monkeypatch.setattr(prov, "_api_get", interfaces)
        assert _run(prov._get_vm_ip(None, "vm-1", exclude="10.255.0.14")) == "10.60.200.50"
        assert _run(prov._get_vm_ip(None, "vm-1")) == "10.255.0.14"

    def test_render_output_feeds_the_provisioner(self, noise_vc):
        """End to end on the worker side: shipped template -> render (reserved addresses) -> NICs."""
        import copy

        tpl = copy.deepcopy(yaml.safe_load((CONTENT / "red-vs-blue" / "template.yaml").read_text()))
        tpl["noise"] = {"enabled": True}
        reserved = {h: f"10.255.0.{40 + i}"
                    for i, h in enumerate(("lnx01", "lnx02", "tgen01", "ws01", "ws02", "ws03", "ws04"))}
        images = lambda alias: "tmpl-win2022" if alias.startswith("win") else "tmpl-ubuntu-2404"  # noqa: E731
        rendered = render.render_topology(tpl, RANGE_ID, images, noise_mgmt=reserved)
        agents = [v for v in rendered["vm_definitions"] if "mgmt" in v]
        nets = [n for n in rendered["network_definitions"] if n["name"] != "noise_mgmt"]
        result = _run(_prov(noise_vc).provision(RANGE_ID, {"vms": agents, "networks": nets}, {}))
        assert result.status == "ok", result.errors
        linux = [v for v in agents if not v["os"].startswith("win")]
        assert linux and sorted(v["mgmt_ip"] for v in result.vms if "mgmt_ip" in v) == sorted(
            v["mgmt"]["ip"] for v in linux)
        meta = _guestinfo_meta(noise_vc.vms[next(v["vm_id"] for v in result.vms if "mgmt_ip" in v)])
        assert meta["network"]["ethernets"]["nic0"]["nameservers"] == {
            "addresses": ["10.30.0.10", "10.30.0.11"], "search": ["corp.local"]}


def test_nic_order_follows_unit_numbers_not_device_keys():
    """govmomi's vcsim gives an added card key 205 after the template's 4000 (unit 7, then 8)."""
    template_card = vim.vm.device.VirtualE1000(key=4000, unitNumber=7)
    added = vim.vm.device.VirtualVmxnet3(key=205, unitNumber=8)
    disk = vim.vm.device.VirtualDisk(key=2000, unitNumber=0)
    assert infra.nic_cards([added, disk, template_card]) == [template_card, added]
    assert infra.nic_cards([vim.vm.device.VirtualVmxnet3(key=4001), template_card])[0] is template_card
