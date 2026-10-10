"""The vsphere_api provisioner against vcsim, govmomi's vCenter simulator (v0.56.0).

tests/worker/test_vsphere_provision.py drives the provisioner through hand-made fakes. Here
every call goes to a real implementation of the vSphere APIs (vcsim: real managed objects,
property collector, tasks and faults), and what the provisioner built is read back from
the simulator through a separate session, not from what the code sent.

Runs against vcsim's default inventory, the one CI's ``vsphere-sim`` job starts
(infra/platform/docker/compose.vcsim.yml): datacenter DC0, cluster DC0_C0, distributed
switch DVS0. The only seeding is one inventory VM template, cloned from a VM vcsim ships.

    docker compose -p tn-vcsim -f infra/platform/docker/compose.vcsim.yml up -d --wait
    VSPHERE_URL=https://127.0.0.1:8989 .venv/bin/python -m pytest -m vcsim tests/integration

Skipped when nothing answers at VSPHERE_URL (or VCSIM_URL), and refused outright when what
answers is not the simulator: these tests create and delete VMs and port groups.

What vcsim v0.56.0 does not do, and how this file handles it:

* REST: ``POST /api/session`` is 404; only the legacy ``/rest/com/vmware/cis/session``
  exists, and no ``/api/vcenter/vm/...`` power, Tools or guest calls. That is what the
  provisioner's dialect detection is for: it logs in through the legacy endpoint and does
  VM operations over the Web Services API. ``test_login_falls_back_to_the_legacy_session``
  checks the detection against the real endpoint.
* Content Library / OVF deploy (``/api/content/library``, ``/api/vcenter/ovf``): vcsim has
  only the legacy ``/rest/com/vmware/content`` forms, so the OVF path is NOT covered here
  (the fakes cover it). Builds clone an inventory template, as the lab does.
* VMware Tools never runs in a vcsim VM: ``tools_ready`` is False and the guest IP is the
  rendered one. Guest operations (deploy-time installs) and guest customization results
  are not exercised.
* Four SOAP differences from vCenter that break any pyVmomi client are patched for this
  module only (``_vcsim_wire_gaps``); none is the provisioner's doing.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from urllib.parse import urlparse

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.vcsim]

pytest.importorskip("pyVmomi")
httpx = pytest.importorskip("httpx")

from pyVim.connect import Disconnect, SmartConnect  # noqa: E402
from pyVmomi import vim, vmodl  # noqa: E402
from worker import render  # noqa: E402
from worker.provisioners import vsphere_api as mod  # noqa: E402
from worker.provisioners import vsphere_infra as infra  # noqa: E402

URL = os.getenv("VCSIM_URL") or os.getenv("VSPHERE_URL") or "https://127.0.0.1:8989"
USER = os.getenv("VSPHERE_USERNAME") or "user"
PASSWORD = os.getenv("VSPHERE_PASSWORD") or "pass"
DC = os.getenv("VSPHERE_DATACENTER") or "DC0"
CLUSTER = os.getenv("VSPHERE_CLUSTER") or "DC0_C0"
DVS = "DVS0"
TEMPLATE_VM = "tn-sim-tmpl-ubuntu"
SOURCE_VM = "DC0_C0_RP0_VM0"


def _run(coro):
    return asyncio.run(coro)


def _reachable() -> bool:
    try:
        httpx.get(f"{URL}/about", verify=False, timeout=5.0)
    except Exception:  # noqa: BLE001
        return False
    return True


if not _reachable():
    pytest.skip(f"no vCenter simulator at {URL} (start compose.vcsim.yml)", allow_module_level=True)


# --------------------------------------------------------------------------- #
# The simulator
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module", autouse=True)
def _vcsim_wire_gaps():
    """Where vcsim's SOAP differs from vCenter's, patched in pyVmomi for this module only.

    1. vcsim leaves ``runtime.faultToleranceState`` / ``runtime.recordReplayState`` empty;
       vCenter sends ``notConfigured`` / ``inactive``. pyVmomi rejects an empty enum.
    2. pyVmomi reads one property with ``Fetch``; for an unset property (``vm.snapshot``
       of a VM without snapshots) vcsim answers with a RetrievePropertiesEx body pyVmomi
       cannot parse (``AttributeError: objects``). vCenter answers an empty Fetch.
    3. vcsim has ``AddDVPortgroup_Task(spec[])`` but not ``CreateDVPortgroup_Task(spec)``
       (MethodNotFound); vCenter has both. The provisioner's spec goes through unchanged.
    4. vcsim names tasks after its Go runner (``TaskInfo.name = "CloneVm"``); pyVmomi
       decodes that as ``UnknownManagedMethod``, which has no ``info``.
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
def si(_vcsim_wire_gaps):
    """An independent session for seeding and for reading back what the provisioner built."""
    u = urlparse(URL)
    conn = SmartConnect(host=u.hostname, port=u.port or 443, user=USER, pwd=PASSWORD,
                        disableSslCertValidation=True)
    if "simulator" not in (conn.content.about.fullName or ""):
        Disconnect(conn)
        pytest.fail(f"{URL} is {conn.content.about.fullName!r}, not vcsim: refusing to create and delete VMs there")
    yield conn
    Disconnect(conn)


