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
* VMware Tools never runs in a vcsim VM: ``tools_ready`` is False, no guest IP is seen,
  and so readiness (vsphere_readiness.py) is switched off in ``env``
  (VSPHERE_GUEST_IP_CHECK=off: the VM's ``ip`` is then '' and ``ip_verified`` False, never
  the rendered one); one test turns it on to see a build refused. Guest operations
  (deploy-time installs) and guest customization results are not exercised.
* Four SOAP differences from vCenter that break any pyVmomi client are patched for this
  module only (``_vcsim_wire_gaps``); none is the provisioner's doing.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import uuid
from urllib.parse import urlparse

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.vcsim]

pytest.importorskip("pyVmomi")
httpx = pytest.importorskip("httpx")

from pyVim.connect import Disconnect, SmartConnect  # noqa: E402
from pyVmomi import vim, vmodl  # noqa: E402
from worker import range_inventory, render  # noqa: E402
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
        "VSPHERE_TOOLS_TIMEOUT": 0, "VSPHERE_RESOURCE_POOL": "", "VSPHERE_RANGE_FOLDER": "TrueNorth/Ranges",
        "VSPHERE_TAGS": True,
        # Tools never runs in a vcsim VM: record what it reports, do not fail on it
        # (test_readiness_refuses_a_range_whose_guests_report_nothing turns it on).
        "VSPHERE_GUEST_IP_TIMEOUT": 0, "VSPHERE_GUEST_IP_CHECK": False, "VSPHERE_MAC_POOL": "",
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


UNPROMOTED = {"promoted": False, "catalogue_id": "ubuntu-2404", "allowed_by": "TN_TEMPLATE_ALLOW_UNPROMOTED"}


@pytest.fixture(autouse=True)
def _pin_step(monkeypatch):
    """The worker's pin step (worker/template_pins.py) for builds a test does not pin itself:
    each VM let through unpromoted to its plain template, as the transition flag does."""
    real = mod.VsphereAPIProvisioner.provision

    async def provision(self, range_id, template, allocations):
        vms = [v if "template_build" in v else {**v, "template_build": UNPROMOTED} for v in template.get("vms", [])]
        return await real(self, range_id, {**template, "vms": vms}, allocations)

    monkeypatch.setattr(mod.VsphereAPIProvisioner, "provision", provision)


def _template(range_id: str, build: dict | None = None) -> dict:
    """The rendered range, each VM pinned as worker/template_pins.py would (ADR 0013 §2):
    to ``build``, or, by default, let through unpromoted to the plain template."""
    out = render.render_topology(RANGE, range_id, lambda alias: TEMPLATE_VM)
    vms = [{**v, "template_build": build or UNPROMOTED} for v in out["vm_definitions"]]
    return {"name": "sim", "vms": vms, "networks": out["network_definitions"]}


def _reserve(prov, range_id: str, template: dict, taken=()) -> dict:
    """The worker's part (worker/range_alloc.py), without a database: the lowest free values."""
    allocations = {}
    for need in prov.allocation_needs(range_id, template):
        free = list(itertools.islice((v for v in need.pool if v not in {str(t) for t in taken}), len(need.holders)))
        got = dict(zip(need.holders, free, strict=False))
        allocations[need.key] = got[need.holders[0]] if need.single else got
    return allocations


def _cis(fn):
    """``fn(client, headers)`` on a fresh legacy REST session (the simulator's tagging API)."""
    with httpx.Client(base_url=URL, verify=False) as c:
        h = {"vmware-api-session-id": c.post("/rest/com/vmware/cis/session", auth=(USER, PASSWORD)).json()["value"]}
        try:
            return fn(c, h)
        finally:
            c.delete("/rest/com/vmware/cis/session", headers=h)


_TAGGING = "/rest/com/vmware/cis/tagging"


def _tags_on(vm_id: str) -> dict[str, str]:
    """category -> tag name on a VM, read back from the simulator."""
    def read(c, h):
        ids = c.post(f"{_TAGGING}/tag-association?~action=list-attached-tags", headers=h,
                     json={"object_id": {"type": "VirtualMachine", "id": vm_id}}).json()["value"]
        out = {}
        for tid in ids:
            tag = c.get(f"{_TAGGING}/tag/id:{tid}", headers=h).json()["value"]
            out[c.get(f"{_TAGGING}/category/id:{tag['category_id']}", headers=h).json()["value"]["name"]] = tag["name"]
        return out
    return _cis(read)


