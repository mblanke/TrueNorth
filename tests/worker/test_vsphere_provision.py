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
from vsphere_cis_fake import FakeCis
from worker import pfsense_config, range_alloc, render, vlan_pool
from worker.provisioners import vsphere_api as mod
from worker.provisioners import vsphere_guest as guest_mod
from worker.provisioners import vsphere_infra as infra
from worker.provisioners import vsphere_macs as macs_mod
from worker.provisioners import vsphere_products as products_mod
from worker.provisioners import vsphere_readiness as readiness
from worker.provisioners import vsphere_roles as roles_mod
from worker.provisioners import vsphere_securityonion as so_mod
from worker.provisioners import vsphere_tags as tags_mod

RANGE_ID = "abcdef12-3456-7890-abcd-ef1234567890"
R8 = RANGE_ID[:8]
KEY = RANGE_ID.replace("-", "")[:12]  # vsphere_tags.range_key: new folders and tn-range tags
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
                 "uplink_ip": {str(v) for v in allocations.pop("used_uplink_ips", ())},
                 "mac": {str(v) for v in allocations.pop("used_macs", ())}}
        for need in self.allocation_needs(range_id, template):
            if need.key in allocations:
                continue
            free = list(itertools.islice((v for v in need.pool if v not in taken[need.kind]), len(need.holders)))
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
    name = runtime = hardware = summary = vm = datastore = network = configManager = config = None  # noqa: N815

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
        # host.config.network.portgroup: what vsphere_vlans.vss_usage / vss_vlan read live.
        self.config = SimpleNamespace(network=SimpleNamespace(portgroup=[]))

        def add(portgrp):
            if any(n.name == portgrp.name for n in self.network):
                raise vim.fault.AlreadyExists()
            self.network.append(SimpleNamespace(name=portgrp.name, _moId=f"network-{next(_ids)}", config=None))
            self.config.network.portgroup.append(SimpleNamespace(spec=portgrp))

        def remove(pgName):  # noqa: N803 -- pyVmomi's keyword
            if not any(n.name == pgName for n in self.network):
                raise vim.fault.NotFound()
            self.network = [n for n in self.network if n.name != pgName]
            self.config.network.portgroup = [p for p in self.config.network.portgroup if p.spec.name != pgName]

        self.configManager.networkSystem.AddPortGroup.side_effect = add
        self.configManager.networkSystem.RemovePortGroup.side_effect = remove


class FakeView(vim.view.ContainerView):
    """A real ContainerView reference (a traversal spec type-checks it) listing ``objs``."""

    view = None

    def __init__(self, objs):
        super().__init__(f"session[fake]view-{next(_ids)}", None)
        self.view = objs

    def Destroy(self):  # noqa: N802
        pass


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
        self._moId = f"dvs-{next(_ids)}"
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


def _generated_mac(default: str) -> str:
    """A MAC vCenter generates for a NIC. With ``FAKE.generated_mac`` set, every clone gets
    that one: what esx02 did on 2026-10-20 to two VMs cloned side by side."""
    return (FAKE.generated_mac if FAKE is not None and FAKE.generated_mac else None) or default


def _apply(devices: list, changes) -> list:
    """What vCenter does with a deviceChange list: edit (new backing; the MAC kept, or the
    manual one the spec sets), add (a manual MAC as given, else a generated one), remove;
    NICs on a vDS port group get a dvPort."""
    out = list(devices)
    for change in changes or []:
        dev = change.device
        # ``FAKE.ignore_manual``: a server that does not honour a manual MAC (never seen; the
        # provisioner's read-back check is for it).
        manual = dev.addressType == "manual" and dev.macAddress and not (FAKE is not None and FAKE.ignore_manual)
        if change.operation == "add":
            dev.key = 4000 + len(out) + 100
            if not manual:
                dev.addressType = "generated"
                dev.macAddress = _generated_mac(f"00:50:56:aa:00:{len(out):02x}")
            out.append(_connect(dev))
        elif change.operation == "edit":
            old = next(d for d in out if d.key == dev.key)
            card = vim.vm.device.VirtualVmxnet3(key=dev.key, backing=dev.backing, connectable=dev.connectable,
                                                addressType=dev.addressType if manual else "assigned")
            card.macAddress = dev.macAddress if manual else _generated_mac(old.macAddress)
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
        if expected.startswith("hashed:"):  # cloud-init got a SHA-512-crypt hash (always, ADR 0012)
            from worker.provisioners.sha512_crypt import sha512_crypt  # noqa: PLC0415

            ok = sha512_crypt(auth.password, expected[7:].split("$")[2]) == expected[7:]
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


class FakeGuest:
    """``vm.guest`` as VMware Tools reports it: Tools runs in a powered-on Linux or Windows
    guest (one the build configured) unless ``tools`` is False; its addresses are the ones
    its configuration gives the NICs it really has (FakeVM.guest_ips)."""

    def __init__(self, vm):
        self._vm = vm
        self.guestOperationsReady = True
        self.customizationInfo = SimpleNamespace(customizationStatus="TOOLSDEPLOYPKG_SUCCEEDED")
        self.ipAddress = None

    @property
    def toolsRunningStatus(self):  # noqa: N802 -- pyVmomi's property name
        return "guestToolsRunning" if self._vm.tools_running() else "guestToolsNotRunning"

    @property
    def net(self):
        return [SimpleNamespace(ipConfig=SimpleNamespace(ipAddress=[SimpleNamespace(ipAddress=ip, state="preferred")]))
                for ip in self._vm.guest_ips()]


class FakeVM:
    def __init__(self, name: str, devices: list, template: bool = False):
        self.name = name
        self._moId = f"vm-{next(_ids)}"
        self.config = SimpleNamespace(template=template, hardware=SimpleNamespace(device=devices))
        self.guest = FakeGuest(self)
        self.runtime = SimpleNamespace(powerState="poweredOff")  # the Web Services view (legacy dialect)
        self.clone_specs: list = []
        self.reconfig_specs: list = []
        self.customize_specs: list = []
        self.clone_fail: str | None = None
        self.tools: bool | None = None  # None: runs when the build configured the guest

    # The guest, as VMware Tools would report it -------------------------
    def powered_on(self) -> bool:
        return self.runtime.powerState == "poweredOn" or (FAKE is not None and FAKE.power.get(self._moId) == "POWERED_ON")

    def _metadata(self) -> dict | None:
        for spec in reversed(self.reconfig_specs):
            extra = {o.key: o.value for o in spec.extraConfig or []}
            if "guestinfo.metadata" in extra:
                return yaml.safe_load(base64.b64decode(extra["guestinfo.metadata"]))
        return None

    def tools_running(self) -> bool:
        configured = self._metadata() is not None or bool(self.customize_specs)
        return self.powered_on() and (configured if self.tools is None else self.tools)

    def guest_ips(self) -> list[str]:
        """cloud-init applies a netplan entry only to the NIC with its ``match: macaddress``;
        Sysprep maps an adapter by its MAC (or, without one, by order). A NIC whose MAC is
        not the one the config names gets no address: the 2026-10-20 failure."""
        if not self.tools_running():
            return []
        have = [str(c.macAddress).lower() for c in infra.nic_cards(self.config.hardware.device)]
        if (meta := self._metadata()) is not None:
            return [a.split("/")[0] for eth in meta["network"]["ethernets"].values()
                    if eth["match"]["macaddress"] in have for a in eth.get("addresses", [])]
        maps = self.customize_specs[-1].nicSettingMap if self.customize_specs else []
        return [m.adapter.ip.ipAddress for i, m in enumerate(maps)
                if ((m.macAddress.lower() in have) if m.macAddress else i < len(have))
                and isinstance(m.adapter.ip, vim.vm.customization.FixedIp)]

    def PowerOnVM_Task(self):  # noqa: N802
        self.runtime.powerState = "poweredOn"
        if FAKE is not None:
            FAKE.on_power_on(self)
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
        vm.parent = folder
        folder.childEntity.append(vm)
        FAKE.vms[vm._moId] = vm
        return FakeTask(result=vm, label=f"clone:{name}")

    def ReconfigVM_Task(self, spec):  # noqa: N802
        self.reconfig_specs.append(spec)
        if spec.deviceChange:
            self.config.hardware.device = _apply(self.config.hardware.device, spec.deviceChange)
        if spec.annotation is not None:
            self.config.annotation = spec.annotation
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
        self.generated_mac: str | None = None  # every generated MAC is this one (the esx02 race)
        self.ignore_manual = False  # treat manual MACs as generated ones
        self.reassigned: list[tuple[str, str, str]] = []  # (vm, old MAC, new MAC) vCenter changed at power-on
        self.cis = FakeCis()  # vCenter tags (vsphere_tags.py)

    def on_power_on(self, vm) -> None:
        """vCenter's late duplicate check: a *generated* MAC another VM already has is
        replaced when the VM powers on. A manual MAC is never touched."""
        others = {str(c.macAddress).lower() for v in self.vms.values() if v is not vm
                  for c in infra.nic_cards(v.config.hardware.device)}
        for card in infra.nic_cards(vm.config.hardware.device):
            if card.addressType != "manual" and str(card.macAddress).lower() in others:
                new = f"00:50:56:ba:{len(self.reassigned) + 0x80:02x}:5b"
                self.reassigned.append((vm.name, card.macAddress, new))
                card.macAddress = new

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
            return FakeView(objs) if kind is vim.VirtualMachine else SimpleNamespace(view=objs, Destroy=lambda: None)

        collector = SimpleNamespace(CreatePropertyCollector=lambda: SimpleNamespace(
            DestroyPropertyCollector=lambda: None), RetrievePropertiesEx=self.retrieve)
        return SimpleNamespace(_stub=object(), content=SimpleNamespace(
            rootFolder=object(), viewManager=SimpleNamespace(CreateContainerView=view), propertyCollector=collector,
            guestOperationsManager=self.guest_ops))

    def retrieve(self, specSet, options):  # noqa: N803 -- pyVmomi's keywords
        """RetrievePropertiesEx over the VMs: name and annotation (the range-ownership check),
        failing the whole call on an unknown VM, as vCenter does."""
        (spec,) = specSet
        paths = list(spec.propSet[0].pathSet)
        objects = []
        if spec.propSet[0].type is vim.dvs.DistributedVirtualPortgroup:  # vsphere_vlans.dvs_usage: one call
            assert spec.objectSet[0].obj._moId == self.dvs._moId
            assert spec.objectSet[0].selectSet[0].path == "portgroup"
            self.portgroup_reads = getattr(self, "portgroup_reads", 0) + 1
            for pg in self.dvs.portgroup:
                vals = {"name": pg.name, "config": pg.config, "tag": getattr(pg, "tag", None)}
                objects.append(SimpleNamespace(obj=pg, propSet=[SimpleNamespace(name=p, val=vals[p]) for p in paths]))
            return SimpleNamespace(objects=objects, token=None)
        if isinstance(spec.objectSet[0].obj, FakeView):  # a container-view traversal: every VM
            assert spec.objectSet[0].selectSet[0].path == "view"
            self.inventory_reads = getattr(self, "inventory_reads", 0) + 1
            pairs = [(vim.VirtualMachine(vm._moId, None), vm) for vm in [*self.vms.values(), *self.templates.values()]]
        else:
            pairs = []
            for o in spec.objectSet:
                vm = self.vms.get(o.obj._moId)
                if vm is None:
                    raise vmodl.fault.ManagedObjectNotFound(obj=o.obj)
                pairs.append((o.obj, vm))
        for ref, vm in pairs:
            vals = {"name": vm.name, "config.annotation": getattr(vm.config, "annotation", None),
                    "config.hardware.device": vm.config.hardware.device, "config.template": vm.config.template,
                    "parent": getattr(vm, "parent", None)}
            objects.append(SimpleNamespace(obj=ref, propSet=[SimpleNamespace(name=p, val=vals.get(p))
                                                             for p in paths]))
        return SimpleNamespace(objects=objects, token=None)

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
        if self.cis.handles(path):
            return self.cis(request)
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
            if query == "action=start":
                self.on_power_on(self.vms[vm_id])
            return httpx.Response(204)
        if m := re.fullmatch(r"/api/vcenter/vm/([^/]+)/tools", path):
            vm = self.vms.get(m.group(1))
            if vm is None:
                return httpx.Response(404, json={"error_type": "NOT_FOUND"})
            return httpx.Response(200, json={"run_state": "RUNNING" if vm.tools_running() else "NOT_RUNNING"})
        if m := re.fullmatch(r"/api/vcenter/vm/([^/]+)/guest/networking/interfaces", path):
            vm = self.vms.get(m.group(1))
            if vm is None:
                return httpx.Response(404, json={"error_type": "NOT_FOUND"})
            if not vm.tools_running():
                return httpx.Response(503, json={"error_type": "SERVICE_UNAVAILABLE"})
            return httpx.Response(200, json=[{"ip": {"ip_addresses": [
                {"ip_address": ip, "prefix_length": 24, "state": "PREFERRED"}]}} for ip in vm.guest_ips()])
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
        "VSPHERE_MAX_VCPU_PER_THREAD": 4.0, "VSPHERE_TOOLS_TIMEOUT": 0, "VSPHERE_GUEST_IP_TIMEOUT": 0,
        "VSPHERE_GUEST_IP_CHECK": True, "VSPHERE_MAC_POOL": "",
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
        folder = infra.find_folder(vc.dc, f"TrueNorth/Ranges/{KEY}")
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
        assert infra.find_folder(vc.dc, f"TrueNorth/Ranges/{KEY}") is None
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
            "folder_id": infra.find_folder(vc.dc, f"TrueNorth/Ranges/{KEY}")._moId,
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
        prov = _prov(vc)
        result = _run(prov.provision(RANGE_ID, _rendered(), {}))
        assert result.status == "ok", result.errors
        # As range_alloc.stored_output records it: with the vCenter it was built on.
        return {"vms": result.vms, "networks": result.networks, "connection": prov.connection_record()}

    def test_removes_vms_then_portgroups_and_folder(self, vc):
        output = self._built(vc)
        vc.requests.clear()
        result = _run(_prov(vc).destroy(RANGE_ID, output))
        assert result.status == "ok", result.errors
        assert result.resources_removed == 4 + 3
        assert not vc.vms and vc.dvs.portgroup == []
        assert infra.find_folder(vc.dc, f"TrueNorth/Ranges/{KEY}") is None
        assert infra.find_folder(vc.dc, "TrueNorth/Ranges") is not None  # shared parent stays
        assert sum(1 for r in vc.requests if r[0] == "DELETE" and r[1].startswith("/api/vcenter/vm/")) == 4
        assert [t["name"] for t in vc.cis.tags.values() if t["name"] == KEY] == []  # its tn-range tag went too

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
        vc.vms["vm-a"].config.annotation = mod.range_annotation(RANGE_ID)  # a build tags its VMs
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
        assert fw["nics"][0] == {"network": SVC, "ip": "10.30.32.101", "mac": fw["nics"][0]["mac"]}
        assert fw["nics"][0]["mac"] in macs_mod.parse_pool("")  # the WAN NIC's MAC is reserved too
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
# Exchange / SharePoint SE / MECM role images: setup at deploy (vsphere_products.py)
# --------------------------------------------------------------------------- #