def _all(si, vimtype) -> list:
    view = si.content.viewManager.CreateContainerView(si.content.rootFolder, [vimtype], True)
    try:
        return list(view.view)
    finally:
        view.Destroy()


def _name(obj) -> str | None:
    try:
        return obj.name
    except vmodl.fault.ManagedObjectNotFound:  # deleted since the view was read
        return None


def _named(si, vimtype, name):
    return next((o for o in _all(si, vimtype) if _name(o) == name), None)


def _wait(si, task, timeout=120):
    mod._wait_task(task, si, timeout)


@pytest.fixture(scope="module")
def template(si):
    """One inventory VM template (what Packer leaves behind on site), cloned from a vcsim VM."""
    tmpl = _named(si, vim.VirtualMachine, TEMPLATE_VM)
    if tmpl is None:
        source = _named(si, vim.VirtualMachine, SOURCE_VM)
        assert source is not None, f"vcsim's default inventory has no {SOURCE_VM}"
        if source.runtime.powerState != "poweredOff":
            _wait(si, source.PowerOffVM_Task())
        task = source.CloneVM_Task(folder=source.parent, name=TEMPLATE_VM,
                                   spec=vim.vm.CloneSpec(location=vim.vm.RelocateSpec(), powerOn=False))
        _wait(si, task)
        tmpl = task.info.result
        tmpl.MarkAsTemplate()
    yield tmpl
    with contextlib.suppress(Exception):
        _wait(si, tmpl.Destroy_Task())


@pytest.fixture
def env(monkeypatch, template):
    """The provisioner's settings for the simulator's default inventory."""
    for name, value in {
        "VSPHERE_URL": URL, "VSPHERE_USERNAME": USER, "VSPHERE_PASSWORD": PASSWORD, "VSPHERE_DATACENTER": DC,
        "VSPHERE_CLUSTER": CLUSTER, "VSPHERE_VERIFY_SSL": False, "VSPHERE_CONTENT_LIBRARY": "",
        "VSPHERE_NETWORK": "", "VSPHERE_MGMT_NETWORK": "VM Network", "VSPHERE_RANGE_SWITCH_MODE": "vds",
        "VSPHERE_RANGE_DVS": DVS, "VSPHERE_VLAN_POOL": "100-199", "VSPHERE_PLACEMENT": "spread",
        "VSPHERE_EXCLUDE_HOSTS": "", "VSPHERE_RANGE_UPLINK_NETWORK": "", "TN_DEPOT_URL": "",
        "VSPHERE_TOOLS_TIMEOUT": 0, "VSPHERE_RESOURCE_POOL": "", "VSPHERE_RANGE_FOLDER": "truenorth/ranges",
    }.items():
        monkeypatch.setattr(mod, name, value)


# Two networks, one VM on each: the smallest range that shows isolation.
RANGE = {
    "name": "sim",
    "network": {"vlans": [{"id": 10, "name": "red", "cidr": "10.10.10.0/24"},
                          {"id": 20, "name": "blue", "cidr": "10.10.20.0/24"}]},
    "nodes": [
        {"id": "attacker", "os": "ubuntu-2404", "vlan": "red", "specs": {"cores": 1, "memory_mb": 512, "disk_gb": 1}},
        {"id": "victim", "os": "ubuntu-2404", "vlan": "blue", "specs": {"cores": 1, "memory_mb": 512, "disk_gb": 1}},
    ],
}