def _tag_names(category: str) -> set[str]:
    def read(c, h):
        names = set()
        for cid in c.get(f"{_TAGGING}/category", headers=h).json()["value"]:
            if c.get(f"{_TAGGING}/category/id:{cid}", headers=h).json()["value"]["name"] == category:
                listed = c.post(f"{_TAGGING}/tag/id:{cid}?~action=list-tags-for-category", headers=h).json()
                for tid in listed["value"] or []:  # vcsim answers null for none
                    names.add(c.get(f"{_TAGGING}/tag/id:{tid}", headers=h).json()["value"]["name"])
        return names
    return _cis(read)


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
    r8 = range_id[:8]
    key = range_id.replace("-", "")[:12]  # vsphere_tags.range_key: the folder and tn-range tag
    assert allocations == {"physical_vlans": {"10": "100", "20": "101"},
                           "nic_macs": {f"{r8}-attacker#nic0": "00:50:56:3c:00:00",
                                        f"{r8}-victim#nic0": "00:50:56:3c:00:01"}}

    built = _run(prov.provision(range_id, template, {**allocations, "range_name": "Sim Range"}))
    assert built.status == "ok", built.errors
    assert prov._dialect == "rest"

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
        assert vm.parent.name == f"sim-range-{key}" and vm.parent.parent.name == "Ranges"
        assert _tags_on(out["vm_id"]) == {"tn-type": "range-vm", "tn-os": "ubuntu-2404", "tn-template": TEMPLATE_VM,
                                          "tn-range": f"sim-range-{key}"}
    assert {v["vm_id"] for v in _run(prov.find_vms(f"{range_id}-"))} == {v["vm_id"] for v in built.vms}

    # What provision_range stores (range_alloc.stored_output): with the vCenter it was built
    # on, which record_build wrote before the first VM existed (worker/connection_binding.py).
    output = {"vms": built.vms, "networks": built.networks, "connection": prov.connection_record()}
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
    assert f"sim-range-{key}" not in _tag_names("tn-range")  # the range's tag went with its VMs
    assert not [f for f in _named(si, vim.Folder, "Ranges").childEntity if f.name.endswith(key)]
    assert _run(mod.VsphereAPIProvisioner().find_vms(f"{range_id}-")) == []
    again = _run(mod.VsphereAPIProvisioner().destroy(range_id, output))
    assert again.status == "ok", again.errors  # a retried destroy finds everything gone
    # Without the record (a range from before records, never confirmed), "every VM gone"
    # may just as well be the wrong vCenter: refused, not recorded destroyed.
    unrecorded = {k: v for k, v in output.items() if k != "connection"}
    refused = _run(mod.VsphereAPIProvisioner().destroy(range_id, unrecorded))
    assert refused.status == "failed" and "does not record which vCenter" in refused.errors[0]
    # S5: "none of its ids is here" is no proof of a clean teardown on what may be another vCenter.
    assert range_inventory.teardown_status(refused.teardown)[0] == "incomplete"


def test_reserved_manual_macs_land_and_spread_uses_every_host(env, si, range_id):
    """2026-10-20: two clones on one host were given one generated MAC, and spread put a
    3-VM range on one host. Here vcsim, a real implementation of the APIs, keeps the
    manual MACs the clone spec sets, and the range is dealt across its three hosts."""
    tpl = {**RANGE, "nodes": [{**RANGE["nodes"][0], "id": f"ubu0{i}"} for i in (1, 2, 3)]}
    prov = mod.VsphereAPIProvisioner()
    rendered = render.render_topology(tpl, range_id, lambda alias: TEMPLATE_VM)
    template = {"name": "sim", "vms": rendered["vm_definitions"], "networks": rendered["network_definitions"]}
    allocations = _reserve(prov, range_id, template)
    built = _run(prov.provision(range_id, template, allocations))
    try:
        assert built.status == "ok", built.errors
        assert len({vm["host"] for vm in built.vms}) == 3  # vcsim's DC0_C0 has three hosts
        reserved = allocations["nic_macs"]
        for out in built.vms:
            (card,) = infra.nic_cards(vim.VirtualMachine(out["vm_id"], si._stub).config.hardware.device)
            assert card.addressType == "manual"
            assert card.macAddress == reserved[f"{out['name']}#nic0"] == out["nics"][0]["mac"]
        assert len({o["nics"][0]["mac"] for o in built.vms}) == 3
    finally:
        output = {"vms": built.vms, "networks": built.networks, "connection": prov.connection_record()}
        assert _run(mod.VsphereAPIProvisioner().destroy(range_id, output)).status == "ok"