PRODUCT_IMAGES = {**IMAGES, **{i: i for i in products_mod.SETUP_IMAGES}}
PRODUCT_TEMPLATE = {**TEMPLATE, "name": "Product Range", "nodes": [
    TEMPLATE["nodes"][0],
    {"id": "exch", "os": WS22, "vlan": "victim_network", "services": ["exchange", "owa"]},
    {"id": "sp", "os": WS22, "vlan": "victim_network", "services": ["sharepoint_se"]},
    {"id": "dc01", "os": WS22, "vlan": "victim_network", "ip": "10.60.201.10",
     "services": ["active_directory", "dns"], "ad_forest": "corp.range.local"},
]}


class TestProductSetup:
    """The provision flow around vsphere_products: when it runs, against which DC, as which
    login, and how its outcome lands on the range. The guest steps are faked here (their
    commands are tested in test_vsphere_products.py)."""

    @pytest.fixture
    def flow(self, vc, monkeypatch):
        for image in products_mod.SETUP_IMAGES:  # the role images exist on the vCenter
            tmpl = FakeVM(image, [], template=True)
            tmpl.config.hardware = FreshHardware(MGMT)
            vc.templates[image] = tmpl
            vc.dc.vmFolder.childEntity.append(tmpl)
        prov = _prov(vc)
        events: list[tuple] = []
        state = {"dc": "ok", "run": "running"}

        def roles_sync(si, vm_def, vm_id, deadline):
            events.append(("roles", vm_def["name"]))
            ok = {"status": "ok", "detail": "installed"}
            return {"ad-ds": {"status": state["dc"], "detail": "x"}, "dns": ok}

        def prepare(session_for, plan, deadline, reboot_fn=None):
            events.append(("prepare", plan.name, plan.dc_name, plan.domain_user, plan.domain_password.reveal()))
            return None

        def run(session_for, plan, deadline, budget_end, **kw):
            events.append(("run", plan.name))
            return products_mod.outcome(plan, state["run"], "last status: Exchange: installing the Mailbox role")

        monkeypatch.setattr(prov, "_roles_sync", roles_sync)
        monkeypatch.setattr(products_mod, "prepare", prepare)
        monkeypatch.setattr(products_mod, "run", run)
        monkeypatch.delenv(products_mod.PRODUCT_KEY_ENV, raising=False)
        built = render.render_topology(PRODUCT_TEMPLATE, RANGE_ID, PRODUCT_IMAGES.get)
        template = {"name": "Product Range", "vms": built["vm_definitions"], "networks": built["network_definitions"]}
        return prov, template, events, state

    def test_setup_runs_after_the_dc_is_promoted_as_its_administrator(self, flow, vc):
        prov, template, events, _ = flow
        result = _run(prov.provision(RANGE_ID, template, {}))
        assert result.status == "ok", result.errors  # a setup still running is a warning, not partial
        names = [e[:2] for e in events]
        assert names.index(("roles", f"{R8}-dc01")) < names.index(("prepare", f"{R8}-exch")) \
            < names.index(("run", f"{R8}-exch"))
        prep = next(e for e in events if e[0] == "prepare")
        dc_password = vc.vms[_vm_out(result, "dc01")["vm_id"]].guest_password("Administrator")
        assert prep[2:] == (f"{R8}-dc01", "CORP\\Administrator", dc_password)  # the DC's Sysprep Administrator
        exch = _vm_out(result, "exch")
        assert exch["roles"]["exchange"]["status"] == "running"
        assert exch["role_setup"]["image"] == "srv2022-exchange2019"
        assert exch["role_setup"]["organization"] == "Product Range"
        assert any(w.startswith(f"VM {R8}-exch: Exchange 2019 setup running") for w in result.warnings)
        dumped = json.dumps(result.__dict__, default=str)
        assert dc_password not in dumped

    def test_sharepoint_without_a_key_is_staged_and_never_touched(self, flow):
        prov, template, events, _ = flow
        result = _run(prov.provision(RANGE_ID, template, {}))
        sp = _vm_out(result, "sp")
        assert sp["roles"]["sharepoint"]["status"] == "skipped"
        assert sp["roles"]["sharepoint"]["detail"].startswith("staged (no product key)")
        assert sp["role_setup"]["image"] == "srv2022-sharepointse"
        assert not [e for e in events if e[0] != "roles" and e[1] == f"{R8}-sp"]
        assert any("SharePoint SE setup skipped: staged (no product key)" in w for w in result.warnings)
        assert not [e for e in result.errors if "sharepoint" in e]

    def test_a_dc_that_was_not_promoted_skips_the_setup(self, flow):
        prov, template, events, state = flow
        state["dc"] = "failed"
        result = _run(prov.provision(RANGE_ID, template, {}))
        exch = _vm_out(result, "exch")["roles"]["exchange"]
        assert exch["status"] == "skipped" and f"domain controller {R8}-dc01 is not ready (ad-ds failed)" in exch["detail"]
        assert not [e for e in events if e[0] in ("prepare", "run")]
        assert result.status == "partial"  # for the DC's own error only
        assert [e for e in result.errors if "exchange" in e] == []

    def test_a_built_role_image_is_cloned_and_set_up(self, flow, vc):
        prov, template, events, _ = flow
        _run(prov.provision(RANGE_ID, template, {}))
        assert [n for _, n, _ in vc.templates["srv2022-exchange2019"].clone_specs] == [f"{R8}-exch"]
        assert not [n for _, n, _ in vc.templates["tmpl-win2022"].clone_specs if n == f"{R8}-exch"]
        assert ("run", f"{R8}-exch") in [e[:2] for e in events]

    def test_an_unbuilt_role_image_builds_srv2022_and_warns(self, flow, vc):
        prov, _, events, _ = flow
        # The registry lists the role images but not as built: the resolver leaves them out.
        built = render.render_topology(PRODUCT_TEMPLATE, RANGE_ID, IMAGES.get)
        template = {"name": "Product Range", "vms": built["vm_definitions"], "networks": built["network_definitions"]}
        result = _run(prov.provision(RANGE_ID, template, {}))
        assert result.status == "ok", result.errors  # not partial: the range still builds
        exch = _vm_out(result, "exch")
        detail = "role image srv2022-exchange2019 not built — using tmpl-win2022; exchange not installed"
        assert exch["roles"]["exchange"] == {"status": "skipped", "detail": detail}
        assert exch["role_setup"] == {"status": "skipped", "detail": detail, "image": "srv2022-exchange2019"}
        assert f"VM {R8}-exch: {detail}" in result.warnings
        assert any(n == f"{R8}-exch" for _, n, _ in vc.templates["tmpl-win2022"].clone_specs)
        assert not vc.templates["srv2022-exchange2019"].clone_specs
        assert not [e for e in events if e[0] in ("prepare", "run") and e[1] == f"{R8}-exch"]

    def test_a_role_image_missing_on_the_vcenter_falls_back_too(self, flow, vc):
        prov, template, events, _ = flow
        gone = vc.templates.pop("srv2022-exchange2019")  # registered as built, but not in vCenter
        vc.dc.vmFolder.childEntity.remove(gone)
        result = _run(prov.provision(RANGE_ID, template, {}))
        assert result.status == "ok", result.errors
        exch = _vm_out(result, "exch")
        assert exch["role_setup"]["status"] == "skipped"
        assert exch["role_setup"]["detail"].startswith("role image srv2022-exchange2019 not built — using tmpl-win2022")
        assert exch["template"] == "tmpl-win2022"
        assert any(n == f"{R8}-exch" for _, n, _ in vc.templates["tmpl-win2022"].clone_specs)
        assert not [e for e in events if e[0] in ("prepare", "run") and e[1] == f"{R8}-exch"]
        assert ("run", f"{R8}-exch") not in [e[:2] for e in events]

    SP2019_TEMPLATE = {**TEMPLATE, "name": "Product Range", "nodes": [
        TEMPLATE["nodes"][0],
        {"id": "sp19", "os": WS22, "vlan": "victim_network", "services": ["sharepoint_2019"]},
        {"id": "dc01", "os": WS22, "vlan": "victim_network", "ip": "10.60.201.10",
         "services": ["active_directory", "dns"], "ad_forest": "corp.range.local"},
    ]}

    def test_sharepoint_2019_full_flow_clones_its_image_and_sets_up_with_the_site_key(self, flow, vc, monkeypatch):
        prov, _, events, _ = flow
        monkeypatch.setenv(products_mod.PRODUCT_KEY_ENV, "ABCDE-FGHIJ-KLMNO-PQRST-UVWXY")
        built = render.render_topology(self.SP2019_TEMPLATE, RANGE_ID, PRODUCT_IMAGES.get)
        template = {"name": "Product Range", "vms": built["vm_definitions"], "networks": built["network_definitions"]}
        result = _run(prov.provision(RANGE_ID, template, {}))
        assert result.status == "ok", result.errors
        assert [n for _, n, _ in vc.templates["srv2022-sharepoint2019"].clone_specs] == [f"{R8}-sp19"]
        names = [e[:2] for e in events]
        assert names.index(("roles", f"{R8}-dc01")) < names.index(("prepare", f"{R8}-sp19")) \
            < names.index(("run", f"{R8}-sp19"))
        sp = _vm_out(result, "sp19")
        assert sp["roles"]["sharepoint-2019"]["status"] == "running"
        assert sp["role_setup"]["image"] == "srv2022-sharepoint2019"
        assert "ABCDE-FGHIJ-KLMNO-PQRST-UVWXY" not in json.dumps(result.__dict__, default=str)

    def test_sharepoint_2019_falls_back_to_plain_srv2022_until_built(self, flow, vc):
        prov, _, events, _ = flow
        built = render.render_topology(self.SP2019_TEMPLATE, RANGE_ID, IMAGES.get)  # not registered as built
        template = {"name": "Product Range", "vms": built["vm_definitions"], "networks": built["network_definitions"]}
        result = _run(prov.provision(RANGE_ID, template, {}))
        assert result.status == "ok", result.errors
        sp = _vm_out(result, "sp19")
        detail = "role image srv2022-sharepoint2019 not built — using tmpl-win2022; sharepoint-2019 not installed"
        assert sp["role_setup"] == {"status": "skipped", "detail": detail, "image": "srv2022-sharepoint2019"}
        assert f"VM {R8}-sp19: {detail}" in result.warnings
        assert any(n == f"{R8}-sp19" for _, n, _ in vc.templates["tmpl-win2022"].clone_specs)
        assert not [e for e in events if e[0] in ("prepare", "run") and e[1] == f"{R8}-sp19"]

    def test_a_finished_setup_is_ok_on_the_role(self, flow):
        prov, template, _, state = flow
        state["run"] = "ok"
        result = _run(prov.provision(RANGE_ID, template, {}))
        assert _vm_out(result, "exch")["roles"]["exchange"]["status"] == "ok"
        assert not [w for w in result.warnings if "Exchange 2019 setup" in w]

    def test_a_running_setup_leaves_the_range_ready(self, flow):
        prov, template, _, _ = flow
        result = _run(prov.provision(RANGE_ID, template, {}))
        assert result.status == "ok"  # a setup still running is never a failed/partial range
        assert _vm_out(result, "exch")["role_setup"]["status"] == "running"

    def test_finish_by_caps_the_budget_below_the_soft_limit(self, flow):
        prov, template, _, _ = flow
        finish = mod.time.monotonic() + 5  # the soft limit minus the reserve, from the task
        result = _run(prov.provision(RANGE_ID, template, {"finish_by": finish}))
        assert abs(prov._budget_end - finish) < 1  # the provisioner stops new work by finish_by
        # No budget left to start the product: skipped, never run past the soft limit.
        assert _vm_out(result, "exch")["role_setup"]["status"] == "skipped"

    def test_all_software_installs_share_one_concurrency_semaphore(self, flow, monkeypatch):
        prov, template, _, _ = flow
        gates: list[int] = []

        async def record(si, vm_defs, vms_out, start, errors, warnings, gate=None):
            gates.append(id(gate))

        monkeypatch.setattr(prov, "_install_software", record)
        _run(prov.provision(RANGE_ID, template, {}))
        # The non-product batch and every product VM install through the same semaphore object.
        assert len(gates) >= 2 and len(set(gates)) == 1
        assert isinstance(prov._budget_end, float)

    @staticmethod
    def _running_output(since: float) -> dict:
        return {"vms": [
            {"name": f"{R8}-exch", "vm_id": "vm-exch", "role_setup": {"status": "running", "reconcile": {
                "role": "exchange", "domain": "corp.range.local", "netbios": "CORP", "dc_node": "dc01",
                "status_file": "C:\\TrueNorth\\deploy\\status", "since": since, "timeout": 7200}}},
            {"name": f"{R8}-so", "vm_id": "vm-so", "securityonion": {"status": "running", "reconcile": {
                "kind": "securityonion", "node": "seconion01", "web_user": "analyst@range.local",
                "web_account": "analyst", "admin_user": "soadmin", "admin_last": False, "install_from_env": True,
                "since": since, "timeout": 7200}}},
        ]}

    def test_reconcile_setups_reads_each_running_setup_and_maps_the_outcome(self, vc, monkeypatch):
        prov = _prov(vc)
        for vid in ("vm-exch", "vm-so"):
            vc.vms[vid] = FakeVM(vid, [])
        output = self._running_output(mod.time.time())
        seen = {}

        async def screen(range_id, vms):
            return [], [], set()

        def prod_reconcile(session, info, **kw):
            seen["prod_user"] = session.creds.username
            seen["prod_pw"] = session.creds.password.reveal()
            seen["prod_deadline"] = kw.get("deadline")
            return {"status": "ok", "detail": "set up", "image": "srv2022-exchange2019", "domain": info["domain"]}

        def so_reconcile(session_for, install, info, **kw):
            seen["so_user"] = install.username
            seen["so_admin_pw"] = kw["admin_password"].reveal()
            return {"status": "ok", "detail": "healthy"}, {"soadmin", "analyst"}

        monkeypatch.setattr(prov, "_screen", screen)
        monkeypatch.setattr(products_mod, "reconcile", prod_reconcile)
        monkeypatch.setattr(so_mod, "reconcile", so_reconcile)
        changes, warnings = _run(prov.reconcile_setups(
            RANGE_ID, output, domain_passwords={"dc01": "Dc-Pass-1!"},
            so_logins={"seconion01": ("soadmin", "So-Pass-1!", "Admin-Pass-2!")}, per_vm_seconds=30))
        assert seen["prod_user"] == "CORP\\Administrator" and seen["prod_pw"] == "Dc-Pass-1!"  # the domain admin
        assert seen["prod_deadline"] - mod.time.monotonic() <= 30  # the per-VM guest wait is capped
        assert seen["so_user"] == "soadmin" and seen["so_admin_pw"] == "Admin-Pass-2!"
        assert changes[f"{R8}-exch"]["role_setup"]["status"] == "ok"
        assert changes[f"{R8}-exch"]["role"] == "exchange"
        assert changes[f"{R8}-so"]["securityonion"]["status"] == "ok"
        assert set(changes[f"{R8}-so"]["accounts_applied"]) == {"soadmin", "analyst"}

    def test_reconcile_setups_times_out_without_any_guest_operation(self, vc, monkeypatch):
        prov = _prov(vc)
        output = self._running_output(0.0)  # started in 1970: long past the timeout

        async def screen(range_id, vms):
            return [], [], set()

        monkeypatch.setattr(prov, "_screen", screen)
        monkeypatch.setattr(products_mod, "reconcile", lambda *a, **k: pytest.fail("no guest op past the timeout"))
        monkeypatch.setattr(so_mod, "reconcile", lambda *a, **k: pytest.fail("no guest op past the timeout"))
        # No credentials at all and the VMs are gone from vCenter: still ends.
        changes, _ = _run(prov.reconcile_setups(RANGE_ID, output, domain_passwords={}, so_logins={}))
        assert changes[f"{R8}-exch"]["role_setup"]["status"] == "timeout"
        assert changes[f"{R8}-so"]["securityonion"]["status"] == "timeout"

    def test_a_terminal_timeout_still_cleans_up_secrets_when_the_guest_answers(self, vc, monkeypatch):
        prov = _prov(vc)
        for vid in ("vm-exch", "vm-so"):
            vc.vms[vid] = FakeVM(vid, [])
        cleaned = []

        async def screen(range_id, vms):
            return [], [], set()

        monkeypatch.setattr(prov, "_screen", screen)
        monkeypatch.setattr(products_mod, "cleanup",
                            lambda session, deadline: cleaned.append(("product", session.creds.username, deadline)))
        monkeypatch.setattr(so_mod, "cleanup",
                            lambda session, install, deadline: cleaned.append(("so", install.username, deadline)))
        changes, _ = _run(prov.reconcile_setups(
            RANGE_ID, self._running_output(0.0), domain_passwords={"dc01": "Dc-Pass-1!"},
            so_logins={"seconion01": ("soadmin", "So-Pass-1!", "")}))
        assert changes[f"{R8}-exch"]["role_setup"]["status"] == "timeout"
        assert changes[f"{R8}-so"]["securityonion"]["status"] == "timeout"
        assert [(k, u) for k, u, _ in cleaned] == [("product", "CORP\\Administrator"), ("so", "soadmin")]
        assert all(d - mod.time.monotonic() <= 30 for _, _, d in cleaned)  # bounded

    def test_reconcile_setups_warns_when_a_dc_admin_is_undeclared(self, vc, monkeypatch):
        prov = _prov(vc)
        vc.vms["vm-exch"] = FakeVM("vm-exch", [])
        output = {"vms": self._running_output(mod.time.time())["vms"][:1]}

        async def screen(range_id, vms):
            return [], [], set()

        monkeypatch.setattr(prov, "_screen", screen)
        changes, warnings = _run(prov.reconcile_setups(RANGE_ID, output, domain_passwords={}, so_logins={}))
        assert not changes and any("no Administrator password" in w for w in warnings)

    def test_reconcile_setups_stops_guest_work_when_the_run_budget_is_spent(self, vc, monkeypatch):
        prov = _prov(vc)
        for vid in ("vm-exch", "vm-so"):
            vc.vms[vid] = FakeVM(vid, [])

        async def screen(range_id, vms):
            return [], [], set()

        monkeypatch.setattr(prov, "_screen", screen)
        monkeypatch.setattr(products_mod, "reconcile", lambda *a, **k: pytest.fail("no budget left"))
        monkeypatch.setattr(so_mod, "reconcile", lambda *a, **k: pytest.fail("no budget left"))
        changes, _ = _run(prov.reconcile_setups(
            RANGE_ID, self._running_output(mod.time.time()), domain_passwords={"dc01": "x"},
            so_logins={"seconion01": ("u", "p", "")}, run_seconds=0))
        assert changes == {}  # left running for the next run


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
        ("red-vs-blue", {"network_monitoring": ["management", "corporate_lan", "dmz", "server_vlan", "blue_team",
                                                "red_team"]}),
        ("large-enterprise", {"network_monitoring": ["dmz", "corporate_lan", "server_vlan", "security_vlan",
                                                     "ot_scada", "management", "ad_forest_b", "cloud_hybrid"]}),
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

    @pytest.mark.parametrize(("name", "sensors"), [("red-vs-blue", ["nids01", "ids01"]),
                                                   ("large-enterprise", ["ids01"])])
    def test_shipped_sensors_receive_every_zone(self, vc, name, sensors):
        """The template's own networks, mirror rule and sensor nodes, built by the real
        provisioner on the fake vCenter. Trimmed to the sensors plus one plain host per
        zone (bare ubuntu, small) so it fits the fake lab; appliances are left out, which
        the session code excludes from destinations anyway."""
        tpl = yaml.safe_load((CONTENT / name / "template.yaml").read_text())
        hosts = [n for n in tpl["nodes"] if n["id"] in sensors]
        zones = {n["vlan"] for n in hosts}
        for n in tpl["nodes"]:
            if n["vlan"] not in zones and not render._is_router(n, n["os"]):
                hosts.append(n)
                zones.add(n["vlan"])
        tpl["nodes"] = [{**n, "os": "ubuntu-2404", "services": [], "specs": {"cores": 1, "memory_mb": 1024,
                                                                           "disk_gb": 10}} for n in hosts]
        result = _run(_prov(vc).provision(RANGE_ID, _mirror_build(tpl), {}))
        assert result.status == "ok", result.errors

        dst = f"tn-{R8}-network_monitoring"
        assert sorted(_sessions(vc)) == [dst, f"{dst}-rx"]
        local = _sessions(vc)[dst]
        monitor_pg = next(n["portgroup"] for n in result.networks if n.get("name") == "network_monitoring")
        sensor_ports = sorted(p for s in sensors for p in _nic_ports(vc, s, monitor_pg))
        assert len(sensor_ports) == len(sensors)  # one monitor NIC each
        assert sorted(local.destinationPort.portKey) == sensor_ports
        # Every routed zone a host sits on is a source (ot_scada too: isolated, still mirrored).
        made = next(m for m in result.mirrors if m["name"] == dst)
        assert set(made["source_zones"]) == {n["name"] for n in result.networks
                                             if n.get("name") not in (None, "network_monitoring") and not n.get("rspan")}
        assert len(local.sourcePortReceived.portKey) == len(hosts)  # every host's NIC on its own zone
        # The monitor NIC sniffs: no address in what the worker built.
        vms = {v["node_id"]: v for v in _mirror_build(tpl)["vms"]}
        for s in sensors:
            (mon,) = [nic for nic in vms[s]["nics"] if nic.get("monitor")]
            assert mon["network"] == "network_monitoring" and mon["ip"] == ""

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
        output = {"vms": result.vms, "networks": result.networks, "mirrors": result.mirrors,
                  "connection": _prov(vc).connection_record()}  # as stored_output records it
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
        assert [n.kind for n in prov.allocation_needs(RANGE_ID, _rendered(LAB))] == ["mac"]  # NIC MACs only
        result = _run(prov.provision(RANGE_ID, _rendered(LAB), {}))
        assert result.status == "ok", result.errors
        assert lab_vc.dvs.portgroup == []
        ((_folder, _name, spec),) = lab_vc.templates["tmpl-ubuntu-2404"].clone_specs
        nics = [c.device for c in spec.config.deviceChange if c.operation != "remove"]
        assert [n.backing.deviceName for n in nics] == ["pg-lab-07"]
        assert result.vms[0]["nics"] == [{"network": "pg-lab-07", "ip": "10.20.0.10", "mac": "00:50:56:3c:00:00"}]
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
        if path.startswith("/rest/com/vmware/cis/tagging/"):  # vcsim serves the legacy tagging API
            return vc.cis(request)
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
    """Nothing but logins and the legacy tagging API (which vcsim serves): never the missing /api."""
    return {p for _, p in vc.legacy_calls if not p.startswith("/rest/com/vmware/cis/tagging/")} <= {
        "/api/session", "/rest/com/vmware/cis/session"}


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
        vm.config.annotation = mod.range_annotation(RANGE_ID)
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

        output = {"vms": result.vms, "networks": result.networks, "connection": prov.connection_record()}
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
        assert out["nics"][1] == {"network": NOISE_PG, "ip": "10.255.0.14", "mac": eth["nic1"]["match"]["macaddress"]}
        assert out["ip_verified"] is True and set(out["guest_ips"]) == {"10.60.200.50", "10.255.0.14"}

    def test_the_mgmt_network_needs_no_vlan_reservation(self, noise_vc):
        prov = _prov(noise_vc)
        (need,) = [n for n in prov.allocation_needs(RANGE_ID, {"vms": [_noise_vm()]}) if n.kind == "vlan"]
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
                    {"ip": {"ip_addresses": [{"ip_address": "10.60.200.51", "state": "PREFERRED"},
                                             {"ip_address": "fe80::1", "state": "PREFERRED"}]}}]

        monkeypatch.setattr(prov, "_api_get", interfaces)
        seen = _run(prov._guest_ipv4s(None, "vm-1"))
        assert seen == ["10.255.0.14", "10.60.200.51"]
        plan = mod.VsphereAPIProvisioner._vm_plan(RANGE_ID, _noise_vm())
        # The planned address is missing: the observed one is reported, never the mgmt one.
        got = readiness.assess(plan, {"tools_ready": True, "seen": seen, "checked": True, "timeout": 0})
        assert got["ip"] == "10.60.200.51" and got["ip_verified"] is False and "10.60.200.50" in got["not_ready"]

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