def _template(range_id: str) -> dict:
    out = render.render_topology(RANGE, range_id, lambda alias: TEMPLATE_VM)
    return {"name": "sim", "vms": out["vm_definitions"], "networks": out["network_definitions"]}


def _reserve(prov, range_id: str, template: dict, taken=()) -> dict:
    """The worker's part (worker/range_alloc.py), without a database: the lowest free values."""
    allocations = {}
    for need in prov.allocation_needs(range_id, template):
        free = [v for v in need.pool if v not in {str(t) for t in taken}]
        got = dict(zip(need.holders, free, strict=False))
        allocations[need.key] = got[need.holders[0]] if need.single else got
    return allocations


def _portgroups(si, prefix: str) -> dict:
    dvs = _named(si, vim.DistributedVirtualSwitch, DVS)
    return {pg.name: pg for pg in dvs.portgroup if pg.name.startswith(prefix)}


@pytest.fixture
def range_id(si):
    rid = str(uuid.uuid4())
    yield rid
    # Whatever a failed test left behind: VMs tagged with this range, then its port groups.
    for vm in _all(si, vim.VirtualMachine):
        with contextlib.suppress(Exception):
            if mod.annotated_range(vm.config.annotation) == rid:
                if vm.runtime.powerState != "poweredOff":
                    _wait(si, vm.PowerOffVM_Task())
                _wait(si, vm.Destroy_Task())
    for pg in _portgroups(si, f"tn-{rid[:8]}-").values():
        with contextlib.suppress(Exception):
            _wait(si, pg.Destroy_Task())


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_login_falls_back_to_the_legacy_session(env):
    prov = mod.VsphereAPIProvisioner()
    token = _run(prov._get_session())
    assert token and prov._dialect == "rest"
    # The token is a live session on the simulator, not just a string.
    resp = httpx.get(f"{URL}/rest/com/vmware/cis/session", headers={"vmware-api-session-id": token}, verify=False)
    assert resp.status_code == 200 and resp.json()["value"]["user"]
    _run(prov._logout())
    gone = httpx.get(f"{URL}/rest/com/vmware/cis/session", headers={"vmware-api-session-id": token}, verify=False)
    assert gone.status_code == 401


def test_finds_the_datacenter_and_cluster(env, si):
    prov = mod.VsphereAPIProvisioner()
    with prov._vim() as own:
        dc, cluster = prov._datacenter_and_cluster(own)
        assert (dc.name, cluster.name) == (DC, CLUSTER)
        hosts = prov._eligible_hosts(cluster)
        assert hosts and all(h.threads > 0 and h.datastores for h in hosts)


def test_a_two_network_range_lifecycle(env, si, range_id):
    prov = mod.VsphereAPIProvisioner()
    template = _template(range_id)
    allocations = _reserve(prov, range_id, template)
    assert allocations == {"physical_vlans": {"10": "100", "20": "101"}}

    built = _run(prov.provision(range_id, template, allocations))
    assert built.status == "ok", built.errors
    assert prov._dialect == "rest"
    r8 = range_id[:8]

    # One port group per network, on its own physical VLAN, read back from the simulator.
    pgs = _portgroups(si, f"tn-{r8}-")
    assert sorted(pgs) == [f"tn-{r8}-v100", f"tn-{r8}-v101"]
    assert {n: pg.config.defaultPortConfig.vlan.vlanId for n, pg in pgs.items()} == {
        f"tn-{r8}-v100": 100, f"tn-{r8}-v101": 101}
    # Each VM has exactly one NIC, on its own network's port group; tagged with the range.
    want = {f"{r8}-attacker": pgs[f"tn-{r8}-v100"].key, f"{r8}-victim": pgs[f"tn-{r8}-v101"].key}
    for out in built.vms:
        vm = vim.VirtualMachine(out["vm_id"], si._stub)
        nics = infra.nic_cards(vm.config.hardware.device)
        assert [n.backing.port.portgroupKey for n in nics] == [want[vm.name]]
        assert mod.annotated_range(vm.config.annotation) == range_id
        assert vm.runtime.powerState == "poweredOn"
        assert vm.parent.name == r8 and vm.parent.parent.name == "ranges"
    assert {v["vm_id"] for v in _run(prov.find_vms(f"{range_id}-"))} == {v["vm_id"] for v in built.vms}

    output = {"vms": built.vms, "networks": built.networks}
    stopped = _run(mod.VsphereAPIProvisioner().stop(range_id, output))
    assert stopped.status == "ok" and stopped.vms_stopped == 2, stopped.errors
    assert {vim.VirtualMachine(v["vm_id"], si._stub).runtime.powerState for v in built.vms} == {"poweredOff"}
    health = _run(mod.VsphereAPIProvisioner().health_check(range_id, output))
    assert not health.healthy and {s["status"] for s in health.vm_statuses} == {"powered_off"}
    started = _run(mod.VsphereAPIProvisioner().start(range_id, output))
    assert started.status == "ok" and started.vms_started == 2, started.errors
    assert _run(mod.VsphereAPIProvisioner().health_check(range_id, output)).healthy

    gone = _run(mod.VsphereAPIProvisioner().destroy(range_id, output))
    assert gone.status == "ok", gone.errors
    assert gone.resources_removed == 2 + 2
    names = {_name(vm) for vm in _all(si, vim.VirtualMachine)}
    assert not names & set(want)
    assert _portgroups(si, f"tn-{r8}-") == {}
    assert _run(mod.VsphereAPIProvisioner().find_vms(f"{range_id}-")) == []
    again = _run(mod.VsphereAPIProvisioner().destroy(range_id, output))
    assert again.status == "ok", again.errors  # a retried destroy finds everything gone