def test_a_vm_anywhere_holding_a_reserved_mac_refuses_the_build(env, si, range_id):
    """The MAC pre-check reads every VM in the vCenter (one property-collector query), not
    only the range's networks: here vcsim's own DC0_C0_RP0_VM1, given a MAC from the slice."""
    squatter = _named(si, vim.VirtualMachine, "DC0_C0_RP0_VM1")
    (card,) = infra.nic_cards(squatter.config.hardware.device)[:1]
    old_type, old_mac = card.addressType, card.macAddress
    prov = mod.VsphereAPIProvisioner()
    template = _template(range_id)
    allocations = _reserve(prov, range_id, template)
    taken = allocations["nic_macs"][f"{range_id[:8]}-victim#nic0"]

    def set_mac(address_type, mac):
        card.addressType, card.macAddress = address_type, mac
        edit = vim.vm.device.VirtualDeviceSpec(operation="edit", device=card)
        _wait(si, squatter.ReconfigVM_Task(spec=vim.vm.ConfigSpec(deviceChange=[edit])))

    set_mac("manual", taken)
    try:
        built = _run(prov.provision(range_id, template, allocations))
        assert built.status == "failed" and built.vms == []
        assert any(f"MAC {taken}" in e and "DC0_C0_RP0_VM1" in e for e in built.errors), built.errors
        assert _portgroups(si, f"tn-{range_id[:8]}-") == {}
    finally:
        set_mac(old_type, old_mac)


def test_readiness_refuses_a_range_whose_guests_report_nothing(env, si, range_id, monkeypatch):
    """Tools never runs in vcsim, so with the check on no VM can show its planned address:
    the build says so per VM instead of the range being reported ready."""
    from worker import range_alloc

    monkeypatch.setattr(mod, "VSPHERE_GUEST_IP_CHECK", True)
    prov = mod.VsphereAPIProvisioner()
    template = _template(range_id)
    built = _run(prov.provision(range_id, template, _reserve(prov, range_id, template)))
    try:
        assert sorted(built.not_ready) == sorted(v["name"] for v in built.vms) and len(built.vms) == 2
        assert all(v["ip"] == "" and v["ip_verified"] is False and v["tools_ready"] is False for v in built.vms)
        assert all("VMware Tools did not report running" in e for e in built.errors), built.errors
        state, error = range_alloc.build_outcome(built)
        assert state == "failed" and "2 VM(s) built but not ready" in error
    finally:
        output = {"vms": built.vms, "networks": built.networks, "connection": prov.connection_record()}
        assert _run(mod.VsphereAPIProvisioner().destroy(range_id, output)).status == "ok"


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
    # The range has no domain controller, so the deploy-time Exchange setup is skipped (not
    # failed, the range stays ok); this test is about the clone's sizing and disk growth.
    assert vm_out["roles"]["exchange"]["status"] == "skipped"
    assert "no forest-root domain controller" in vm_out["roles"]["exchange"]["detail"]
    assert vm_out["role_setup"]["status"] == "skipped"
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
        _run(prov.run_in_guest(gs["vm_id"], login, [GuestStep("health", "/bin/true")], timeout=2,
                               range_id=range_id, vm_name=gs["name"]))

    gone = _run(mod.VsphereAPIProvisioner().destroy(range_id, {"vms": built.vms, "networks": built.networks}))
    assert gone.status == "ok", gone.errors
    assert _named(si, vim.VirtualMachine, gs["name"]) is None
    assert _portgroups(si, f"tn-{range_id[:8]}-") == {}


def _copies(si, template, dates_builds: dict[str, tuple[str, str]], hosts=None) -> list:
    """``<template>@<host>-<date>`` on each host (default: every host of the cluster), stamped
    in its annotation as the build ``dates_builds[date]``, as build.sh replicate leaves them."""
    from worker.provisioners import template_copies

    cluster = _named(si, vim.ClusterComputeResource, CLUSTER)
    made = []
    for host in hosts or cluster.host:
        short = template_copies.host_short_name(host.name)
        for date, (build_id, checksum) in dates_builds.items():
            spec = vim.vm.CloneSpec(powerOn=False, location=vim.vm.RelocateSpec(
                host=host, pool=cluster.resourcePool, datastore=host.datastore[0]))
            task = template.CloneVM_Task(folder=template.parent, name=f"{TEMPLATE_VM}@{short}-{date}", spec=spec)
            _wait(si, task)
            copy = task.info.result
            _wait(si, copy.ReconfigVM_Task(spec=vim.vm.ConfigSpec(
                annotation=f"TrueNorth golden\n{template_copies.stamp('ubuntu-2404', build_id, checksum)}")))
            copy.MarkAsTemplate()
            made.append(copy)
    return made