# --------------------------------------------------------------------------- #
# Host-local template copies (ADR 0012 decision 6): <template>@<host>-<yyyymmdd>
# --------------------------------------------------------------------------- #

ONLY_ESX02 = "esx01.truenorth.lab,esx03.truenorth.lab,esx04.truenorth.lab"


def _host(vc, short: str):
    return next(h for h in vc.hosts if h.name.split(".")[0] == short)


def _add_copy(vc, name: str, on: str) -> FakeVM:
    """An inventory template ``name`` whose disks are on host ``on``'s local datastore."""
    tmpl = FakeVM(name, [], template=True)
    tmpl.config.hardware = FreshHardware(MGMT)
    tmpl.datastore = list(_host(vc, on).datastore)
    vc.templates[name] = tmpl
    vc.dc.vmFolder.childEntity.append(tmpl)
    return tmpl


ONE_LINUX = {**TEMPLATE, "nodes": [TEMPLATE["nodes"][1]]}


class TestHostLocalTemplateCopies:
    def test_each_vm_clones_the_copy_on_its_own_host(self, vc, caplog):
        for short in ("esx02", "esx03", "esx04"):
            for base in ("tmpl-ubuntu-2404", "tmpl-win2022", "tmpl-pfsense"):
                _add_copy(vc, f"{base}@{short}-20261019", short)
        caplog.set_level("INFO", logger=mod.__name__)
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {}))
        assert result.status == "ok", result.errors
        assert len({vm["host"] for vm in result.vms}) > 1  # spread: the test covers several hosts
        for vm in result.vms:
            short = vm["host"].split(".")[0]
            assert vm["template"].endswith(f"@{short}-20261019")
            (clone,) = [s for s in vc.templates[vm["template"]].clone_specs if s[1] == vm["name"]]
            assert clone[2].location.datastore in _host(vc, short).datastore
        assert not any(vc.templates[b].clone_specs for b in ("tmpl-ubuntu-2404", "tmpl-win2022", "tmpl-pfsense"))
        assert "host-local template copy 'tmpl-ubuntu-2404@" in caplog.text

    def test_the_newest_date_wins(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_EXCLUDE_HOSTS", ONLY_ESX02)
        for date in ("20260701", "20261019", "20261001"):
            _add_copy(vc, f"tmpl-ubuntu-2404@esx02-{date}", "esx02")
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {}))
        assert result.status == "ok", result.errors
        assert result.vms[0]["template"] == "tmpl-ubuntu-2404@esx02-20261019"
        assert not vc.templates["tmpl-ubuntu-2404@esx02-20261001"].clone_specs

    def test_no_copy_on_the_host_falls_back_to_the_plain_name(self, vc, monkeypatch, caplog):
        monkeypatch.setattr(mod, "VSPHERE_EXCLUDE_HOSTS", ONLY_ESX02)
        _add_copy(vc, "tmpl-ubuntu-2404@esx03-20261019", "esx03")  # another host's copy: not used
        caplog.set_level("INFO", logger=mod.__name__)
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {}))
        assert result.status == "ok", result.errors
        assert result.vms[0]["host"] == "esx02.truenorth.lab"
        assert result.vms[0]["template"] == "tmpl-ubuntu-2404"
        assert vc.templates["tmpl-ubuntu-2404"].clone_specs
        assert not vc.templates["tmpl-ubuntu-2404@esx03-20261019"].clone_specs
        assert "no host-local copy on esx02.truenorth.lab" in caplog.text

    def test_a_copy_named_for_the_host_but_stored_elsewhere_is_not_used(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_EXCLUDE_HOSTS", ONLY_ESX02)
        _add_copy(vc, "tmpl-ubuntu-2404@esx02-20261019", "esx03")  # misnamed, or moved by hand
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {}))
        assert result.vms[0]["template"] == "tmpl-ubuntu-2404"

    def test_no_copy_anywhere_still_clones_the_plain_template(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {}))
        assert result.status == "ok", result.errors
        assert result.vms[0]["template"] == "tmpl-ubuntu-2404"

    def test_a_copy_without_the_plain_template_still_builds_on_that_host(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_EXCLUDE_HOSTS", ONLY_ESX02)
        _add_copy(vc, "tmpl-ubuntu-2404@esx02-20261019", "esx02")
        vc.templates.pop("tmpl-ubuntu-2404")
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {}))
        assert result.status == "ok", result.errors

    def test_a_host_local_copy_is_used_before_the_content_library(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_EXCLUDE_HOSTS", ONLY_ESX02)
        monkeypatch.setattr(mod, "VSPHERE_CONTENT_LIBRARY", "TrueNorth-Templates")
        vc.library = {"name": "TrueNorth-Templates", "items": {"tmpl-ubuntu-2404": "item-ubuntu"}}
        _add_copy(vc, "tmpl-ubuntu-2404@esx02-20261019", "esx02")
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {}))
        assert result.vms[0]["template"] == "tmpl-ubuntu-2404@esx02-20261019"
        assert not any("/ovf/library-item/" in path for _, path, _ in vc.requests)

    def test_the_library_path_is_unchanged_without_a_copy(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_CONTENT_LIBRARY", "TrueNorth-Templates")
        vc.library = {"name": "TrueNorth-Templates", "items": {"tmpl-ubuntu-2404": "item-ubuntu"}}
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {}))
        assert result.vms[0]["template"] == "library:tmpl-ubuntu-2404"
        assert not vc.templates["tmpl-ubuntu-2404"].clone_specs

    def test_cluster_placement_has_no_host_and_uses_the_plain_name(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_PLACEMENT", "cluster")
        monkeypatch.setattr(mod, "VSPHERE_DATASTORE", "esx02-local")
        _add_copy(vc, "tmpl-ubuntu-2404@esx02-20261019", "esx02")
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {}))
        assert result.status == "ok", result.errors
        assert result.vms[0]["template"] == "tmpl-ubuntu-2404"