def test_a_vlan_another_port_group_carries_builds_nothing(env, si, range_id):
    """The VLAN pool must be TrueNorth's alone: a clash is refused, and the half-made
    range (its other port group) is rolled back."""
    dvs = _named(si, vim.DistributedVirtualSwitch, DVS)
    squatter = f"squat-{range_id[:8]}"
    _wait(si, dvs.CreateDVPortgroup_Task(infra.dvs_portgroup_spec(squatter, 101, False)))
    try:
        prov = mod.VsphereAPIProvisioner()
        template = _template(range_id)
        result = _run(prov.provision(range_id, template, _reserve(prov, range_id, template)))
        assert result.status == "failed" and result.vms == []
        assert any("VLAN 101 is already used" in e for e in result.errors), result.errors
        assert _portgroups(si, f"tn-{range_id[:8]}-") == {}  # v100 was made, then rolled back
        assert _run(prov.find_vms(f"{range_id}-")) == []
    finally:
        pg = _named(si, vim.dvs.DistributedVirtualPortgroup, squatter)
        if pg is not None:
            _wait(si, pg.Destroy_Task())


def test_snapshot_restore_and_delete(env, si, range_id):
    prov = mod.VsphereAPIProvisioner()
    template = _template(range_id)
    built = _run(prov.provision(range_id, template, _reserve(prov, range_id, template)))
    assert built.status == "ok", built.errors
    output = {"vms": built.vms, "networks": built.networks}
    try:
        snap = _run(mod.VsphereAPIProvisioner().snapshot(range_id, output, "tn-baseline"))
        assert snap.status == "ok" and snap.vms_snapped == 2, snap.errors
        restored = _run(mod.VsphereAPIProvisioner().restore(range_id, output, "tn-baseline", True))
        assert restored.status == "ok" and restored.vms_reverted == 2, restored.errors
        assert {vim.VirtualMachine(v["vm_id"], si._stub).runtime.powerState for v in built.vms} == {"poweredOn"}
        cleaned = _run(mod.VsphereAPIProvisioner().delete_snapshot(range_id, output, "tn-baseline"))
        assert cleaned.status == "ok" and cleaned.vms_cleaned == 2, cleaned.errors
        assert all(vim.VirtualMachine(v["vm_id"], si._stub).snapshot is None for v in built.vms)
    finally:
        assert _run(mod.VsphereAPIProvisioner().destroy(range_id, output)).status == "ok"


def test_a_missing_vm_is_reported_not_counted(env, si):
    output = {"vms": [{"name": "ghost", "vm_id": "vm-999999"}]}
    result = _run(mod.VsphereAPIProvisioner().stop("ghost-range", output))
    assert result.status == "failed" and result.vms_stopped == 0
    assert result.errors and result.errors[0].startswith("VM ghost:")
    with pytest.raises(vmodl.fault.ManagedObjectNotFound):
        vim.VirtualMachine("vm-999999", si._stub).runtime  # noqa: B018 -- the simulator agrees it is gone