def _pinned_build(si, date: str, build_id: str = "b1", checksum: str = "a" * 64) -> dict:
    """A promoted build naming ``<template>@<host>-<date>`` on every host (worker/template_pins.py)."""
    from worker.provisioners import template_copies

    shorts = [template_copies.host_short_name(h.name) for h in _named(si, vim.ClusterComputeResource, CLUSTER).host]
    return {"promoted": True, "catalogue_id": "ubuntu-2404", "build_id": build_id, "checksum": checksum,
            "build_date": "2026-10-01", "iso_checksum": "", "template_name": TEMPLATE_VM,
            "copies": {s: f"{TEMPLATE_VM}@{s}-{date}" for s in shorts}}


def test_the_promoted_builds_copies_are_cloned_and_pruning_keeps_them(env, si, range_id, template):
    """ADR 0013 §2 on real managed objects: every host has an older copy of the promoted
    build b1 and a newer copy of b2. Each VM clones b1's copy on its own host (no newest-copy
    selection; ``VirtualMachine.datastore`` and the annotation as vCenter serves them), and
    pruning by the build records removes only b2's copies. vcsim's hosts share LocalDS_0, so
    residence holds on every host here; the per-host datastore check is covered by the fakes."""
    from worker.provisioners import template_copies

    b1, b2 = ("b1", "a" * 64), ("b2", "b" * 64)
    made = _copies(si, template, {"20261001": b1, "20261019": b2})
    cluster = _named(si, vim.ClusterComputeResource, CLUSTER)
    try:
        prov = mod.VsphereAPIProvisioner()
        tpl = _template(range_id, _pinned_build(si, "20261001"))
        built = _run(prov.provision(range_id, tpl, _reserve(prov, range_id, tpl)))
        assert built.status == "ok", built.errors
        for out in built.vms:
            short = template_copies.host_short_name(out["host"])
            assert out["template"] == f"{TEMPLATE_VM}@{short}-20261001"
            assert (out["template_build"]["build_id"], out["template_build"]["checksum"]) == b1
            assert vim.VirtualMachine(out["vm_id"], si._stub).runtime.host.name == out["host"]

        promoted = {f"{TEMPLATE_VM}@{template_copies.host_short_name(h.name)}-20261001" for h in cluster.host}
        policy = template_copies.PrunePolicy(
            protected=frozenset(promoted | template_copies.names_in_use([{"vms": built.vms}])),
            managed=frozenset({TEMPLATE_VM}), builds=frozenset({("ubuntu-2404", *b1)}))
        report = _run(mod.VsphereAPIProvisioner().prune_template_copies(policy))
        assert report["dry_run"] and report["deleted"] == []
        assert len(report["deletable"]) == len(cluster.host) and all(n.endswith("-20261019")
                                                                      for n in report["deletable"])
        done = _run(mod.VsphereAPIProvisioner().prune_template_copies(policy, dry_run=False))
        assert sorted(done["deleted"]) == sorted(report["deletable"]) and done["errors"] == []
        left = {_name(vm) for vm in _all(si, vim.VirtualMachine)}
        assert not left & set(done["deleted"]) and TEMPLATE_VM in left
        assert promoted <= left
        _run(mod.VsphereAPIProvisioner().destroy(range_id, {"vms": built.vms, "networks": built.networks}))
    finally:
        for vm in made:
            with contextlib.suppress(Exception):
                _wait(si, vm.Destroy_Task())


# --------------------------------------------------------------------------- #
# Range certification S1: allocation against the live switch, through the real
# allocator (worker/range_alloc.py) on a throwaway SQLite database
# --------------------------------------------------------------------------- #


class _AllocDB:
    """The API's tables on in-memory SQLite, with one range in ``provisioning``."""

    def __init__(self, range_id: str):
        import app.network_inventory  # noqa: F401 — registers network_reservations
        import app.range_leases  # noqa: F401
        import sqlalchemy as sa
        from app.db import Base
        from app.models import Range, Template, Tenant
        from sqlalchemy.orm import Session, sessionmaker

        self.sa = sa
        self.engine = sa.create_engine("sqlite://", poolclass=sa.StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, class_=Session)
        tid = uuid.uuid4()
        with self.session() as s:
            s.add(Tenant(id=tid, name="sim", slug=f"sim-{tid.hex[:6]}"))
            s.flush()
            tmpl = Template(id=uuid.uuid4(), name="sim", yaml="id: sim\n", tenant_id=tid)
            s.add(tmpl)
            s.flush()
            s.add(Range(id=uuid.UUID(range_id), name="sim", template_id=tmpl.id, tenant_id=tid, state="provisioning",
                        provisioner_backend="vsphere_api"))

    @contextlib.contextmanager
    def session(self):
        s = self.factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def rows(self, kind: str = "vlan") -> list[tuple]:
        """(value, holder, external?) of every reservation of ``kind``."""
        from worker.tables import network_reservations as nr

        with self.session() as s:
            return sorted((r.value, r.holder, r.range_id is None) for r in s.execute(
                self.sa.select(nr.c.value, nr.c.holder, nr.c.range_id).where(nr.c.kind == kind)).all())