class FakeCopy:
    def __init__(self, vm_id, name, template=True, fail=None):
        self._moId, self.name, self.fail, self.destroyed = vm_id, name, fail, False
        self.config = SimpleNamespace(template=template)

    def Destroy_Task(self):  # noqa: N802
        self.destroyed = True
        return FakeTask(fail=self.fail, label=f"destroy-template:{self.name}")


class TestPruneTemplateCopies:
    @pytest.fixture
    def inv(self, vc):
        objs = {o._moId: o for o in (
            FakeCopy("vm-1", "srv@esx02-20260701"), FakeCopy("vm-2", "srv@esx02-20261001"),
            FakeCopy("vm-3", "srv@esx02-20261019"), FakeCopy("vm-4", "srv@esx03-20260701"),
            FakeCopy("vm-5", "srv"), FakeCopy("vm-6", "web@esx02-20260101", template=False),
            FakeCopy("vm-7", "web@esx02-20261019"))}
        prov = _prov(vc)
        listed = [(o._moId, o.name, "", o.config.template) for o in objs.values()]  # as listed, before any change
        prov._vm_rows_sync = lambda si: listed
        prov._vm = lambda si, vm_id: objs[vm_id]
        return prov, objs

    def test_dry_run_is_the_default_and_deletes_nothing(self, inv):
        prov, objs = inv
        out = _run(prov.prune_template_copies(set()))
        assert out["dry_run"] is True and out["deleted"] == []
        assert out["deletable"] == ["srv@esx02-20260701", "srv@esx02-20261001"]
        assert not any(o.destroyed for o in objs.values())

    def test_deletes_the_superseded_copies_not_in_use(self, inv):
        prov, objs = inv
        out = _run(prov.prune_template_copies({"srv@esx02-20260701"}, dry_run=False))
        assert out["deleted"] == ["srv@esx02-20261001"] and out["kept_in_use"] == ["srv@esx02-20260701"]
        # The newest per host, the only copy on esx03, the plain template and a VM are never touched.
        assert [o.name for o in objs.values() if o.destroyed] == ["srv@esx02-20261001"]
        assert out["errors"] == []

    def test_a_failed_delete_is_reported_and_the_rest_go_on(self, inv):
        prov, objs = inv
        objs["vm-1"].fail = "locked by a clone"
        out = _run(prov.prune_template_copies(set(), dry_run=False))
        assert out["deleted"] == ["srv@esx02-20261001"]
        assert out["errors"] == ["srv@esx02-20260701 (vm-1): locked by a clone"]

    def test_an_object_that_is_no_longer_that_template_is_left_alone(self, inv):
        prov, objs = inv
        objs["vm-2"].config.template = False  # converted to a VM since the listing
        out = _run(prov.prune_template_copies(set(), dry_run=False))
        assert not objs["vm-2"].destroyed
        assert out["errors"] == ["srv@esx02-20261001 (vm-2): no longer that template, left alone"]


# --------------------------------------------------------------------------- #
# Security Onion: so-setup per range (vsphere_securityonion.py)
# --------------------------------------------------------------------------- #

SO_TEMPLATE = {
    "name": "soc",
    "network": {"vlans": [
        {"id": 201, "name": "victim_network", "cidr": "10.60.201.0/24"},
        {"id": 202, "name": "soc_tools", "cidr": "10.60.202.0/24"},
        {"id": 203, "name": "network_monitoring", "cidr": "10.60.203.0/24"},
    ], "firewall_rules": [{"name": "monitoring-mirror", "src": "*", "dst": "network_monitoring", "action": "mirror"}]},
    "nodes": [
        {"id": "fw", "role": "firewall", "os": "pfsense", "vlan": "soc_tools",
         "interfaces": [{"vlan": "victim_network"}, {"vlan": "soc_tools"}, {"vlan": "network_monitoring"}]},
        {"id": "web01", "role": "server", "os": "ubuntu-2404", "vlan": "victim_network", "ip": "10.60.201.20"},
        # Monitor NIC not flagged: the provisioner takes the NIC on the mirror destination.
        {"id": "seconion01", "role": "network_monitor", "os": "securityonion", "vlan": "soc_tools",
         "ip": "10.60.202.60", "interfaces": [{"vlan": "soc_tools"}, {"vlan": "network_monitoring"}],
         "services": ["zeek", "suricata"]},
    ],
}