def test_a_windows_server_role_sizes_the_clone_and_grows_its_disk(env, si, range_id, monkeypatch):
    """An image role (Exchange): the role image is cloned (here the seeded template stands
    in for it), sized to the role's floor, its system disk grown, read back from vcsim.
    Image roles need no guest operations, which vcsim cannot run anyway. Cluster placement:
    vcsim's hosts have under 3 GiB free, less than any role's floor."""
    monkeypatch.setattr(mod, "VSPHERE_PLACEMENT", "cluster")
    monkeypatch.setattr(mod, "VSPHERE_DATASTORE", "LocalDS_0")
    tpl = {"name": "sim-roles", "network": {"vlans": [{"id": 10, "name": "lan", "cidr": "10.10.10.0/24"}]},
           "nodes": [{"id": "exch", "os": "windows-server-2022", "vlan": "lan", "services": ["exchange", "owa"],
                      "specs": {"cores": 1, "memory_mb": 512, "disk_gb": 1}}]}
    asked: list[str] = []
    out = render.render_topology(tpl, range_id, lambda alias: asked.append(alias) or TEMPLATE_VM)
    assert out["role_errors"] == [] and asked[0] == "srv2022-exchange2019"
    template = {"name": "sim-roles", "vms": out["vm_definitions"], "networks": out["network_definitions"]}
    prov = mod.VsphereAPIProvisioner()
    built = _run(prov.provision(range_id, template, _reserve(prov, range_id, template)))
    assert built.status == "ok", built.errors
    (vm_out,) = built.vms
    assert vm_out["roles"] == {"exchange": {"status": "ok", "detail": "in role image"}}
    vm = vim.VirtualMachine(vm_out["vm_id"], si._stub)
    assert (vm.config.hardware.numCPU, vm.config.hardware.memoryMB) == (4, 16384)
    disk = next(d for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk))
    assert disk.capacityInKB >= 200 * 1024 * 1024
    assert _run(mod.VsphereAPIProvisioner().destroy(range_id, {"vms": built.vms, "networks": built.networks})
                ).status == "ok"


def test_a_noise_agent_gets_its_management_nic(env, si, range_id, monkeypatch):
    """The background-noise management NIC, on a port group made here for the test."""
    dvs = _named(si, vim.DistributedVirtualSwitch, DVS)
    noise_pg = f"tn-sim-noise-{range_id[:8]}"
    _wait(si, dvs.CreateDVPortgroup_Task(infra.dvs_portgroup_spec(noise_pg, 4001, False)))
    monkeypatch.setattr(mod, "VSPHERE_NOISE_NETWORK", noise_pg)
    try:
        prov = mod.VsphereAPIProvisioner()
        template = _template(range_id)
        agent = next(v for v in template["vms"] if v["node_id"] == "victim")
        agent["mgmt"] = {"vlan_id": 4001, "ip": "10.255.0.21", "prefix": 24}
        built = _run(prov.provision(range_id, template, _reserve(prov, range_id, template)))
        assert built.status == "ok", built.errors
        out = next(v for v in built.vms if v["name"] == agent["name"])
        assert out["mgmt_ip"] == "10.255.0.21"
        vm = vim.VirtualMachine(out["vm_id"], si._stub)
        noise_key = _named(si, vim.dvs.DistributedVirtualPortgroup, noise_pg).key
        keys = [n.backing.port.portgroupKey for n in infra.nic_cards(vm.config.hardware.device)]
        assert len(keys) == 2 and keys[1] == noise_key
        # The guest gets the management address on the noise NIC's MAC, the training one on the other.
        import base64

        import yaml

        extra = {o.key: o.value for o in vm.config.extraConfig}
        eth = yaml.safe_load(base64.b64decode(extra["guestinfo.metadata"]))["network"]["ethernets"]
        cards = infra.nic_cards(vm.config.hardware.device)
        by_mac = {e["match"]["macaddress"]: e.get("addresses") for e in eth.values()}
        assert by_mac[cards[1].macAddress.lower()] == ["10.255.0.21/24"]
        assert by_mac[cards[0].macAddress.lower()] == [f"{agent['ip']}/24"]
        other = next(v for v in built.vms if v["name"] != agent["name"])
        assert "mgmt_ip" not in other
        assert _run(mod.VsphereAPIProvisioner().destroy(range_id, {"vms": built.vms, "networks": built.networks})
                    ).status == "ok"
        assert _named(si, vim.dvs.DistributedVirtualPortgroup, noise_pg) is not None  # shared: never removed
    finally:
        pg = _named(si, vim.dvs.DistributedVirtualPortgroup, noise_pg)
        if pg is not None:
            _wait(si, pg.Destroy_Task())