def _plant(si, name: str, vlan_spec):
    """A port group that is not TrueNorth's, as a network admin would make it."""
    dvs = _named(si, vim.DistributedVirtualSwitch, DVS)
    port = vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy(vlan=vlan_spec)
    spec = vim.dvs.DistributedVirtualPortgroup.ConfigSpec(name=name, type="earlyBinding", numPorts=8,
                                                          defaultPortConfig=port)
    _wait(si, dvs.CreateDVPortgroup_Task(spec))
    return name


def _drop(si, *names):
    for name in names:
        pg = _named(si, vim.dvs.DistributedVirtualPortgroup, name)
        if pg is not None:
            with contextlib.suppress(Exception):
                _wait(si, pg.Destroy_Task())


def test_a_non_truenorth_port_group_on_a_pool_vlan_is_never_allocated(env, si, range_id):
    """S1: VLAN 100 is carried by a plain port group and 101-102 by a trunk, neither
    TrueNorth's; DVS0's own uplink port group (trunk 0-4094) is not counted. The
    allocator records them as external and hands the range 103 and 104, which it builds."""
    from worker import range_alloc

    vlan_spec = vim.dvs.VmwareDistributedVirtualSwitch
    corp = _plant(si, f"corp-{range_id[:8]}", vlan_spec.VlanIdSpec(vlanId=100))
    trunk = _plant(si, f"trunk-{range_id[:8]}", vlan_spec.TrunkVlanSpec(vlanId=[vim.NumericRange(start=101, end=102)]))
    db = _AllocDB(range_id)
    try:
        prov = mod.VsphereAPIProvisioner()
        template = _template(range_id)
        allocations = range_alloc.reserve_for_build(db.session, range_id, prov, template)
        assert allocations["physical_vlans"] == {"10": "103", "20": "104"}
        source = f"{urlparse(URL).hostname}/{DVS}"
        external = {v: h for v, h, no_range in db.rows() if no_range}
        assert external == {"100": f"external:{source}:{corp}", "101": f"external:{source}:{trunk}",
                            "102": f"external:{source}:{trunk}"}
        built = _run(prov.provision(range_id, template, allocations))
        assert built.status == "ok", built.errors
        pgs = _portgroups(si, f"tn-{range_id[:8]}-")
        assert sorted(pgs) == [f"tn-{range_id[:8]}-v103", f"tn-{range_id[:8]}-v104"]
        assert {pg.config.defaultPortConfig.vlan.vlanId for pg in pgs.values()} == {103, 104}
        assert {pg.config.description for pg in pgs.values()} == {f"TrueNorth range {range_id}"}
        output = {"vms": built.vms, "networks": built.networks, "connection": prov.connection_record()}
        assert _run(mod.VsphereAPIProvisioner().destroy(range_id, output)).status == "ok"
        assert _named(si, vim.dvs.DistributedVirtualPortgroup, corp) is not None  # never touched
    finally:
        _drop(si, corp, trunk)


def test_a_vlan_appearing_between_allocation_and_creation_stops_the_build(env, si, range_id):
    """S1: the range is allocated 100 and 101; then a port group appears on 101. The build
    creates nothing (v100 is made, then undone), clones no VM, and the worker frees both
    VLANs, records 101 as external and fails the range finally."""
    from worker import range_alloc

    db = _AllocDB(range_id)
    prov = mod.VsphereAPIProvisioner()
    template = _template(range_id)
    allocations = range_alloc.reserve_for_build(db.session, range_id, prov, template)
    assert allocations["physical_vlans"] == {"10": "100", "20": "101"}
    late = _plant(si, f"late-{range_id[:8]}", vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec(vlanId=101))
    try:
        result = _run(prov.provision(range_id, template, allocations))
        assert result.status == "failed" and result.vms == []
        assert f"VLAN 101 is already used by port group '{late}' on {DVS}" in result.errors[0], result.errors
        assert _portgroups(si, f"tn-{range_id[:8]}-") == {}
        assert _run(prov.find_vms(f"{range_id}-")) == []
        assert result.conflict["release"] == ["100", "101"]
        err = range_alloc.build_failure(db.session, range_id, result)
        assert isinstance(err, range_alloc.NetworkConflictError)
        source = f"{urlparse(URL).hostname}/{DVS}"
        assert db.rows() == [("101", f"external:{source}:{late}", True)]  # the range holds no VLAN now
        assert len(db.rows("mac")) == 2  # its NIC MACs stay until its destroy, as for any failed build
    finally:
        _drop(si, late)