class TestSecurityOnion:
    @pytest.fixture
    def so_vc(self, vc, monkeypatch):
        monkeypatch.setenv("VSPHERE_SO_INSTALL_USER", "soadmin")
        monkeypatch.setenv("VSPHERE_SO_INSTALL_PASSWORD", "Build-Pass-77")
        monkeypatch.setattr(so_mod, "VSPHERE_SO_POLL", 0)
        real = FakeVM.guest_password
        monkeypatch.setattr(FakeVM, "guest_password", lambda self, user: "Build-Pass-77" if (
            user == "soadmin" and self.name.endswith("seconion01")) else real(self, user))
        return vc

    @staticmethod
    def _build(vc):
        images = {**IMAGES, "securityonion": "tmpl-ubuntu-2404"}
        out = render.render_topology(SO_TEMPLATE, RANGE_ID, images.get)
        tpl = {"name": "soc", "vms": out["vm_definitions"], "networks": out["network_definitions"],
               "network": SO_TEMPLATE["network"]}
        return _run(_prov(vc).provision(RANGE_ID, tpl, {}))

    def test_sensor_is_set_up_standalone_monitoring_the_mirror_zone(self, so_vc, caplog):
        caplog.set_level("DEBUG")
        vc = so_vc
        vc.guest_ops.exit_codes = {"[!0-9]": 0}  # the status read: healthy
        result = self._build(vc)
        assert result.status == "ok", result.errors

        so_out = _vm_out(result, "seconion01")
        vm = vc.vms[so_out["vm_id"]]
        pg_name = {p.key: p.name for p in vc.dvs.portgroup}
        mac_on = {pg_name[c.backing.port.portgroupKey]: c.macAddress.lower()
                  for c in infra.nic_cards(vm.config.hardware.device)}
        zone_pg = {n["name"]: n["portgroup"] for n in result.networks if n.get("portgroup")}
        mgmt_mac, mon_mac = mac_on[zone_pg["soc_tools"]], mac_on[zone_pg["network_monitoring"]]
        assert mgmt_mac != mon_mac

        sec = so_out["securityonion"]
        assert sec["status"] == "ok" and (sec["management_mac"], sec["monitor_mac"]) == (mgmt_mac, mon_mac)
        # The answer file the staging command writes maps the NICs by MAC.
        started = [s for s in vc.guest_ops.started if s[0].endswith("seconion01")]
        assert [s[1] for s in started] == ["soadmin", "soadmin"]  # stage, then one status read
        b64 = re.search(r"printf %s ([A-Za-z0-9+/=]+) \| base64 -d > /root/tn-so-answers\.env",
                        started[0][3]).group(1)
        answers = base64.b64decode(b64).decode()
        assert f"TN_MGMT_MAC={mgmt_mac}" in answers and f"TN_MON_MAC={mon_mac}" in answers
        assert "manager_ip_mask=10.60.202.60/24" in answers.splitlines()
        assert "MGATEWAY=10.60.202.1" in answers.splitlines()
        # Its services come with so-setup: no catalogue install was tried on it.
        assert "software" not in so_out
        # The monitor NIC receives the mirror session's copies; its port group is promiscuous.
        local = _sessions(vc)[f"tn-{R8}-network_monitoring"]
        assert set(_nic_ports(vc, "seconion01", zone_pg["network_monitoring"])) <= set(local.destinationPort.portKey)
        mon_pg = next(p for p in vc.dvs.portgroup if p.name == zone_pg["network_monitoring"])
        assert mon_pg.spec.defaultPortConfig.securityPolicy.allowPromiscuous.value is True
        # Only the generated web password notice, which never holds the password.
        (note,) = [w for w in result.warnings if "Security Onion" in w]
        assert "generated password" in note
        assert "Build-Pass-77" not in caplog.text + repr(result)

    def test_declared_accounts_become_the_admin_and_the_soc_web_login(self, so_vc, caplog):
        """As worker/range_accounts.attach hands them over: ``_accounts`` on the VM."""
        caplog.set_level("DEBUG")
        vc = so_vc
        vc.guest_ops.exit_codes = {"[!0-9]": 0}
        images = {**IMAGES, "securityonion": "tmpl-ubuntu-2404"}
        out = render.render_topology(SO_TEMPLATE, RANGE_ID, images.get)
        for vm in out["vm_definitions"]:
            if vm["node_id"] == "seconion01":
                vm["_accounts"] = [
                    guest_mod.RangeAccount("analyst@range.local", guest_mod.Secret("Web-Gen.Pass#1"), False),
                    guest_mod.RangeAccount("tnadmin", guest_mod.Secret("Adm-Gen.Pass#2"), True)]
        tpl = {"name": "soc", "vms": out["vm_definitions"], "networks": out["network_definitions"],
               "network": SO_TEMPLATE["network"]}
        result = _run(_prov(vc).provision(RANGE_ID, tpl, {}))
        assert result.status == "ok", result.errors
        sec = _vm_out(result, "seconion01")["securityonion"]
        assert (sec["status"], sec["web_user"], sec["web_password"], sec["admin_user"]) == (
            "ok", "analyst@range.local", "range account", "tnadmin")
        stage = next(s for s in vc.guest_ops.started if s[0].endswith("seconion01"))[3]
        b64 = re.search(r"printf %s ([A-Za-z0-9+/=]+) \| base64 -d > /root/tn-so-answers\.env", stage).group(1)
        answers = base64.b64decode(b64).decode().splitlines()
        assert "WEBUSER=analyst@range.local" in answers and "TN_ADMIN_USER=tnadmin" in answers
        # Its accounts are its setup's to set and report: no "not set at deploy" warning, no notice.
        assert not [w for w in result.warnings if "seconion01" in w]
        so_out = _vm_out(result, "seconion01")
        assert so_out["accounts_applied"] == ["analyst@range.local", "tnadmin"]  # verified (getent, so-user)
        labels = [s[3] for s in vc.guest_ops.started if s[0].endswith("seconion01")]
        assert any("so-user list" in a for a in labels) and any("getent passwd tnadmin" in a for a in labels)
        # Not through cloud-init: no account hashes in its guestinfo, nothing left to scrub.
        assert "users" not in _userdata(vc.vms[so_out["vm_id"]])
        assert "Web-Gen.Pass#1" not in caplog.text + repr(result)
        assert "Adm-Gen.Pass#2" not in caplog.text + repr(result)

    def test_failed_setup_is_a_warning_not_a_partial_range(self, so_vc):
        vc = so_vc
        vc.guest_ops.exit_codes = {"[!0-9]": 13}
        result = self._build(vc)
        assert result.status == "ok", result.errors
        assert _vm_out(result, "seconion01")["securityonion"]["status"] == "failed"
        assert any("Security Onion setup failed: so-setup failed" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# 2026-10-20 on the work vCenter, range "Ubuntu Starter 01" (3 x ubuntu-lts):
#   1. ubu02 and ubu03, cloned side by side on esx02, were both given MAC
#      00:50:56:ba:2b:47; vCenter then changed ubu02's, after its netplan was keyed to
#      the first, and ubu02 never got its static IP;
#   2. "spread" put all three VMs on esx02;
#   3. the range was recorded ready, showing all three planned IPs.
# --------------------------------------------------------------------------- #

STARTER = {
    "name": "Ubuntu Starter 01",
    "network": {"vlans": [{"id": 10, "name": "lan", "cidr": "10.70.10.0/24"}]},
    "nodes": [{"id": f"ubu0{i}", "role": "workstation", "os": "ubuntu-2404", "vlan": "lan",
               "ip": f"10.70.10.{10 + i}", "specs": {"cores": 2, "memory_mb": 4096, "disk_gb": 40}}
              for i in (1, 2, 3)],
}
STARTER_IPS = {f"{R8}-ubu0{i}": f"10.70.10.{10 + i}" for i in (1, 2, 3)}
ONE_STARTER = {**STARTER, "nodes": STARTER["nodes"][:1]}


def _starter_vc(vc):
    """The work site: esx02 nearly idle with many threads, esx03/esx04 busy but with room,
    each with a host-local copy of the Ubuntu template on its own datastore."""
    by_short = {h.name.split(".")[0]: h for h in vc.hosts}
    by_short["esx02"].hardware.cpuInfo.numCpuThreads = 64
    for short in ("esx03", "esx04"):
        by_short[short].vm[0].summary.config.numCpu = 40  # 16 x 4 - 40 = 24 vCPU headroom left
    for short in ("esx02", "esx03", "esx04"):
        _add_copy(vc, f"tmpl-ubuntu-2404@{short}-20261019", short)
    return vc


def _with_handler(prov, vc, override):
    """Answer some REST calls differently (``override`` returns a response or None)."""
    prov._transport = httpx.MockTransport(lambda request: override(request) or vc.handler(request))
    return prov


class TestMacRace:
    def test_concurrent_clones_given_one_generated_mac_still_get_their_own(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_EXCLUDE_HOSTS", ONLY_ESX02)  # all three on esx02, as on site
        vc.generated_mac = "00:50:56:ba:2b:47"  # what vCenter generated for both clones
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(STARTER), {}))
        assert result.status == "ok", result.errors
        assert vc.reassigned == []  # a manual MAC is never regenerated at power-on
        seen = set()
        for out in result.vms:
            vm = vc.vms[out["vm_id"]]
            (card,) = infra.nic_cards(vm.config.hardware.device)
            assert card.addressType == "manual" and card.macAddress in macs_mod.parse_pool("")
            assert card.macAddress != "00:50:56:ba:2b:47" and card.macAddress not in seen
            seen.add(card.macAddress)
            eth = _guestinfo_meta(vm)["network"]["ethernets"]["nic0"]
            assert eth["match"]["macaddress"] == card.macAddress == out["nics"][0]["mac"]
            assert (out["ip"], out["ip_verified"], out["guest_ips"]) == (
                STARTER_IPS[out["name"]], True, [STARTER_IPS[out["name"]]])
        assert result.not_ready == []

    def test_the_old_generated_mac_race_is_caught_by_readiness_not_reported_ready(self, vc, monkeypatch):
        """Without explicit MACs (the provisioner before this fix) the race reproduces in the
        fake, and readiness now refuses to call the range ready."""
        monkeypatch.setattr(mod, "VSPHERE_EXCLUDE_HOSTS", ONLY_ESX02)
        monkeypatch.setattr(mod.macs, "assign", lambda vm_defs, reserved: None)
        vc.generated_mac = "00:50:56:ba:2b:47"
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(STARTER), {}))
        lost = {name for name, old, _ in vc.reassigned if old == "00:50:56:ba:2b:47"}
        assert lost and set(result.not_ready) == lost  # every VM vCenter changed, and only those
        assert len(result.vms) == 3 and result.status == "partial"
        for out in result.vms:
            if out["name"] in lost:
                assert out["ip"] == "" and out["ip_verified"] is False  # not the planned address
                assert out["planned_ip"] == STARTER_IPS[out["name"]]
            else:
                assert out["ip"] == STARTER_IPS[out["name"]] and out["ip_verified"] is True
        for name in lost:
            (err,) = [e for e in result.errors if e.startswith(f"VM {name}:")]
            assert f"planned address {STARTER_IPS[name]} not reported by VMware Tools" in err
        state, error = range_alloc.build_outcome(result)
        assert state == "failed" and all(n in error for n in lost)

    def test_windows_sysprep_maps_each_adapter_by_its_reserved_mac(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {}))
        assert result.status == "ok", result.errors
        out = _vm_out(result, "dc01")
        vm = vc.vms[out["vm_id"]]
        (spec,) = vm.customize_specs
        cards = infra.nic_cards(vm.config.hardware.device)
        assert [m.macAddress for m in spec.nicSettingMap] == [c.macAddress for c in cards]
        assert [c.macAddress for c in cards] == [n["mac"] for n in out["nics"]]
        assert out["ip"] == "10.60.201.10" and out["ip_verified"] is True

    @staticmethod
    def _squatter(vc, mac: str, name: str = "someone-elses-vm", annotation: str | None = None, folder=None):
        """A VM on no network of the range (a NIC on some unrelated port group) holding ``mac``."""
        vm = FakeVM(name, [_card(4000, "some-other-pg")])
        vm.config.hardware.device[0].macAddress = mac
        vm.config.annotation = annotation
        vm.parent = folder
        vc.vms[vm._moId] = vm
        return vm

    def test_a_reserved_mac_anywhere_in_the_vcenter_is_refused_before_any_clone(self, vc):
        """Any VM, on any network (another install sharing the slice, a VM left by an
        abandoned range), not only VMs on the range's own port groups."""
        squatter = self._squatter(vc, "00:50:56:3C:00:01")  # by hand, or another install
        self._squatter(vc, "00:50:56:aa:00:00", name="outside-the-pool")  # not in the slice: never read as a clash
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(STARTER), {}))
        assert result.status == "failed" and result.vms == []
        (err,) = [e for e in result.errors if "MAC" in e]
        assert f"MAC 00:50:56:3c:00:01 (for {R8}-ubu02#nic0) is already used by VM 'someone-elses-vm' " \
               f"({squatter._moId}), which is not this range's" in err
        assert "each install on a shared vCenter needs its own slice" in err and "outside-the-pool" not in err
        assert vc.inventory_reads == 1  # one property-collector query over every VM
        assert not vc.templates["tmpl-ubuntu-2404"].clone_specs  # nothing cloned
        assert vc.dvs.portgroup == []  # and the port group it made is rolled back

    @pytest.mark.parametrize("path", [f"truenorth/ranges/{R8}", f"TrueNorth/Ranges/{R8}",
                                      f"TrueNorth/Ranges/ubuntu-starter-01-{KEY}", f"TrueNorth/Ranges/renamed-{KEY}"])
    def test_a_redelivered_build_does_not_refuse_its_own_vms(self, vc, path):
        """The first delivery's VMs hold the same reserved MACs: by annotation, or (its tag
        not yet written) by sitting in the range's own folder, they are this range's. The
        folder is found by its id8 suffix (vsphere_tags), old layout, new layout or renamed."""
        folder = infra.ensure_folder(vc.dc, path)
        self._squatter(vc, "00:50:56:3c:00:00", name=f"{R8}-ubu01-attempt1", annotation=mod.range_annotation(RANGE_ID))
        self._squatter(vc, "00:50:56:3c:00:01", name=f"{R8}-ubu02-attempt1", folder=folder)
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(STARTER), {}))
        assert not [e for e in result.errors if "MAC" in e], result.errors
        assert result.status == "ok", result.errors

    def test_a_vm_in_another_ranges_folder_is_not_taken_for_this_ranges(self, vc):
        """Only this range's id8 suffix counts: a folder ending in another id (or merely
        containing this one) does not exempt its VMs."""
        for path in ("TrueNorth/Ranges/other-99999999", f"TrueNorth/Ranges/{R8}-copy", f"TrueNorth/Ranges/other-{R8}",
                     "TrueNorth/Ranges/x-abcdef129999"):  # the same id8, another range's 12-hex key
            self._squatter(vc, "00:50:56:3c:00:01", name="parked", folder=infra.ensure_folder(vc.dc, path))
            result = _run(_prov(vc).provision(RANGE_ID, _rendered(STARTER), {}))
            assert result.status == "failed" and any("VM 'parked'" in e for e in result.errors), (path, result.errors)
            vc.vms.clear()

    def test_an_old_id8_folder_does_not_exempt_another_ranges_vm(self, vc):
        """An earlier build's <id8> folder is shared by every range of that id8: a VM in it
        that is annotated for another range (same id8) is still that range's, not ours."""
        twin = f"{R8}-9999-4999-8999-999999999999"
        folder = infra.ensure_folder(vc.dc, f"truenorth/ranges/{R8}")
        self._squatter(vc, "00:50:56:3c:00:01", name=f"{R8}-twin", annotation=mod.range_annotation(twin), folder=folder)
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(STARTER), {}))
        assert result.status == "failed" and any(f"VM '{R8}-twin'" in e for e in result.errors), result.errors

    def test_another_ranges_vm_with_the_mac_is_named_not_blamed_on_this_range(self, vc):
        other = "99999999-0000-0000-0000-000000000000"
        self._squatter(vc, "00:50:56:3c:00:02", name="99999999-web", annotation=mod.range_annotation(other))
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(STARTER), {}))
        assert result.status == "failed"
        assert any("VM '99999999-web'" in e and "not this range's" in e for e in result.errors), result.errors

    def test_a_clone_without_its_reserved_mac_is_never_configured_or_powered_on(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_EXCLUDE_HOSTS", ONLY_ESX02)
        vc.ignore_manual = True  # a server that hands out its own MAC whatever the spec says
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(STARTER), {}))
        assert result.status == "failed" and result.vms == []
        vm_errors = [e for e in result.errors if e.startswith("VM ")]
        assert len(vm_errors) == 3 and all("do not have their reserved MACs" in e for e in vm_errors), result.errors
        assert not vc.vms  # each half-built VM deleted again
        assert "POWERED_ON" not in vc.power.values()

    def test_a_nic_without_a_reservation_is_refused_not_picked(self, vc):
        allocations = {"physical_vlans": {"10": "100"}, "nic_macs": {f"{R8}-ubu01#nic0": "00:50:56:3c:00:00"}}
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(STARTER), allocations))
        assert result.status == "failed" and not vc.vms
        assert any("have no MAC reservation" in e and f"{R8}-ubu02#nic0" in e for e in result.errors), result.errors