def test_a_tenants_connection_replaces_the_environment(env, si, range_id, monkeypatch):
    """H6: the range's own tenant's HypervisorConnection (as worker.base_tasks passes it)
    is what the provisioner logs in to, for the build and the teardown. The environment
    here points at nothing, so a call that fell back to it would fail."""
    monkeypatch.setattr(mod, "VSPHERE_URL", "https://127.0.0.1:9")
    monkeypatch.setattr(mod, "VSPHERE_PASSWORD", "env-password-not-used")
    endpoint = urlparse(URL)
    creds = {"host": f"{endpoint.hostname}:{endpoint.port}", "username": USER, "password": PASSWORD,
             "verify_ssl": False, "datacenter": DC}
    prov = mod.VsphereAPIProvisioner(credentials=creds)
    template = _template(range_id)
    built = _run(prov.provision(range_id, template, _reserve(prov, range_id, template)))
    assert built.status == "ok", built.errors
    output = {"vms": built.vms, "networks": built.networks}
    assert _run(mod.VsphereAPIProvisioner(credentials=creds).health_check(range_id, output)).healthy
    gone = _run(mod.VsphereAPIProvisioner(credentials=creds).destroy(range_id, output))
    assert gone.status == "ok", gone.errors
    assert _portgroups(si, f"tn-{range_id[:8]}-") == {}


def test_a_vyos_router_gets_its_config_in_cloud_init_guestinfo(env, si, range_id):
    """vsphere_api's TODO(appliance): a VyOS router's per-range commands reach its VM's
    extraConfig before power-on, read back from the simulator."""
    import base64

    import yaml
    from worker import vyos_config

    topo = {**RANGE, "nodes": [*RANGE["nodes"], {
        "id": "rtr", "role": "router", "os": "vyos", "vlan": "red", "ip": "10.10.10.1",
        "specs": {"cores": 1, "memory_mb": 512, "disk_gb": 1}, "interfaces": [{"vlan": "red"}, {"vlan": "blue"}]}]}
    out = render.render_topology(topo, range_id, lambda alias: TEMPLATE_VM)
    template = {"name": "sim", "vms": out["vm_definitions"], "networks": out["network_definitions"]}
    prov = mod.VsphereAPIProvisioner()
    built = _run(prov.provision(range_id, template, _reserve(prov, range_id, template)))
    assert built.status == "ok", built.errors
    rtr = next(v for v in built.vms if v["node_id"] == "rtr")
    assert rtr["vyos"]["delivery"] == "cloud-init guestinfo"
    vm = vim.VirtualMachine(rtr["vm_id"], si._stub)
    extra = {o.key: o.value for o in vm.config.extraConfig}
    cmds = vyos_config.commands_of(extra)
    macs = [c.macAddress.lower() for c in infra.nic_cards(vm.config.hardware.device)]
    assert cmds[:2] == [f"set interfaces ethernet eth{i} hw-id '{m}'" for i, m in enumerate(macs)]
    assert any(c.startswith("set interfaces ethernet eth0 address '10.10.10.") for c in cmds)
    assert "set firewall ipv4 forward filter default-action 'drop'" in cmds
    meta = yaml.safe_load(base64.b64decode(extra["guestinfo.metadata"]))
    assert meta["instance-id"].startswith(rtr["name"])
    assert _run(mod.VsphereAPIProvisioner().destroy(range_id, {"vms": built.vms, "networks": built.networks})
                ).status == "ok"