def test_a_host_missing_the_promoted_copy_is_avoided_and_none_anywhere_fails(env, si, range_id, template):
    """Only the first host holds b1's copy: every VM goes there. With no copy on any host the
    build fails and leaves nothing behind (no plain-name fallback)."""
    first = _named(si, vim.ClusterComputeResource, CLUSTER).host[0]
    made = _copies(si, template, {"20261001": ("b1", "a" * 64)}, hosts=[first])
    try:
        prov = mod.VsphereAPIProvisioner()
        tpl = _template(range_id, _pinned_build(si, "20261001"))
        built = _run(prov.provision(range_id, tpl, _reserve(prov, range_id, tpl)))
        assert built.status == "ok", built.errors
        assert {out["host"] for out in built.vms} == {first.name}
        _run(mod.VsphereAPIProvisioner().destroy(range_id, {"vms": built.vms, "networks": built.networks}))
    finally:
        for vm in made:
            with contextlib.suppress(Exception):
                _wait(si, vm.Destroy_Task())
    prov = mod.VsphereAPIProvisioner()
    tpl = _template(range_id, _pinned_build(si, "20261001"))
    failed = _run(prov.provision(range_id, tpl, _reserve(prov, range_id, tpl)))
    assert failed.status == "failed" and not failed.vms
    assert any("holds its promoted template build" in e for e in failed.errors), failed.errors
    assert _portgroups(si, f"tn-{range_id[:8]}-") == {}


# --------------------------------------------------------------------------- #
# Range certification S5 (#243): teardown by identified inventory, and drift
# --------------------------------------------------------------------------- #


def _built(range_id: str) -> tuple[dict, object]:
    prov = mod.VsphereAPIProvisioner()
    template = _template(range_id)
    built = _run(prov.provision(range_id, template, {**_reserve(prov, range_id, template), "range_name": "Sim Range"}))
    assert built.status == "ok", built.errors
    output = {"vms": built.vms, "networks": built.networks, "connection": prov.connection_record(),
              "inventory": built.inventory}
    return output, built


def _stray(si, name: str, annotation: str = "") -> object:
    """A VM nobody recorded, cloned from one vcsim ships (not on the range's port groups)."""
    source = _named(si, vim.VirtualMachine, "DC0_H0_VM1")
    task = source.CloneVM_Task(folder=source.parent, name=name,
                               spec=vim.vm.CloneSpec(location=vim.vm.RelocateSpec(), powerOn=False))
    _wait(si, task)
    vm = task.info.result
    if annotation:
        _wait(si, vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(annotation=annotation)))
    return vm


def _tag(vm_id: str, tag_name: str) -> None:
    """Attach the existing tn-range tag ``tag_name`` to a VM, as a person in vCenter would."""
    def attach(c, h):
        for cid in c.get(f"{_TAGGING}/category", headers=h).json()["value"]:
            if c.get(f"{_TAGGING}/category/id:{cid}", headers=h).json()["value"]["name"] != "tn-range":
                continue
            for tid in c.post(f"{_TAGGING}/tag/id:{cid}?~action=list-tags-for-category", headers=h).json()["value"]:
                if c.get(f"{_TAGGING}/tag/id:{tid}", headers=h).json()["value"]["name"] == tag_name:
                    c.post(f"{_TAGGING}/tag-association/id:{tid}?~action=attach", headers=h,
                           json={"object_id": {"type": "VirtualMachine", "id": vm_id}}).raise_for_status()
                    return
        raise AssertionError(f"no tn-range tag {tag_name}")
    _cis(attach)


def test_s5_the_inventory_is_recorded_at_clone(env, si, range_id):
    output, built = _built(range_id)
    try:
        for out in built.vms:
            vm = vim.VirtualMachine(out["vm_id"], si._stub)
            ident = out["identity"]
            assert ident["moid"] == out["vm_id"] and ident["instance_uuid"] == vm.config.instanceUuid
            assert ident["folder"] == vm.parent._moId == built.inventory["folder"]["moid"]
            disks = [d.backing.fileName for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)]
            assert [d["path"] for d in ident["disks"]] == disks and all(d["datastore"] for d in ident["disks"])
            assert ident["datastores"] == sorted(ds._moId for ds in vm.datastore)
            assert (ident["num_cpu"], ident["memory_mb"]) == (vm.config.hardware.numCPU, vm.config.hardware.memoryMB)
            assert [n["mac"] for n in ident["nics"]] == [n["mac"] for n in out["nics"]]
        pgs = _portgroups(si, f"tn-{range_id[:8]}-")
        assert {n["portgroup"]: (n["moid"], n["key"]) for n in built.networks if n.get("portgroup")} == {
            name: (pg._moId, pg.key) for name, pg in pgs.items()}
        assert built.inventory["vlans"] == {"10": 100, "20": 101} and len(built.inventory["macs"]) == 2
        assert range_inventory.recorded(output)["complete"]
    finally:
        assert _run(mod.VsphereAPIProvisioner().destroy(range_id, output)).status == "ok"


