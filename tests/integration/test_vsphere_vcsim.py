"""The vsphere_api provisioner against vcsim, govmomi's vCenter simulator.

tests/worker/test_vsphere_provision.py drives the provisioner through hand-made fakes;
here every pyVmomi call goes to a real implementation of the vSphere Web Services API
(vcsim: real managed objects, property collector, tasks and faults), and what the
provisioner built is read back from the simulator, not from what the code sent.

What vcsim does NOT implement, and how this test handles it (govmomi v0.56.0):

* The Automation REST API under ``/api``: vcsim serves ``POST /api/session`` as 404
  (only the legacy ``/rest/com/vmware/cis/session``), and has no
  ``/api/vcenter/vm/{vm}/power``, ``/tools`` or ``/guest/networking/interfaces`` (404).
  ``DELETE /api/vcenter/vm/{vm}`` it does implement. ``VcsimRestBridge`` below is the
  provisioner's httpx transport: it forwards what vcsim has (DELETE), logs in through the
  legacy session endpoint and checks every session token against vcsim, and translates
  the missing calls 1:1 to their SOAP equivalents on the same simulator. So the REST
  *paths and payload shapes* are not validated by vcsim (the bridge encodes our reading
  of the vCenter 8 API), while their effect (power state, deletion) is real.
* VMware Tools never reports running in vcsim for a non-container VM, so
  ``tools_ready`` is False and no guest IP is read: the build falls back to the rendered
  IP. Guest operations (deploy-time software installs) are not exercised: they need a
  running guest.
* Content Library / OVF deploy (``/api/content/library``, ``/api/vcenter/ovf``) are not
  exercised: vcsim only has the legacy ``/rest/com/vmware/...`` forms. The lab has no
  library either (VSPHERE_CONTENT_LIBRARY empty -> inventory-template clone path).

Run it (vcsim + govc on PATH or ~/go/bin; see scripts/dev/vcsim-up.sh for installing):

    VCSIM=1 .venv/bin/python -m pytest tests/integration/test_vsphere_vcsim.py -v

or against a running, already-seeded simulator: ``VCSIM_URL=https://user:pass@host:8989``.
``VCSIM=0`` skips it. tests/integration is outside the DoD/CI unit selection.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import re
import shutil
import socket
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.vcsim]

pytest.importorskip("pyVmomi")
httpx = pytest.importorskip("httpx")
yaml = pytest.importorskip("yaml")

from pyVim.connect import Disconnect, SmartConnect  # noqa: E402
from pyVim.task import WaitForTask  # noqa: E402
from pyVmomi import vim, vmodl  # noqa: E402
from worker import pfsense_config, render  # noqa: E402
from worker.provisioners import vsphere_api as mod  # noqa: E402
from worker.provisioners import vsphere_infra as infra  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SEED = REPO / "scripts" / "dev" / "vcsim-up.sh"
RANGE_ID = "5eed1234-0000-4000-8000-00000000c0de"
R8 = RANGE_ID[:8]
DC, CLUSTER, DVS = "DC0", "DC0_C0", "vDS-10G"
VCSA_HOST = "DC0_C0_H0"  # esx01 on site: runs the VCSA, never takes range VMs
MGMT = ("dPG-TN-MGMT", "VM Network")


def _vcsim_bin() -> str | None:
    for cand in (os.getenv("VCSIM_BIN"), shutil.which("vcsim"), str(Path.home() / "go" / "bin" / "vcsim")):
        if cand and os.access(cand, os.X_OK):
            return cand
    return None


if os.getenv("VCSIM") == "0":
    pytest.skip("VCSIM=0", allow_module_level=True)
if not os.getenv("VCSIM_URL") and not _vcsim_bin():
    pytest.skip("no vcsim binary (VCSIM_BIN, PATH, ~/go/bin) and no VCSIM_URL", allow_module_level=True)
if not shutil.which("govc"):
    pytest.skip("govc is needed to seed the simulator (brew install govc)", allow_module_level=True)


# --------------------------------------------------------------------------- #
# The simulator
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module", autouse=True)
def _vcsim_wire_gaps():
    """Two places where vcsim's SOAP differs from vCenter's, patched in pyVmomi for these
    tests only (neither is the provisioner's doing; both break any pyVmomi client):

    1. vcsim leaves ``runtime.faultToleranceState`` and ``runtime.recordReplayState``
       empty on every VM; vCenter sends ``notConfigured`` / ``inactive``. pyVmomi 8 rejects
       an empty enum (``getattr(FaultToleranceState, '')``), failing every read of
       ``vm.runtime`` / ``vm.summary``. Mapped to what vCenter sends.
    2. pyVmomi 8 reads a property with the ``Fetch`` call. vcsim turns Fetch into
       RetrievePropertiesEx and, when the property is unset (``vm.snapshot`` on a VM with
       no snapshots), sends back the RetrievePropertiesEx body itself
       (simulator/property_collector.go, ``return res``), which pyVmomi cannot parse
       (``AttributeError: objects``). vCenter answers an empty Fetch: the property is unset.
    3. vcsim implements ``AddDVPortgroup_Task(spec[])`` but not ``CreateDVPortgroup_Task(spec)``
       (MethodNotFound); vCenter has both (vSphere 4.0+). The provisioner's spec is sent
       unchanged through the one vcsim has.
    4. vcsim names its tasks after its Go runner (``TaskInfo.name = "CloneVm"``,
       simulator/task.go) where vCenter sends a vmodl method name. pyVmomi decodes the
       unknown name as ``UnknownManagedMethod``, and pyVim's ``WaitForTask`` then dies on
       ``info.name.info.name`` whenever it first sees a task still running. Given an
       ``info`` that names itself, WaitForTask runs as it does against vCenter.
    """
    from pyVmomi import SoapAdapter, VmomiSupport

    real = SoapAdapter.SoapStubAdapter.InvokeAccessor

    def invoke_accessor(self, mo, info):
        try:
            return real(self, mo, info)
        except AttributeError as exc:
            if exc.args != ("objects",):
                raise
            return info.type() if info.type.__name__.endswith("[]") else None

    fts, rrs = vim.VirtualMachine.FaultToleranceState, vim.VirtualMachine.RecordReplayState
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fts, "", fts.notConfigured, raising=False)
        mp.setattr(rrs, "", rrs.inactive, raising=False)
        mp.setattr(SoapAdapter.SoapStubAdapter, "InvokeAccessor", invoke_accessor)
        mp.setattr(vim.DistributedVirtualSwitch, "CreateDVPortgroup_Task",
                   lambda self, spec: self.AddDVPortgroup_Task([spec]))
        mp.setattr(VmomiSupport.UnknownManagedMethod, "info", property(lambda self: self), raising=False)
        yield


@pytest.fixture(scope="module")
def vcsim():
    """A seeded simulator: {"url", "user", "password", "host", "port", "version"}."""
    proc = None
    if os.getenv("VCSIM_URL"):
        u = urlparse(os.environ["VCSIM_URL"])
        host, port = u.hostname, u.port or 443
        user, password = u.username or "user", u.password or "pass"
    else:
        host, port, user, password = "127.0.0.1", _free_port(), "user", "pass"
        proc = subprocess.Popen(
            [_vcsim_bin(), "-l", f"{host}:{port}", "-api-version", "8.0.3.0", "-dc", "1", "-cluster", "1", "-host", "4", "-ds", "4",
             "-pod", "0", "-app", "0", "-folder", "0", "-pg", "1", "-vm", "2", "-standalone-host", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        line = proc.stdout.readline()  # "export GOVC_URL=... GOVC_SIM_PID=..." once it listens
        if "GOVC_URL" not in line:
            proc.kill()
            pytest.fail(f"vcsim did not start: {line!r}")
    env = {**os.environ, "GOVC_URL": f"https://{user}:{password}@{host}:{port}/sdk", "GOVC_INSECURE": "1"}
    seeded = subprocess.run(["bash", str(SEED), "--seed-only"], env=env, capture_output=True, text=True)
    if seeded.returncode:
        if proc:
            proc.kill()
        pytest.fail(f"seeding vcsim failed:\n{seeded.stdout}\n{seeded.stderr}")
    version = subprocess.run(["govc", "about"], env=env, capture_output=True, text=True).stdout
    try:
        yield {"url": f"https://{host}:{port}", "user": user, "password": password, "host": host, "port": port,
               "version": version}
    finally:
        if proc:
            proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)


@pytest.fixture(scope="module")
def si(vcsim):
    """An independent pyVmomi session for reading back what the provisioner built."""
    conn = SmartConnect(host=vcsim["host"], port=vcsim["port"], user=vcsim["user"], pwd=vcsim["password"],
                        disableSslCertValidation=True)
    yield conn
    Disconnect(conn)


def _all(si, vimtype) -> list:
    view = si.content.viewManager.CreateContainerView(si.content.rootFolder, [vimtype], True)
    try:
        return list(view.view)
    finally:
        view.Destroy()


def _by_name(si, vimtype, name):
    return next((o for o in _all(si, vimtype) if o.name == name), None)


# --------------------------------------------------------------------------- #
# REST: vcsim's /api is partial; forward what it has, translate the rest to SOAP
# --------------------------------------------------------------------------- #

_POWER = {"poweredOn": "POWERED_ON", "poweredOff": "POWERED_OFF", "suspended": "SUSPENDED"}


class VcsimRestBridge(httpx.AsyncBaseTransport):
    """The provisioner's REST transport. ``log`` records (method, path, how) per call,
    ``how`` being ``vcsim`` (vcsim served it), ``soap`` (translated) or ``404``."""

    def __init__(self, sim: dict):
        self._sim = sim
        self._net, self._net_loop = None, None
        self._si = SmartConnect(host=sim["host"], port=sim["port"], user=sim["user"], pwd=sim["password"],
                                disableSslCertValidation=True)
        self.log: list[tuple[str, str, str]] = []

    async def aclose(self) -> None:
        # Every httpx client the provisioner opens closes its transport on exit; this one is
        # shared by all of them (on site each client has its own), so closing the pool here
        # cut off other clients' requests in flight (ReadError <- ClosedResourceError).
        pass

    def close(self) -> None:
        Disconnect(self._si)

    async def _vcsim(self, method: str, path: str, headers: dict) -> httpx.Response:
        req = httpx.Request(method, f"{self._sim['url']}{path}", headers=headers)
        loop = asyncio.get_running_loop()
        if self._net_loop is not loop:  # pooled connections belong to the loop that opened them
            self._net, self._net_loop = httpx.AsyncHTTPTransport(verify=False), loop
        resp = await self._net.handle_async_request(req)
        await resp.aread()
        return resp

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path, query, method = request.url.path, request.url.query.decode(), request.method
        if method == "POST" and path == "/api/session":
            # vCenter 7+: POST /api/session -> 201 "token". vcsim: legacy endpoint only.
            resp = await self._vcsim("POST", "/rest/com/vmware/cis/session",
                                     {"authorization": request.headers.get("authorization", "")})
            self.log.append((method, path, "vcsim:/rest/com/vmware/cis/session"))
            if resp.status_code != 200:
                return httpx.Response(resp.status_code, content=resp.content)
            return httpx.Response(201, json=resp.json()["value"])
        token = request.headers.get("vmware-api-session-id", "")
        check = await self._vcsim("GET", "/rest/com/vmware/cis/session", {"vmware-api-session-id": token})
        if check.status_code != 200:  # vcsim rejects the token: the provisioner must log in again
            self.log.append((method, path, "vcsim:401"))
            return httpx.Response(401, json={"error_type": "UNAUTHENTICATED"})
        if method == "DELETE" and re.fullmatch(r"/api/vcenter/vm/[^/]+", path):
            resp = await self._vcsim("DELETE", path, {"vmware-api-session-id": token})
            self.log.append((method, path, "vcsim"))
            return httpx.Response(204 if resp.status_code == 200 else resp.status_code, content=b"" if
                                  resp.status_code == 200 else resp.content)
        if m := re.fullmatch(r"/api/vcenter/vm/([^/]+)/(power|tools)", path):
            self.log.append((method, path + (f"?{query}" if query else ""), "soap"))
            return await asyncio.to_thread(self._soap, m.group(1), m.group(2), method, query)
        self.log.append((method, path, "404"))
        return httpx.Response(404, json={"error_type": "NOT_FOUND", "messages": []})

    def _soap(self, vm_id: str, what: str, method: str, query: str) -> httpx.Response:
        vm = vim.VirtualMachine(vm_id, self._si._stub)
        try:
            state = vm.runtime.powerState
            if what == "tools":
                running = vm.guest.toolsRunningStatus == "guestToolsRunning"
                return httpx.Response(200, json={"run_state": "RUNNING" if running else "NOT_RUNNING"})
            if method == "GET":
                return httpx.Response(200, json={"state": _POWER.get(state, state)})
            want = {"action=start": "poweredOn", "action=stop": "poweredOff"}[query]
            if state == want:  # what vCenter's /api answers for a no-op power change
                return httpx.Response(400, json={"error_type": "ALREADY_IN_DESIRED_STATE", "messages": []})
            WaitForTask(vm.PowerOnVM_Task() if want == "poweredOn" else vm.PowerOffVM_Task(), si=self._si)
            return httpx.Response(204)
        except vmodl.fault.ManagedObjectNotFound:
            return httpx.Response(404, json={"error_type": "NOT_FOUND", "messages": []})


# --------------------------------------------------------------------------- #
# The range
# --------------------------------------------------------------------------- #

SMALL = {"cores": 1, "memory_mb": 512, "disk_gb": 8}  # vcsim hosts: 2 threads, 4 GiB
TEMPLATE = {
    "name": "vcsim-lab",
    "network": {"vlans": [
        {"id": 201, "name": "victim_network", "cidr": "10.60.201.0/24"},
        {"id": 203, "name": "network_monitoring", "cidr": "10.60.203.0/24"},
    ]},
    "nodes": [
        {"id": "fw", "role": "firewall", "os": "pfsense", "vlan": "victim_network", "specs": SMALL,
         "interfaces": [{"vlan": "victim_network"}, {"vlan": "network_monitoring"}]},
        {"id": "web01", "role": "server", "os": "ubuntu-2404", "vlan": "victim_network", "ip": "10.60.201.20",
         "specs": SMALL},
        {"id": "sensor", "role": "network_monitor", "os": "ubuntu-2404", "vlan": "network_monitoring",
         "specs": SMALL},
        {"id": "dc01", "role": "domain_controller", "os": "windows-server-2022", "vlan": "victim_network",
         "ip": "10.60.201.10", "specs": SMALL},
    ],
}
IMAGES = {"pfsense": "tmpl-pfsense", "ubuntu-2404": "tmpl-ubuntu-2404", "windows-server-2022": "tmpl-win2022"}


def _rendered() -> dict:
    out = render.render_topology(TEMPLATE, RANGE_ID, IMAGES.get)
    return {"name": TEMPLATE["name"], "vms": out["vm_definitions"], "networks": out["network_definitions"]}


@pytest.fixture
def prov(vcsim, monkeypatch):
    """The provisioner, configured as the worker would be from the environment."""
    for name, value in {
        "VSPHERE_URL": vcsim["url"], "VSPHERE_USERNAME": vcsim["user"], "VSPHERE_PASSWORD": vcsim["password"],
        "VSPHERE_DATACENTER": DC, "VSPHERE_CLUSTER": CLUSTER, "VSPHERE_DATASTORE": "LocalDS_0",
        "VSPHERE_NETWORK": "", "VSPHERE_MGMT_NETWORK": ",".join(MGMT), "VSPHERE_CONTENT_LIBRARY": "",
        "VSPHERE_VERIFY_SSL": False, "VSPHERE_RANGE_SWITCH_MODE": "vds", "VSPHERE_RANGE_DVS": DVS,
        "VSPHERE_VLAN_POOL": "100-199", "VSPHERE_PLACEMENT": "spread", "VSPHERE_RANGE_FOLDER": "truenorth/ranges",
        "VSPHERE_EXCLUDE_HOSTS": VCSA_HOST, "VSPHERE_MAX_VCPU_PER_THREAD": 4.0,
        "VSPHERE_TOOLS_TIMEOUT": 1,  # vcsim never reports Tools running (see the module docstring)
        "VSPHERE_RANGE_UPLINK_NETWORK": "", "TN_DEPOT_URL": "", "VSPHERE_SNAPSHOT_TIMEOUT": 120,
    }.items():
        monkeypatch.setattr(mod, name, value)
    p = mod.VsphereAPIProvisioner()
    bridge = VcsimRestBridge(vcsim)
    p._transport = bridge
    p.bridge = bridge
    yield p
    bridge.close()


def _run(coro):
    return asyncio.run(coro)


def _range_vms(si) -> list:
    return [v for v in _all(si, vim.VirtualMachine) if v.name.startswith(f"{R8}-")]


def _guestinfo(vm) -> dict:
    return {o.key: o.value for o in vm.config.extraConfig if o.key.startswith("guestinfo.")}


def test_simulator_version(vcsim):
    """Recorded in the test output: which vcsim this ran against."""
    assert "govmomi simulator" in vcsim["version"], vcsim["version"]
    print(vcsim["version"])


def test_range_lifecycle(vcsim, si, prov):
    used = [100]  # held by another range
    # Like the lab's template (imported from Ubuntu's cloud-image OVA): a vApp config with
    # the guestinfo OVF transport, which Linux clones must not inherit.
    tmpl = _by_name(si, vim.VirtualMachine, "tmpl-ubuntu-2404")
    if tmpl.config.vAppConfig is None:  # a template cannot be reconfigured: VM, reconfigure, template
        cluster = _by_name(si, vim.ClusterComputeResource, CLUSTER)
        tmpl.MarkAsVirtualMachine(pool=cluster.resourcePool, host=tmpl.runtime.host)
        WaitForTask(tmpl.ReconfigVM_Task(vim.vm.ConfigSpec(vAppConfig=vim.vApp.VmConfigSpec(
            ovfEnvironmentTransport=["com.vmware.guestInfo"],
            product=[vim.vApp.ProductSpec(operation="add", info=vim.vApp.ProductInfo(key=0, name="Ubuntu"))],
        ))), si=si)
        tmpl.MarkAsTemplate()
    assert tmpl.config.vAppConfig is not None
    rendered = _rendered()
    # Where spread placement should put each VM, from the simulator's capacity before the
    # build (vcsim puts its two default powered-on VMs on random hosts, so this varies).
    cluster = _by_name(si, vim.ClusterComputeResource, CLUSTER)
    capacity = [c for h in cluster.host if h.name != VCSA_HOST and (c := infra.host_capacity(h))]
    expected = {f"{R8}-{v['node_id']}": host.name
                for v, (host, _) in zip(rendered["vms"], infra.place(capacity, rendered["vms"], "spread", 4.0),
                                        strict=True)}
    result = _run(prov.provision(RANGE_ID, rendered, {"used_vlans": used}))
    print("REST calls:", sorted({(m, re.sub(r"vm-\d+", "{vm}", p), how) for m, p, how in prov.bridge.log}))
    assert result.status == "ok", result.errors

    # --- port groups: from the pool past the used VLAN, with the security policy ----------
    dvs = _by_name(si, vim.DistributedVirtualSwitch, DVS)
    pgs = {pg.name: pg for pg in dvs.portgroup if pg.name.startswith(f"tn-{R8}-")}
    assert sorted(pgs) == [f"tn-{R8}-v101", f"tn-{R8}-v102"]
    by_net = {n["name"]: n for n in result.networks}
    assert by_net["victim_network"]["physical_vlan"] == 101
    assert by_net["network_monitoring"]["physical_vlan"] == 102
    for name, pg in pgs.items():
        port = pg.config.defaultPortConfig
        assert port.vlan.vlanId == int(name.rsplit("v", 1)[1])
        sec = port.securityPolicy
        assert sec.allowPromiscuous.value is (name.endswith("v102"))  # the monitoring zone only
        assert sec.forgedTransmits.value is False and sec.macChanges.value is False
        assert pg.config.type == "earlyBinding"

    # --- VMs: in the range folder, spread over the eligible hosts, never the VCSA host ----
    folder = infra.find_folder(_by_name(si, vim.Datacenter, DC), f"truenorth/ranges/{R8}")
    assert folder is not None
    vms = {v.name: v for v in folder.childEntity}
    assert sorted(vms) == sorted(f"{R8}-{n}" for n in ("fw", "web01", "sensor", "dc01"))
    assert sorted(v.name for v in _range_vms(si)) == sorted(vms)
    reported = {v["name"]: v for v in result.vms}
    hosts = {}
    for name, vm in vms.items():
        host = vm.runtime.host
        assert host.name == reported[name]["host"] != VCSA_HOST
        hosts.setdefault(host.name, []).append(name)
        assert vm.runtime.powerState == "poweredOn"
        assert vm.config.template is False
        assert vm.config.hardware.numCPU == 1 and vm.config.hardware.memoryMB == 512
        # Disks on a datastore of the VM's own host: the one placement chose.
        disks = [d for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)]
        assert disks
        for disk in disks:
            assert disk.backing.datastore in host.datastore
            assert disk.backing.datastore.name == reported[name]["datastore"]
            assert disk.backing.fileName.startswith(f"[{reported[name]['datastore']}]")
    assert {name: vm.runtime.host.name for name, vm in vms.items()} == expected
    assert len(hosts) >= 2, hosts  # spread: 4 one-vCPU VMs never land on a single 2-thread host

    # --- NICs: one per zone, on the range port groups, never the management network -----
    key = {pg.key: pg.name for pg in pgs.values()}
    mgmt_keys = {pg.key for pg in dvs.portgroup if pg.name in MGMT}
    want = {"fw": [f"tn-{R8}-v101", f"tn-{R8}-v102"], "web01": [f"tn-{R8}-v101"],
            "sensor": [f"tn-{R8}-v102"], "dc01": [f"tn-{R8}-v101"]}
    for node, pg_names in want.items():
        vm = vms[f"{R8}-{node}"]
        cards = infra.nic_cards(vm.config.hardware.device)
        assert [key.get(c.backing.port.portgroupKey) for c in cards] == pg_names, node
        for c in cards:
            assert isinstance(c.backing, vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo)
            assert c.backing.port.switchUuid == dvs.uuid
            assert c.backing.port.portgroupKey not in mgmt_keys
            assert c.connectable.startConnected is True
        assert {n.name for n in vm.network}.isdisjoint(MGMT)
        assert [n["network"] for n in reported[f"{R8}-{node}"]["nics"]] == pg_names

    # --- customization: cloud-init guestinfo on Linux, Sysprep on Windows, config.xml on pfSense
    for node, ip in (("web01", "10.60.201.20"), ("sensor", None)):
        vm = vms[f"{R8}-{node}"]
        extra = _guestinfo(vm)
        assert extra["guestinfo.metadata.encoding"] == "base64"
        meta = yaml.safe_load(base64.b64decode(extra["guestinfo.metadata"]))
        assert meta["local-hostname"] == node and meta["instance-id"] == f"{R8}-{node}"
        eth = meta["network"]["ethernets"]["nic0"]
        # The MAC vCenter generated for the clone's NIC, read after the clone.
        assert eth["match"]["macaddress"] == infra.nic_cards(vm.config.hardware.device)[0].macAddress.lower()
        if ip:
            assert eth["addresses"] == [f"{ip}/24"]
        assert base64.b64decode(extra["guestinfo.userdata"]).decode().startswith("#cloud-config")
    assert not _guestinfo(vms[f"{R8}-dc01"])
    # pfSense: its per-range config.xml, read back from the simulator, NICs named by the
    # MACs vcsim generated for the clone.
    fw = vms[f"{R8}-fw"]
    extra = _guestinfo(fw)
    assert set(extra) == {"guestinfo.tn.pfsense.config", "guestinfo.tn.pfsense.ifmap"}
    macs = [c.macAddress.lower() for c in infra.nic_cards(fw.config.hardware.device)]
    assert extra["guestinfo.tn.pfsense.ifmap"] == f"vmx0={macs[0]} vmx1={macs[1]}"
    fw_cfg = ET.fromstring(pfsense_config.decode(extra["guestinfo.tn.pfsense.config"]))
    assert [(el.tag, el.findtext("ipaddr")) for el in fw_cfg.find("interfaces")] == [
        ("wan", "10.60.201.1"), ("lan", "10.60.203.1")]  # no uplink here: the first zone takes WAN
    assert reported[f"{R8}-fw"]["pfsense"]["config_sha256"] == hashlib.sha256(
        pfsense_config.decode(extra["guestinfo.tn.pfsense.config"]).encode()).hexdigest()
    # vcsim accepted the clone spec with vAppConfigRemoved from a vApp template. It proves no
    # more: vcsim ignores vAppConfigRemoved and never copies vAppConfig to a clone (unit test).
    assert vms[f"{R8}-web01"].config.vAppConfig is None and tmpl.config.vAppConfig is not None
    dc01 = vms[f"{R8}-dc01"]
    # vcsim applies a pending Sysprep spec at power-on: hostname and the static IP.
    assert dc01.guest.customizationInfo.customizationStatus == "TOOLSDEPLOYPKG_SUCCEEDED"
    assert dc01.guest.hostName == "dc01"
    assert dc01.guest.ipAddress == "10.60.201.10"
    for vm in result.vms:  # vcsim limitation: Tools never runs, so the rendered IP is reported
        assert vm["tools_ready"] is False

    # --- day-2 operations -----------------------------------------------------------------
    ids = {v["name"]: v["vm_id"] for v in result.vms}
    assert all(vms[n]._moId == i for n, i in ids.items())
    output = {"vms": result.vms, "networks": result.networks}

    health = _run(prov.health_check(RANGE_ID, output))
    assert health.healthy, health.errors

    stopped = _run(prov.stop(RANGE_ID, output))
    assert stopped.status == "ok" and stopped.vms_stopped == 4, stopped.errors
    assert {vm.runtime.powerState for vm in vms.values()} == {"poweredOff"}
    again = _run(prov.stop(RANGE_ID, output))  # idempotent: already off is not an error
    assert again.status == "ok", again.errors

    started = _run(prov.start(RANGE_ID, output))
    assert started.status == "ok" and started.vms_started == 4, started.errors
    assert {vm.runtime.powerState for vm in vms.values()} == {"poweredOn"}

    snap = _run(prov.snapshot(RANGE_ID, output, "baseline"))
    assert snap.status == "ok" and snap.vms_snapped == 4, snap.errors
    for vm in vms.values():
        assert [s.name for s in mod._named_snapshots(vm, "baseline")] == ["baseline"]

    restored = _run(prov.restore(RANGE_ID, output, "baseline", True))
    assert restored.status == "ok" and restored.vms_restored == 4 and restored.vms_reverted == 4, restored.errors
    assert {vm.runtime.powerState for vm in vms.values()} == {"poweredOn"}
    missing = _run(prov.restore(RANGE_ID, output, "no-such-snapshot", False))
    assert missing.status == "failed" and missing.vms_reverted == 0

    deleted = _run(prov.delete_snapshot(RANGE_ID, output, "baseline"))
    assert deleted.status == "ok" and deleted.vms_cleaned == 4, deleted.errors
    assert all(not mod._named_snapshots(vm, "baseline") for vm in vms.values())

    gone = _run(prov.destroy(RANGE_ID, output))
    assert gone.status == "ok", gone.errors
    assert gone.resources_removed == 4 + 2  # VMs, then port groups
    assert _range_vms(si) == []
    assert not [pg for pg in dvs.portgroup if pg.name.startswith(f"tn-{R8}-")]
    assert infra.find_folder(_by_name(si, vim.Datacenter, DC), f"truenorth/ranges/{R8}") is None

    twice = _run(prov.destroy(RANGE_ID, output))  # a retried destroy: everything already gone
    assert twice.status == "ok", twice.errors


def test_failed_build_leaves_nothing_behind(vcsim, si, prov):
    """A template that is not in the inventory: every VM fails, the port groups are rolled back."""
    rendered = _rendered()
    for vm in rendered["vms"]:
        vm["template_name"] = "tmpl-does-not-exist"
    result = _run(prov.provision(RANGE_ID, rendered, {}))
    assert result.status == "failed"
    assert all("neither a Content Library item nor an inventory VM template" in e for e in result.errors), \
        result.errors
    dvs = _by_name(si, vim.DistributedVirtualSwitch, DVS)
    assert not [pg for pg in dvs.portgroup if pg.name.startswith(f"tn-{R8}-")]
    assert _range_vms(si) == []
    assert infra.find_folder(_by_name(si, vim.Datacenter, DC), f"truenorth/ranges/{R8}") is None


def test_vlan_already_on_the_switch_is_refused(vcsim, si, prov):
    """A pool VLAN some non-range port group carries (dPG-TN-MGMT is VLAN 30) is refused."""
    result = _run(prov.provision(RANGE_ID, _rendered(), {"physical_vlans": {201: 30, 203: 131}}))
    assert result.status == "failed"
    assert any("VLAN 30 is already used by port group 'dPG-TN-MGMT'" in e for e in result.errors), result.errors
    assert _range_vms(si) == []


def test_unreachable_vcenter_fails_cleanly(vcsim, prov):
    """Nothing listening at VSPHERE_URL: a failed result, not a hang or an exception."""
    prov._base_url = f"https://127.0.0.1:{_free_port()}"
    started = time.monotonic()
    result = _run(prov.provision(RANGE_ID, _rendered(), {}))
    assert result.status == "failed" and result.errors
    assert time.monotonic() - started < 60