class TestMacPool:
    def test_the_default_slice_is_inside_vmwares_manual_range(self):
        pool = macs_mod.parse_pool("")
        assert (pool[0], pool[-1], len(pool)) == ("00:50:56:3c:00:00", "00:50:56:3f:ff:ff", 262144)
        assert list(itertools.islice(pool, 2)) == ["00:50:56:3c:00:00", "00:50:56:3c:00:01"]
        assert "00:50:56:3C:00:05" in pool and "00:50:56:ba:2b:47" not in pool

    @pytest.mark.parametrize("bad", ["00:50:56:40:00:00-00:50:56:40:00:ff",  # above the manual range
                                     "00:0c:29:00:00:00-00:0c:29:00:00:ff",  # ESXi's own generated prefix
                                     "00:50:56:3f:00:00-00:50:56:3c:00:00", "nonsense"])
    def test_a_pool_outside_the_manual_range_is_refused(self, bad):
        with pytest.raises(ValueError):
            macs_mod.parse_pool(bad)

    def test_two_nics_given_one_mac_are_refused(self):
        vms = [{"name": "a", "nics": [{}]}, {"name": "b", "nics": [{}]}]
        with pytest.raises(RuntimeError, match="reserved for both"):
            macs_mod.assign(vms, {"a#nic0": "00:50:56:3c:00:00", "b#nic0": "00:50:56:3C:00:00"})


class TestSpreadPlacement:
    def test_spread_deals_the_range_round_the_hosts_not_onto_the_emptiest(self):
        """Most-headroom-first packed: esx02 (64 threads, idle) beat busy esx03/esx04 for
        every VM of a small range."""
        hosts = [_cap("esx02", threads=64), _cap("esx03", vcpus=40), _cap("esx04", vcpus=40)]
        placed = infra.place(hosts, _vms(3, cores=2), "spread", 4)
        assert sorted(h.name for h, _ in placed) == ["esx02", "esx03", "esx04"]
        hosts = [_cap("esx02", threads=64), _cap("esx03"), _cap("esx04")]
        names = [h.name for h, _ in infra.place(hosts, _vms(7, cores=2), "spread", 4)]
        assert sorted(names.count(n) for n in ("esx02", "esx03", "esx04")) == [2, 2, 3]

    def test_spread_still_honours_capacity(self):
        hosts = [_cap("esx02"), _cap("esx03", free_mb=4_000), _cap("esx04", vcpus=62)]  # esx03: no RAM
        names = [h.name for h, _ in infra.place(hosts, _vms(3, cores=2, mem=8192), "spread", 4)]
        assert (names.count("esx02"), names.count("esx03"), names.count("esx04")) == (2, 0, 1)  # esx04: 2 vCPU
        with pytest.raises(infra.PlacementError, match="no host fits"):
            infra.place([_cap("esx03", free_mb=4_000)], _vms(1, mem=8192), "spread", 4)

    def test_spread_prefers_a_host_and_datastore_holding_the_template_copy(self):
        a, b = _cap("esx02"), _cap("esx03")
        b.datastores["esx03-small"] = [200 * GIB, "esx03-small"]  # holds the copy; less free space
        local = {"esx03": frozenset({"esx03-small"})}
        placed = infra.place([a, b], _vms(1), "spread", 4, local=lambda v, h: local.get(h.name, frozenset()))
        assert (placed[0][0].name, placed[0][1]) == ("esx03", "esx03-small")

    def test_spread_deals_across_the_hosts_with_a_local_copy_before_any_cross_host_copy(self):
        hosts = [_cap("esx02"), _cap("esx03"), _cap("esx04")]
        copies = {"esx02": frozenset({"esx02-local"}), "esx03": frozenset({"esx03-local"})}
        names = [h.name for h, _ in infra.place(hosts, _vms(4, cores=2), "spread", 4,
                                                local=lambda v, h: copies.get(h.name, frozenset()))]
        assert sorted(names) == ["esx02", "esx02", "esx03", "esx03"]  # esx04 would copy disks across hosts
        # Only when the hosts with a copy are full does a VM go to one without.
        hosts = [_cap("esx02", vcpus=60), _cap("esx03", vcpus=60), _cap("esx04")]  # 4 vCPU headroom each
        names = [h.name for h, _ in infra.place(hosts, _vms(3, cores=4), "spread", 4,
                                                local=lambda v, h: copies.get(h.name, frozenset()))]
        assert sorted(names) == ["esx02", "esx03", "esx04"]

    @pytest.mark.parametrize("exclude", ["esx01", "ESX01", "esx01.truenorth.lab", "esx01.other.domain"])
    def test_the_excluded_host_matches_by_short_name_or_fqdn(self, vc, monkeypatch, exclude):
        monkeypatch.setattr(mod, "VSPHERE_EXCLUDE_HOSTS", exclude)
        prov = _prov(vc)
        assert [h.name for h in prov._eligible_hosts(vc.cluster)] == [
            "esx02.truenorth.lab", "esx03.truenorth.lab", "esx04.truenorth.lab"]
        vc.hosts[0].name = "esx01"  # vCenter lists the host by its short name
        assert "esx01" not in [h.name for h in prov._eligible_hosts(vc.cluster)]
        monkeypatch.setattr(mod, "VSPHERE_EXCLUDE_HOSTS", "10.30.30.11")
        vc.hosts[0].name = "10.30.30.1"  # an IP is never cut down to "10"
        assert "10.30.30.1" in [h.name for h in _prov(vc)._eligible_hosts(vc.cluster)]

    def test_an_unknown_mode_is_an_error_not_silently_spread(self):
        with pytest.raises(infra.PlacementError, match="unknown VSPHERE_PLACEMENT"):
            infra.place([_cap("esx02")], _vms(1), "packed", 4)

    def test_the_starter_range_spans_esx02_to_esx04_from_their_local_copies(self, vc):
        result = _run(_prov(_starter_vc(vc)).provision(RANGE_ID, _rendered(STARTER), {}))
        assert result.status == "ok", result.errors
        hosts = sorted(out["host"] for out in result.vms)
        assert hosts == ["esx02.truenorth.lab", "esx03.truenorth.lab", "esx04.truenorth.lab"]  # never esx01
        for out in result.vms:
            short = out["host"].split(".")[0]
            assert out["template"] == f"tmpl-ubuntu-2404@{short}-20261019"
            assert out["datastore"] == f"{short}-local"
        assert not vc.templates["tmpl-ubuntu-2404"].clone_specs  # no cross-host disk copy