def test_s5_a_clean_teardown_records_zero_remaining_ids(env, si, range_id):
    output, built = _built(range_id)
    gone = _run(mod.VsphereAPIProvisioner().destroy(range_id, output))
    assert gone.status == "ok", gone.errors
    report = gone.teardown
    assert report["remaining"] == [] and report["unidentified"] == [] and report["not_checked"] == []
    assert {d["id"] for d in report["deleted"]} == {v["vm_id"] for v in built.vms}
    assert range_inventory.teardown_status(report)[0] == "pass"


def test_s5_a_stray_vm_annotated_or_tagged_for_the_range_is_reported_not_deleted(env, si, range_id):
    output, _ = _built(range_id)
    key = range_id.replace("-", "")[:12]
    annotated = _stray(si, f"stray-ann-{range_id[:8]}", mod.range_annotation(range_id))
    tagged = _stray(si, f"stray-tag-{range_id[:8]}")
    _tag(tagged._moId, f"sim-range-{key}")
    try:
        gone = _run(mod.VsphereAPIProvisioner().destroy(range_id, output))
        assert gone.status == "ok", gone.errors  # the range's own items went
        listed = {u["moid"]: u for u in gone.teardown["unidentified"]}
        assert set(listed) == {annotated._moId, tagged._moId}
        assert listed[annotated._moId]["marks"] == ["annotation"] and listed[tagged._moId]["marks"] == ["tag"]
        assert listed[annotated._moId]["instance_uuid"] == annotated.config.instanceUuid
        # Neither was deleted, and the tag that marks one of them stays as evidence.
        assert annotated.name and tagged.name
        assert f"sim-range-{key}" in _tag_names("tn-range")
        status, summary = range_inventory.teardown_status(gone.teardown)
        assert status == "incomplete" and "2 unidentified" in summary

        # An admin confirms the annotated one by moid; a listing that no longer matches
        # (its instance UUID changed) is left alone.
        wrong = {**listed[tagged._moId], "instance_uuid": "00000000-0000-0000-0000-000000000000"}
        after = _run(mod.VsphereAPIProvisioner().delete_confirmed(range_id, output, [listed[annotated._moId], wrong]))
        assert [d["moid"] for d in after["confirmed_deleted"]] == [annotated._moId]
        assert [s["moid"] for s in after["confirm_skipped"]] == [tagged._moId]
        with pytest.raises(vmodl.fault.ManagedObjectNotFound):
            annotated.name  # noqa: B018 -- deleted
        assert tagged.name and [u["moid"] for u in after["unidentified"]] == [tagged._moId]
    finally:
        with contextlib.suppress(Exception):
            _wait(si, tagged.Destroy_Task())


def test_s5_a_port_group_with_the_ranges_owner_mark_that_was_not_recorded_is_reported_not_deleted(env, si, range_id):
    output, _ = _built(range_id)
    dvs = _named(si, vim.DistributedVirtualSwitch, DVS)
    name = f"tn-{range_id[:8]}-v199"  # what a build that died before recording it would leave
    stray = infra.ensure_dvs_portgroup(dvs, name, 199, False, lambda t: _wait(si, t),
                                       mod.live_vlans.owner_mark(range_id))
    try:
        gone = _run(mod.VsphereAPIProvisioner().destroy(range_id, output))
        assert gone.status == "ok", gone.errors
        [listed] = gone.teardown["unidentified"]
        assert (listed["kind"], listed["moid"], listed["name"], listed["vlan"]) == ("portgroup", stray._moId, name, 199)
        assert stray.name == name  # left alone
        after = _run(mod.VsphereAPIProvisioner().delete_confirmed(range_id, output, [listed]))
        assert [d["moid"] for d in after["confirmed_deleted"]] == [stray._moId] and after["unidentified"] == []
        assert range_inventory.teardown_status(after)[0] == "pass"
    finally:
        _drop(si, name)