def test_greyspace_gs_core_is_built_wired_and_torn_down(env, si, range_id, monkeypatch):
    """ADR 0007 on vSphere: gs-core (worker/greyspace_host.py) is cloned with the range,
    on the range's Greyspace port group, with its cloud-init and the stack bundle in
    guestinfo; the edge router's config routes the public space to it; the configure
    stage's guest channel fails closed on the simulator (below); destroy removes it with
    the range. Proven on vcsim, not on a real vCenter."""
    import base64
    import hashlib

    import yaml
    from worker import greyspace, greyspace_host, vyos_config
    from worker.provisioners.base import GuestStep

    monkeypatch.setenv("GREYSPACE_HOST_TEMPLATE", TEMPLATE_VM)
    monkeypatch.setenv("GREYSPACE_HOST_CORES", "1")
    monkeypatch.setenv("GREYSPACE_HOST_MEMORY_MB", "512")
    monkeypatch.setenv("GREYSPACE_HOST_DISK_GB", "1")
    topo = {
        **RANGE,
        "network": {"vlans": [*RANGE["network"]["vlans"], {"id": 30, "name": "greyspace", "cidr": "100.64.30.0/24"}]},
        "nodes": [*RANGE["nodes"], {
            "id": "rtr", "role": "router", "os": "vyos", "vlan": "red", "ip": "10.10.10.1",
            "specs": {"cores": 1, "memory_mb": 512, "disk_gb": 1},
            "interfaces": [{"vlan": "red"}, {"vlan": "blue"}, {"vlan": "greyspace"}]}],
    }
    out = render.render_topology(topo, range_id, lambda alias: TEMPLATE_VM)
    template = {"name": "sim", "vms": out["vm_definitions"], "networks": out["network_definitions"]}
    vm_def, info = greyspace_host.host_vm(range_id, dict(greyspace.DEFAULT_BLOCK), template)
    template["vms"].append(vm_def)
    assert greyspace_host.route_router(template, info) == f"{range_id[:8]}-rtr"

    prov = mod.VsphereAPIProvisioner()
    allocations = _reserve(prov, range_id, template)
    built = _run(prov.provision(range_id, template, allocations))
    assert built.status == "ok", built.errors
    gs = next(v for v in built.vms if v["node_id"] == "gs-core")
    assert gs["name"] == f"{range_id[:8]}-gs-core" and gs["nics"][0]["ip"] == "100.64.30.254"

    vm = vim.VirtualMachine(gs["vm_id"], si._stub)
    assert mod.annotated_range(vm.config.annotation) == range_id
    pg = _portgroups(si, f"tn-{range_id[:8]}-")[f"tn-{range_id[:8]}-v{allocations['physical_vlans']['30']}"]
    assert [n.backing.port.portgroupKey for n in infra.nic_cards(vm.config.hardware.device)] == [pg.key]
    extra = {o.key: o.value for o in vm.config.extraConfig}
    userdata = yaml.safe_load(base64.b64decode(extra["guestinfo.userdata"]))
    assert [u["name"] for u in userdata["users"][1:]] == ["tn-greyspace"]
    assert userdata["runcmd"] == [["/opt/greyspace/bootstrap.sh", "unpack"]]
    count = int(extra["guestinfo.tn.greyspace.bundle.count"])
    bundle = base64.b64decode("".join(extra[f"guestinfo.tn.greyspace.bundle.{n}"] for n in range(count)))
    assert hashlib.sha256(bundle).hexdigest() == extra["guestinfo.tn.greyspace.bundle.sha256"]

    rtr = next(v for v in built.vms if v["node_id"] == "rtr")
    rtr_extra = {o.key: o.value for o in vim.VirtualMachine(rtr["vm_id"], si._stub).config.extraConfig}
    assert "set protocols static route '198.18.0.0/15' next-hop '100.64.30.254'" in vyos_config.commands_of(rtr_extra)

    # The configure stage's guest channel. vcsim v0.56.0 runs no VMware Tools, and its
    # guestOperationsManager.authManager reply carries no xsi:type, which pyVmomi cannot
    # read (KeyError 'type'): guest operations cannot be exercised on the simulator. What
    # is proven here is that the channel fails closed (an exception, which the configure
    # stage records as failed), never a false "ok". GuestSession itself is tested with a
    # fake vCenter in tests/worker/test_vsphere_guest.py.
    prov._guest_poll = 0.2
    login = greyspace_host.login(range_id)
    with pytest.raises((RuntimeError, KeyError)):
        _run(prov.run_in_guest(gs["vm_id"], login, [GuestStep("health", "/bin/true")], timeout=2))

    gone = _run(mod.VsphereAPIProvisioner().destroy(range_id, {"vms": built.vms, "networks": built.networks}))
    assert gone.status == "ok", gone.errors
    assert _named(si, vim.VirtualMachine, gs["name"]) is None
    assert _portgroups(si, f"tn-{range_id[:8]}-") == {}