class TestReadiness:
    def test_a_vm_that_never_gets_its_ip_fails_the_range_with_the_reason(self, vc):
        """ubu02's guest network config does not apply: Tools runs and reports no address
        for it. Not ready, never shown with its planned address."""

        def no_address_on_ubu02(request):
            path = request.url.path
            if path.endswith("/guest/networking/interfaces") and vc.vms[path.split("/")[4]].name.endswith("ubu02"):
                return httpx.Response(200, json=[])
            return None

        prov = _with_handler(_prov(vc), vc, no_address_on_ubu02)
        result = _run(prov.provision(RANGE_ID, _rendered(STARTER), {}))
        assert result.not_ready == [f"{R8}-ubu02"] and len(result.vms) == 3
        ubu02 = next(v for v in result.vms if v["name"] == f"{R8}-ubu02")
        assert (ubu02["ip"], ubu02["planned_ip"], ubu02["ip_verified"], ubu02["guest_ips"], ubu02["tools_ready"]) == (
            "", "10.70.10.12", False, [], True)
        (err,) = [e for e in result.errors if e.startswith(f"VM {R8}-ubu02:")]
        assert "planned address 10.70.10.12 not reported by VMware Tools within 0s of power-on" in err
        assert "Tools reports no IPv4 address" in err
        others = [v for v in result.vms if v["name"] != f"{R8}-ubu02"]
        assert all(v["ip"] == v["planned_ip"] and v["ip_verified"] for v in others)
        state, error = range_alloc.build_outcome(result)
        assert state == "failed" and error.startswith(f"1 VM(s) built but not ready: VM {R8}-ubu02: planned")
        stored = range_alloc.stored_output("vsphere_api", RANGE_ID, result)
        assert stored["not_ready"] == [f"{R8}-ubu02"] and len(stored["vms"]) == 3  # destroy finds them all

    def test_no_roles_software_or_setups_on_a_vm_already_not_ready(self, vc):
        seen: dict[str, list] = {}

        def spy(name):
            async def record(si, vm_defs, outs, *a, **k):
                seen[name] = sorted(o["name"] for o in outs)
            return record

        def no_address_on_ubu02(request):
            path = request.url.path
            if path.endswith("/guest/networking/interfaces") and vc.vms[path.split("/")[4]].name.endswith("ubu02"):
                return httpx.Response(200, json=[])
            return None

        prov = _with_handler(_prov(vc), vc, no_address_on_ubu02)
        for name in ("_install_roles", "_install_software", "_setup_securityonion", "_verify_accounts"):
            setattr(prov, name, spy(name))
        result = _run(prov.provision(RANGE_ID, _rendered(STARTER), {}))
        assert result.not_ready == [f"{R8}-ubu02"]
        ready = [f"{R8}-ubu01", f"{R8}-ubu03"]
        assert seen == {"_install_roles": ready, "_install_software": ready, "_setup_securityonion": ready,
                        "_verify_accounts": ready}
        assert f"roles, software and setups skipped on VMs not ready: {R8}-ubu02" in result.warnings

    def test_tools_that_never_start_fail_a_linux_vm(self, vc):
        prov = _with_handler(_prov(vc), vc, lambda r: httpx.Response(200, json={"run_state": "NOT_RUNNING"})
                             if r.url.path.endswith("/tools") else None)
        result = _run(prov.provision(RANGE_ID, _rendered(ONE_STARTER), {}))
        assert result.not_ready == [f"{R8}-ubu01"] and result.vms[0]["ip"] == ""
        assert any("VMware Tools did not report running" in e and "10.70.10.11" in e for e in result.errors)

    def test_the_wait_is_bounded_and_polls_until_the_address_shows(self, vc):
        """cloud-init / Sysprep take a while: the address may show after a few polls."""
        polls = []

        def late(request):
            if request.url.path.endswith("/guest/networking/interfaces"):
                polls.append(1)
                return httpx.Response(200, json=[]) if len(polls) < 3 else None
            return None

        prov = _with_handler(_prov(vc), vc, late)
        prov._guest_ip_timeout = 30
        result = _run(prov.provision(RANGE_ID, _rendered(ONE_STARTER), {}))
        assert result.status == "ok", result.errors
        assert len(polls) == 3 and result.vms[0]["ip_verified"] is True

    def test_a_vm_powered_on_just_before_the_budget_ends_is_left_to_reconcile_not_failed(self, vc, monkeypatch):
        """Powered on ~40 s before the build's ``finish_by`` with a slow Sysprep/cloud-init:
        the budget, not VSPHERE_GUEST_IP_TIMEOUT, ends the wait. Not failed: the check is
        left running for the reconcile task, which later records it ready."""
        import time as real_time

        class Clock:  # 10 s pass per look at the clock, so the test takes none of them
            t = real_time.monotonic()
            sleep = staticmethod(lambda s: None)
            time = staticmethod(real_time.time)

            def monotonic(self):
                Clock.t += 10
                return Clock.t

        monkeypatch.setattr(mod, "time", Clock())
        slow = {"on": True}
        prov = _with_handler(_prov(vc), vc, lambda r: httpx.Response(200, json=[])
                             if slow["on"] and r.url.path.endswith("/guest/networking/interfaces") else None)
        prov._guest_ip_timeout = 600
        result = _run(prov.provision(RANGE_ID, _rendered(ONE_STARTER), {"finish_by": Clock.t + 60}))
        assert result.status == "ok" and result.not_ready == [], result.errors
        (out,) = result.vms
        assert out["ip_verified"] is None and out["ip"] == ""  # pending: neither verified nor the plan shown
        check = out["ip_check"]
        assert check["status"] == "running" and check["planned"] == ["10.70.10.11"]
        assert real_time.time() + 500 < check["deadline"] <= real_time.time() + 600  # the window's end, epoch
        assert any("guest network not verified yet" in w for w in result.warnings)
        assert range_alloc.build_outcome(result) == ("ready", None)
        stored = range_alloc.stored_output("vsphere_api", RANGE_ID, result)
        assert '"running"' in json.dumps(stored)  # what db_ops.ranges_with_running_setups selects on

        # Later, the reconcile: the guest has its address now.
        slow["on"] = False
        settled = _run(_prov(vc).reconcile_guest_ips(RANGE_ID, stored))
        assert settled == {out["name"]: {"guest_ips": ["10.70.10.11"], "ip": "10.70.10.11", "ip_verified": True,
                                         "ip_check": {"status": "ok"}}}

    def test_reconcile_leaves_a_check_open_inside_its_window_and_fails_it_after(self, vc):
        built = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_STARTER), {}))
        out = dict(built.vms[0], ip_check={"status": "running", "planned": ["10.70.10.99"], "deadline": 1000.0})
        prov = _prov(vc)
        assert _run(prov.reconcile_guest_ips(RANGE_ID, {"vms": [out]}, now=999.0)) == {}
        (verdict,) = _run(prov.reconcile_guest_ips(RANGE_ID, {"vms": [out]}, now=1001.0)).values()
        assert verdict["ip_verified"] is False and verdict["ip_check"]["status"] == "failed"
        assert "planned address 10.70.10.99 not reported" in verdict["not_ready"]
        assert verdict["guest_ips"] == ["10.70.10.11"]  # what Tools does report, recorded

    def test_reconcile_never_reads_a_vm_that_is_not_this_ranges(self, vc):
        built = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_STARTER), {}))
        out = dict(built.vms[0], ip_check={"status": "running", "planned": ["10.70.10.11"], "deadline": 0})
        vc.vms[out["vm_id"]].config.annotation = mod.range_annotation("another-range-entirely")
        assert _run(_prov(vc).reconcile_guest_ips(RANGE_ID, {"vms": [out]}, now=1.0)) == {}

    def test_a_node_opted_out_with_verify_ip_false_is_recorded_unverified_not_failed(self, vc):
        """A deliberately Tools-less vulnerable image: ``verify_ip: false`` on the node (or
        the template's default) keeps it from failing the range."""
        tpl = {**STARTER, "nodes": [{**STARTER["nodes"][0], "verify_ip": False}, STARTER["nodes"][1]]}
        rendered = _rendered(tpl)
        assert [v.get("verify_ip") for v in rendered["vms"]] == [False, None]
        assert [v.get("verify_ip") for v in _rendered({**STARTER, "verify_ip": False})["vms"]] == [False] * 3
        node_wins = {**STARTER, "verify_ip": False, "nodes": [{**STARTER["nodes"][0], "verify_ip": True}]}
        assert _rendered(node_wins)["vms"][0].get("verify_ip") is None
        prov = _with_handler(_prov(vc), vc, lambda r: httpx.Response(200, json={"run_state": "NOT_RUNNING"})
                             if r.url.path.endswith("/tools") else None)  # no Tools anywhere
        result = _run(prov.provision(RANGE_ID, rendered, {}))
        assert result.not_ready == [f"{R8}-ubu02"]  # the one still held to it
        opted = next(v for v in result.vms if v["name"] == f"{R8}-ubu01")
        assert (opted["ip_verified"], opted["ip"], opted["tools_ready"]) == (None, "10.70.10.11", False)
        assert not [e for e in result.errors if "ubu01" in e]

    def test_an_appliance_without_tools_is_recorded_unverified_not_failed(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {}))
        assert result.status == "ok", result.errors
        fw = _vm_out(result, "fw")
        assert (fw["tools_ready"], fw["ip_verified"], fw["ip"]) == (False, None, "10.60.200.1")

    def test_the_check_can_be_switched_off_for_a_simulator(self, vc):
        prov = _with_handler(_prov(vc), vc, lambda r: httpx.Response(200, json={"run_state": "NOT_RUNNING"})
                             if r.url.path.endswith("/tools") else None)
        prov._guest_ip_check = False
        result = _run(prov.provision(RANGE_ID, _rendered(ONE_STARTER), {}))
        assert result.status == "ok" and result.not_ready == []
        assert result.vms[0]["ip_verified"] is False and result.vms[0]["ip"] == ""  # recorded, not invented


# --------------------------------------------------------------------------- #
# Range folders <root>/<range-name>-<id8> and vCenter tags (vsphere_tags.py)
# --------------------------------------------------------------------------- #


def _vm_obj(vc, name: str) -> str:
    return next(i for i, v in vc.vms.items() if v.name == name)


def _output(prov, result) -> dict:
    return {"vms": result.vms, "networks": result.networks, "connection": prov.connection_record()}


class TestRangeFolderAndTags:
    def test_folder_is_named_for_the_range_and_its_id(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {"range_name": "Blue Team 3"}))
        assert result.status == "ok", result.errors
        folder = infra.find_folder(vc.dc, f"TrueNorth/Ranges/blue-team-3-{KEY}")
        assert folder is not None and {v.name for v in folder.childEntity} == {v["name"] for v in result.vms}

    def test_every_range_vm_is_tagged(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {"range_name": "Blue Team 3"}))
        assert result.status == "ok", result.errors
        web = vc.cis.tags_of(_vm_obj(vc, f"{R8}-web01"))
        assert web == {"tn-type": "range-vm", "tn-os": "ubuntu-2404", "tn-template": "tmpl-ubuntu-2404",
                       "tn-range": f"blue-team-3-{KEY}"}
        assert vc.cis.tags_of(_vm_obj(vc, f"{R8}-dc01"))["tn-os"] == "windows-server-2022"
        assert vc.cis.tags_of(_vm_obj(vc, f"{R8}-fw"))["tn-template"] == "tmpl-pfsense"
        for name in tags_mod.CATEGORIES:  # created once, single cardinality, for VMs
            (cat,) = [c for c in vc.cis.categories.values() if c["name"] == name]
            assert cat["cardinality"] == "SINGLE" and cat["associable_types"] == ["VirtualMachine"]
        assert sum(1 for t in vc.cis.tags.values() if t["name"] == f"blue-team-3-{KEY}") == 1

    def test_a_vm_from_a_host_copy_is_tagged_with_the_catalogue_id(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_EXCLUDE_HOSTS", ONLY_ESX02)
        _add_copy(vc, "tmpl-ubuntu-2404@esx02-20261019", "esx02")
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {}))
        assert result.vms[0]["template"] == "tmpl-ubuntu-2404@esx02-20261019"
        assert vc.cis.tags_of(result.vms[0]["vm_id"])["tn-template"] == "tmpl-ubuntu-2404"
        assert vc.cis.tags_of(result.vms[0]["vm_id"])["tn-range"] == KEY  # no range name given: the id

    def test_existing_categories_and_tags_are_reused(self, vc):
        vc.cis.add_tag("tn-type", "range-vm")
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {"range_name": "x"}))
        assert result.status == "ok", result.errors
        assert sum(1 for c in vc.cis.categories.values() if c["name"] == "tn-type") == 1
        assert sum(1 for t in vc.cis.tags.values() if t["name"] == "range-vm") == 1
        assert vc.cis.tags_of(result.vms[0]["vm_id"])["tn-type"] == "range-vm"

    def test_a_tagging_failure_never_fails_the_build(self, vc, caplog):
        vc.cis.fail = "*"
        caplog.set_level("WARNING", logger=tags_mod.__name__)
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {"range_name": "r"}))
        assert result.status == "ok", result.errors
        assert len(result.vms) == 4 and not result.errors
        # One warning for the build, not one per VM, and no call after the first failure.
        assert caplog.text.count("vCenter tagging stopped for the rest of this build (the build goes on)") == 1
        assert len(vc.cis.calls) == 1

    def test_a_destroy_whose_tag_delete_fails_is_still_ok(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {"range_name": "r"}))
        vc.cis.fail = "*"
        gone = _run(_prov(vc).destroy(RANGE_ID, _output(_prov(vc), result)))
        assert gone.status == "ok", gone.errors

    def test_tags_can_be_switched_off(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_TAGS", False)
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {}))
        assert result.status == "ok", result.errors
        assert _run(_prov(vc).destroy(RANGE_ID, _output(_prov(vc), result))).status == "ok"
        assert vc.cis.calls == []

    def test_a_retried_build_reuses_the_ranges_folder(self, vc):
        infra.ensure_folder(vc.dc, f"TrueNorth/Ranges/old-name-{KEY}")
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {"range_name": "new name"}))
        assert result.status == "ok", result.errors
        assert infra.find_folder(vc.dc, f"TrueNorth/Ranges/new-name-{KEY}") is None
        assert infra.find_folder(vc.dc, f"TrueNorth/Ranges/old-name-{KEY}").childEntity

    @pytest.mark.parametrize("path", [f"truenorth/ranges/{R8}", f"TrueNorth/Ranges/{R8}",
                                      f"TrueNorth/Ranges/renamed-since-{KEY}"])
    def test_destroy_finds_the_folder_by_id_in_old_and_new_layouts(self, vc, path):
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {"range_name": "r"}))
        built = infra.find_folder(vc.dc, f"TrueNorth/Ranges/r-{KEY}")
        target = infra.ensure_folder(vc.dc, path)  # where an operator (or vcenter-organize.sh) put it
        target.childEntity.extend(built.childEntity)
        built.parent.childEntity.remove(built)
        gone = _run(_prov(vc).destroy(RANGE_ID, _output(_prov(vc), result)))
        assert gone.status == "ok", gone.errors
        assert infra.find_folder(vc.dc, path) is None

    def test_destroy_leaves_other_ranges_folders_and_tags(self, vc):
        other = infra.ensure_folder(vc.dc, "TrueNorth/Ranges/other-0badc0de0000")
        other.CreateFolder("keep")
        keep = vc.cis.add_tag("tn-range", "other-0badc0de0000")
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {"range_name": "r"}))
        assert _run(_prov(vc).destroy(RANGE_ID, _output(_prov(vc), result))).status == "ok"
        assert infra.find_folder(vc.dc, "TrueNorth/Ranges/other-0badc0de0000") is not None
        assert keep in vc.cis.tags and not any(t["name"] == f"r-{KEY}" for t in vc.cis.tags.values())

    def test_the_legacy_dialect_tags_over_the_legacy_api(self, vc):
        prov = _legacy(vc)
        result = _run(prov.provision(RANGE_ID, _rendered(ONE_LINUX), {"range_name": "sim"}))
        assert result.status == "ok", result.errors
        assert vc.cis.tags_of(result.vms[0]["vm_id"])["tn-range"] == f"sim-{KEY}"
        assert vc.cis.calls and all(p.startswith("/rest/com/vmware/cis/tagging/") for _, p in vc.cis.calls)

    def test_an_old_id8_folder_is_never_reused_for_a_new_build(self, vc):
        """Every range of that id8 would match an earlier build's <id8> folder; a new build
        gets its own <name>-<key> folder and leaves the old one (and what is in it) alone."""
        for path in (f"truenorth/ranges/{R8}", f"TrueNorth/Ranges/{R8}"):
            infra.ensure_folder(vc.dc, path).CreateFolder("someone-elses")
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {"range_name": "r"}))
        assert result.status == "ok", result.errors
        assert [v.name for v in infra.find_folder(vc.dc, f"TrueNorth/Ranges/r-{KEY}").childEntity] == [
            result.vms[0]["name"]]
        assert [c.name for c in infra.find_folder(vc.dc, f"TrueNorth/Ranges/{R8}").childEntity] == ["someone-elses"]

    @pytest.mark.parametrize("bad", ["", "/", " / ", "a/../b"])
    def test_an_empty_or_root_range_folder_is_refused(self, vc, monkeypatch, bad):
        """VSPHERE_RANGE_FOLDER empty or "/" would make the datacenter's VM root the range
        root, where a destroy's sweep by key would reach every folder: refused at start."""
        monkeypatch.setattr(mod, "VSPHERE_RANGE_FOLDER", bad)
        with pytest.raises(ValueError, match="refusing to use the datacenter's VM root"):
            mod.VsphereAPIProvisioner()

    def test_teardown_refuses_a_root_range_folder_and_touches_nothing(self, vc):
        victim = vc.dc.vmFolder.CreateFolder(f"x-{KEY}")  # at the VM root: must survive
        prov = _prov(vc)
        prov._range_folder = ""  # as if it had been set after construction
        with pytest.raises(ValueError):
            prov._teardown_sync(vc.si(), RANGE_ID, [])
        assert victim in vc.dc.vmFolder.childEntity

    def test_a_retired_template_is_never_a_clone_source(self, vc):
        vc.templates["tmpl-ubuntu-2404"].parent = SimpleNamespace(name="_retired")
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {}))
        assert result.status == "failed"
        assert any("is neither a Content Library item nor an inventory VM template" in e for e in result.errors)
        assert not vc.templates["tmpl-ubuntu-2404"].clone_specs

    def test_a_slow_tagging_service_stops_tagging_once_and_the_build_goes_on(self, vc, caplog):
        """One timed-out call stops tagging for the whole build: one warning, no more calls."""
        started: list[str] = []

        async def slow(request):
            if vc.cis.handles(request.url.path):
                started.append(request.url.path)
                await asyncio.sleep(0.5)
            return vc.handler(request)

        prov = _prov(vc)
        prov._transport = httpx.MockTransport(slow)
        prov._tag_timeout = 0.05
        caplog.set_level("WARNING", logger=tags_mod.__name__)
        result = _run(prov.provision(RANGE_ID, _rendered(), {"range_name": "r"}))
        assert result.status == "ok" and len(result.vms) == 4, result.errors
        assert caplog.text.count("vCenter tagging stopped for the rest of this build") == 1
        assert started == ["/api/cis/tagging/category"]  # the one that timed out; nothing after it

    def test_no_tagging_in_the_last_minutes_of_the_build_budget(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_PROVISION_BUDGET", 60)  # the budget's end is inside the tag cut-off
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(ONE_LINUX), {"range_name": "r"}))
        assert result.status == "ok", result.errors
        assert vc.cis.calls == []