def test_s5_a_renamed_and_re_annotated_vm_is_still_deleted_by_its_moid(env, si, range_id):
    output, built = _built(range_id)
    vm = vim.VirtualMachine(built.vms[0]["vm_id"], si._stub)
    _wait(si, vm.Rename_Task(newName="someone-renamed-me"))
    _wait(si, vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(annotation="not a range VM any more")))
    gone = _run(mod.VsphereAPIProvisioner().destroy(range_id, output))
    assert gone.status == "ok", gone.errors
    with pytest.raises(vmodl.fault.ManagedObjectNotFound):
        vm.name  # noqa: B018 -- deleted by its recorded moid + instance UUID
    assert gone.teardown["remaining"] == [] and range_inventory.teardown_status(gone.teardown)[0] == "pass"


def test_s5_a_disk_left_behind_on_its_datastore_is_detected(env, si, range_id, monkeypatch):
    """vCenter leaving a disk file behind (one held by another VM, or on a datastore the
    delete could not reach) is simulated by putting the file back right after the delete."""
    output, built = _built(range_id)
    disk = built.vms[0]["identity"]["disks"][0]
    dc = _named(si, vim.Datacenter, DC)
    real = mod.VsphereAPIProvisioner._delete_vm

    async def delete_then_leave_a_disk(self, client, vm_id):
        await real(self, client, vm_id)
        if vm_id == built.vms[0]["vm_id"]:
            si.content.fileManager.MakeDirectory(name=disk["path"].rsplit("/", 1)[0], datacenter=dc,
                                                 createParentDirectories=True)
            spec = vim.VirtualDiskManager.FileBackedVirtualDiskSpec(diskType="thin", adapterType="lsiLogic",
                                                                     capacityKb=1024)
            _wait(si, si.content.virtualDiskManager.CreateVirtualDisk_Task(name=disk["path"], datacenter=dc,
                                                                           spec=spec))

    monkeypatch.setattr(mod.VsphereAPIProvisioner, "_delete_vm", delete_then_leave_a_disk)
    gone = _run(mod.VsphereAPIProvisioner().destroy(range_id, output))
    try:
        assert gone.status == "ok", gone.errors  # every VM was deleted ...
        assert [r for r in gone.teardown["remaining"] if r["kind"] == "disk"] == [
            {"kind": "disk", "id": disk["path"], "path": disk["path"], "datastore": disk["datastore"],
             "name": built.vms[0]["name"]}]
        status, summary = range_inventory.teardown_status(gone.teardown)  # ... but the record says what is left
        assert status == "fail" and disk["path"] in summary
    finally:
        with contextlib.suppress(Exception):
            _wait(si, si.content.fileManager.DeleteDatastoreFile_Task(name=disk["path"].rsplit("/", 1)[0],
                                                                      datacenter=dc))


def test_s5_drift_ram_vlan_and_snapshots(env, si, range_id):
    output, built = _built(range_id)
    try:
        def drift(rows=()):
            live = _run(mod.VsphereAPIProvisioner().live_inventory(range_id, output))
            return range_inventory.compare(output, live, rows)

        clean = drift()
        assert (clean["status"], clean["findings"], clean["checked"]) == ("pass", [], {"vms": 2, "portgroups": 2})
        # A snapshot TrueNorth took (its ids recorded as taken) is no drift; one taken by hand is.
        snap = _run(mod.VsphereAPIProvisioner().snapshot(range_id, output, "tnbaseline"))
        assert snap.status == "ok" and set(snap.snapshot_ids) == {v["vm_id"] for v in built.vms}
        rows = [{"state": "ready", "backend_name": "tnbaseline",
                 "data": {"snapshot_name": "tnbaseline", "snapshot_ids": snap.snapshot_ids}}]
        assert drift(rows)["findings"] == []
        vm = vim.VirtualMachine(built.vms[0]["vm_id"], si._stub)
        _wait(si, vm.CreateSnapshot_Task(name="by-hand", memory=False, quiesce=False))
        # RAM changed by hand: drift, not a failure; a port group moved to another VLAN: a failure.
        _wait(si, vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(memoryMB=built.vms[0]["identity"]["memory_mb"] * 2)))
        result = drift(rows)
        assert {f["kind"] for f in result["findings"]} == {"memory_changed", "snapshot_unexpected"}
        assert result["status"] == "incomplete"
        pg = next(iter(_portgroups(si, f"tn-{range_id[:8]}-").values()))
        spec = vim.dvs.DistributedVirtualPortgroup.ConfigSpec(
            configVersion=pg.config.configVersion,
            defaultPortConfig=vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy(
                vlan=vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec(inherited=False, vlanId=3999)))
        _wait(si, pg.ReconfigureDVPortgroup_Task(spec))
        result = drift(rows)
        assert "portgroup_vlan_changed" in {f["kind"] for f in result["findings"]} and result["status"] == "fail"
    finally:
        assert _run(mod.VsphereAPIProvisioner().destroy(range_id, output)).status == "ok"