# --------------------------------------------------------------------------- #
# Range certification S1: the live switch, read before allocation and again right
# before each port group is created (vsphere_vlans.py)
# --------------------------------------------------------------------------- #


PLANNED = {200: 150, 201: 151, 203: 152}  # what the worker reserved for TEMPLATE's three VLANs


def _squat(vc, name, vlan, description="", trunk=None):
    pg = FakePortgroup(vc.dvs, infra.dvs_portgroup_spec(name, vlan, False))
    pg.config.description = description
    if trunk:
        pg.config.defaultPortConfig = vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy(
            vlan=vim.dvs.VmwareDistributedVirtualSwitch.TrunkVlanSpec(
                vlanId=[vim.NumericRange(start=a, end=b) for a, b in trunk]))
    vc.dvs.portgroup.append(pg)
    return pg


class TestLiveVlanRecheck:
    def test_a_vlan_that_appears_mid_build_stops_it_and_undoes_this_steps_port_groups(self, vc):
        """v150 is created; then someone puts a port group on 151: nothing further is made,
        v150 goes again, no VM is cloned, and every planned VLAN is released."""
        real = vc.dvs.CreateDVPortgroup_Task

        def create_then_squat(spec):
            task = real(spec)
            if spec.name == f"tn-{R8}-v150":
                _squat(vc, "corp-new", 151)
            return task

        vc.dvs.CreateDVPortgroup_Task = create_then_squat
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {"physical_vlans": dict(PLANNED)}))
        assert result.status == "failed" and result.vms == [] and not vc.vms
        assert "VLAN 151 is already used by port group 'corp-new' on vDS-10G" in result.errors[0]
        assert "the build stopped before creating anything further" in result.errors[0]
        assert [pg.name for pg in vc.dvs.portgroup] == ["corp-new"]  # v150 undone; the newcomer untouched
        assert result.conflict == {"domain": "vsphere:vlans", "source": "vcsa.test/vDS-10G",
                                   "vlans": {"151": "corp-new"}, "release": ["150", "151", "152"]}

    def test_created_port_groups_carry_the_owner_mark(self, vc):
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {"physical_vlans": dict(PLANNED)}))
        assert result.status == "ok", result.errors
        assert {pg.spec.description for pg in vc.dvs.portgroup} == {f"TrueNorth range {RANGE_ID}"}

    def test_a_port_group_with_this_ranges_name_but_not_its_own_is_never_reused_or_removed(self, vc):
        squat = _squat(vc, f"tn-{R8}-v150", 999, description="made by hand")
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {"physical_vlans": dict(PLANNED)}))
        assert result.status == "failed" and not vc.vms
        assert f"port group 'tn-{R8}-v150' already exists on vDS-10G with VLAN 999" in result.errors[0]
        assert vc.dvs.portgroup == [squat]
        assert result.conflict["release"] == ["150", "151", "152"]
        assert result.conflict["vlans"] == {}  # VLAN 150 itself is free: not recorded as external
        # Its destroy goes by the recorded names: the hand-made one (VLAN 999) stays.
        nets = [{"portgroup": f"tn-{R8}-v{v}", "physical_vlan": v, "switch_mode": "vds"} for v in PLANNED.values()]
        assert _run(_prov(vc).destroy(RANGE_ID, {"vms": [], "networks": nets})).status == "ok"
        assert vc.dvs.portgroup == [squat]

    def test_a_trunk_covering_a_planned_vlan_stops_the_build(self, vc):
        _squat(vc, "nested-esx", 0, trunk=[(140, 160)])
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {"physical_vlans": dict(PLANNED)}))
        assert result.status == "failed" and not vc.vms
        assert "VLAN 150 is already used by trunk port group 'nested-esx'" in result.errors[0]
        assert [pg.name for pg in vc.dvs.portgroup] == ["nested-esx"]

    def test_a_retry_keeps_the_port_group_an_earlier_attempt_made(self, vc):
        mine = _squat(vc, f"tn-{R8}-v150", 150)  # pre-S1: no owner mark
        _squat(vc, "corp-new", 152)
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {"physical_vlans": dict(PLANNED)}))
        assert result.status == "failed"
        assert sorted(pg.name for pg in vc.dvs.portgroup) == ["corp-new", f"tn-{R8}-v150"]  # v151 undone
        assert mine in vc.dvs.portgroup
        assert result.conflict["release"] == ["151", "152"]  # v150 is still held: its destroy removes it

    def test_the_uplink_port_group_trunks_everything_and_is_ignored(self, vc):
        _squat(vc, "vDS-10G-DVUplinks-35", 0, trunk=[(0, 4094)])
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {"physical_vlans": dict(PLANNED)}))
        assert result.status == "ok", result.errors

    def test_live_usage_reads_the_switch_before_allocation(self, vc):
        _squat(vc, "vDS-10G-DVUplinks-35", 0, trunk=[(0, 4094)])
        _squat(vc, "corp-voice", 101)
        _squat(vc, "nested-esx", 0, trunk=[(104, 105)])
        _squat(vc, "tn-0badc0de-v106", 106)
        prov = _prov(vc)
        needs = prov.allocation_needs(RANGE_ID, _rendered())
        (usage,) = prov.live_usage(needs)
        assert (usage.domain, usage.kind, usage.source) == ("vsphere:vlans", "vlan", "vcsa.test/vDS-10G")
        assert usage.foreign == {"101": "corp-voice", "104": "nested-esx", "105": "nested-esx"}
        assert usage.managed == {"106": "tn-0badc0de-v106"}

    def test_the_switch_is_read_once_per_step_not_once_per_port_group(self, vc):
        """One property-collector call reads every port group (name, config, tag); the
        build's recheck reuses it for all three VLANs, and so does the pre-allocation read."""
        for i in range(40):  # a busy switch: the call count must not grow with it either
            _squat(vc, f"corp-{i}", 400 + i)
        prov = _prov(vc)
        prov.live_usage(prov.allocation_needs(RANGE_ID, _rendered()))
        assert vc.portgroup_reads == 1
        result = _run(prov.provision(RANGE_ID, _rendered(), {"physical_vlans": dict(PLANNED)}))
        assert result.status == "ok", result.errors
        assert vc.portgroup_reads == 2  # + one for the whole _prepare_sync step

    def test_a_conflict_reads_the_switch_once_more_and_no_more(self, vc):
        _squat(vc, "corp-new", 152)
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {"physical_vlans": dict(PLANNED)}))
        assert result.status == "failed" and vc.portgroup_reads == 2  # the step's read + what is kept

    def test_a_missing_switch_fails_the_live_read(self, vc, monkeypatch):
        monkeypatch.setattr(mod, "VSPHERE_RANGE_DVS", "vDS-gone")
        prov = _prov(vc)
        with pytest.raises(RuntimeError, match="Distributed switch not found"):
            prov.live_usage(prov.allocation_needs(RANGE_ID, _rendered()))


def _vss_plant(host, name, vlan):
    host.configManager.networkSystem.AddPortGroup(portgrp=infra.vss_portgroup_spec(name, vlan, "vSwitch1", False))


def _vss_names(host) -> dict:
    return {p.spec.name: p.spec.vlanId for p in host.config.network.portgroup}


class TestLiveVlanRecheckVss:
    """The same rules on standard vSwitches: the recheck reads exactly the hosts the port
    groups go on, a conflict undoes only those hosts' port groups, and nothing removes a
    same-named port group carrying another VLAN."""

    @pytest.fixture(autouse=True)
    def _vss(self, vc, monkeypatch):  # after vc, which sets "vds"
        monkeypatch.setattr(mod, "VSPHERE_RANGE_SWITCH_MODE", "vss")

    def test_a_vlan_on_the_hosts_stops_the_build_and_undoes_this_steps_port_groups(self, vc):
        for host in vc.hosts:
            _vss_plant(host, "corp", 151)
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {"physical_vlans": dict(PLANNED)}))
        assert result.status == "failed" and not vc.vms
        assert "VLAN 151 is already used by port group 'corp' on vSwitch1" in result.errors[0]
        assert all(_vss_names(h) == {"corp": 151} for h in vc.hosts)  # v150 undone everywhere it was made
        assert result.conflict["vlans"] == {"151": "corp"}
        assert result.conflict["release"] == ["150", "151", "152"]

    def test_a_conflict_never_touches_a_host_the_build_did_not_use(self, vc):
        """esx01 (excluded: the VCSA host) has its own tn-<range8>-v150 on VLAN 150; the
        build's v150 on the placed hosts is undone, esx01's is not."""
        esx01 = vc.hosts[0]
        _vss_plant(esx01, f"tn-{R8}-v150", 150)
        for host in vc.hosts[1:]:
            _vss_plant(host, "corp", 151)
        result = _run(_prov(vc).provision(RANGE_ID, _rendered(), {"physical_vlans": dict(PLANNED)}))
        assert result.status == "failed"
        assert _vss_names(esx01) == {f"tn-{R8}-v150": 150}
        assert all(_vss_names(h) == {"corp": 151} for h in vc.hosts[1:])

    def test_teardown_leaves_a_same_named_port_group_on_another_vlan(self, vc):
        _vss_plant(vc.hosts[1], f"tn-{R8}-v150", 999)  # made by hand on esx02
        _vss_plant(vc.hosts[2], f"tn-{R8}-v150", 150)  # the range's own on esx03
        nets = [{"portgroup": f"tn-{R8}-v150", "physical_vlan": 150, "switch_mode": "vss"}]
        assert _run(_prov(vc).destroy(RANGE_ID, {"vms": [], "networks": nets})).status == "ok"
        assert _vss_names(vc.hosts[1]) == {f"tn-{R8}-v150": 999}
        assert _vss_names(vc.hosts[2]) == {}
