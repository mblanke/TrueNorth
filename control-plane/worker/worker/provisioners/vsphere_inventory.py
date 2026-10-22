"""What vCenter holds for a range, read by managed-object id (ADR 0013 §5, range certification S5).

Blocking pyVmomi reads, run through ``asyncio.to_thread`` by the provisioner
(vsphere_api.py). Every read is a property-collector call over a list of ids (one round
trip, not one per property), and an id vCenter no longer has is reported as missing, not
raised. What the readings are compared with, and how a teardown is graded, is
``worker/range_inventory.py`` (no SDK there).

* ``identity``: what a VM is recorded as at clone (moid, instance UUID, VMX, datastores,
  disk files, folder, CPU/RAM, NICs with port group key and MAC).
* ``live``: the same fields now, for the drift check, and each recorded port group's VLAN.
* ``marked_vms`` / ``marked_portgroups``: everything that carries the range's annotation
  or port group owner mark (the ``tn-range`` tag is read over REST by the provisioner).
  Marks are hints, never authority to delete: the provisioner lists what is not recorded
  as *unidentified*.
* ``remaining``: which recorded ids still exist after a teardown: VMs (by moid, and by
  instance UUID anywhere in vCenter), disk files on their datastores (datastore browser),
  port groups, the folder.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

try:
    from pyVmomi import vim, vmodl
except ImportError:  # the provisioner refuses to run without it
    vim = vmodl = None  # type: ignore[assignment]

from . import vsphere_infra as infra
from . import vsphere_vlans as live_vlans

logger = logging.getLogger(__name__)

IDENTITY_PATHS = (
    "name", "config.instanceUuid", "config.uuid", "config.files.vmPathName", "config.annotation",
    "config.hardware.numCPU", "config.hardware.memoryMB", "config.hardware.device", "datastore", "parent",
    "snapshot",
)
MARK_PATHS = ("name", "config.annotation", "config.instanceUuid", "config.template")
PORTGROUP_PATHS = ("name", "key", "config")  # config whole, as vsphere_vlans reads it (VLAN, description)


def description(config) -> str:
    """A vDS port group's description (S1 writes the range's owner mark there)."""
    return str(getattr(config, "description", None) or "")


def read_props(si, vimtype, ids: list[str], paths) -> tuple[dict[str, dict], set[str]]:
    """``paths`` of the objects of ``vimtype`` named by ``ids``, in one RetrievePropertiesEx
    call: ({id: {path: value}}, ids vCenter does not have). vCenter fails the whole call
    with ManagedObjectNotFound naming one missing object; it is dropped and the call
    repeated. An unset property is simply absent."""
    pcq = vmodl.query.PropertyCollector
    pc = si.content.propertyCollector
    remaining, missing = list(dict.fromkeys(ids)), set()
    while remaining:
        spec = pcq.FilterSpec(
            objectSet=[pcq.ObjectSpec(obj=vimtype(i, si._stub), skip=False) for i in remaining],
            propSet=[pcq.PropertySpec(type=vimtype, all=False, pathSet=list(paths))],
        )
        try:
            result = pc.RetrievePropertiesEx(specSet=[spec], options=pcq.RetrieveOptions())
        except vmodl.fault.ManagedObjectNotFound as fault:
            gone = getattr(getattr(fault, "obj", None), "_moId", None)
            if gone not in remaining:
                raise
            remaining.remove(gone)
            missing.add(gone)
            continue
        props: dict[str, dict] = {}
        while result is not None:
            for content in result.objects or []:
                props[content.obj._moId] = {p.name: p.val for p in content.propSet or []}
            result = pc.ContinueRetrievePropertiesEx(token=result.token) if result.token else None
        return props, missing | {i for i in remaining if i not in props}
    return {}, missing


def _moid(ref) -> str | None:
    return getattr(ref, "_moId", None) if ref is not None else None


def _str(value) -> str | None:
    return None if value is None else str(value)


def nics_of(devices) -> list[dict]:
    """Each NIC in guest order: its port group key (vDS) or network name (standard), its MAC."""
    out = []
    for card in infra.nic_cards(devices or []):
        backing = getattr(card, "backing", None)
        port = getattr(backing, "port", None)
        out.append({"portgroup_key": _str(getattr(port, "portgroupKey", None)),
                    "network": _str(getattr(backing, "deviceName", None)),
                    "mac": str(getattr(card, "macAddress", "") or "").lower()})
    return out


def disks_of(devices) -> list[dict]:
    """Each virtual disk's file (``[datastore] dir/file.vmdk``) and its datastore moid."""
    out = []
    for dev in devices or []:
        if vim is not None and not isinstance(dev, vim.vm.device.VirtualDisk):
            continue
        backing = getattr(dev, "backing", None)
        path = getattr(backing, "fileName", None)
        if path:
            out.append({"path": str(path), "datastore": _moid(getattr(backing, "datastore", None))})
    return out


def snapshots_of(info) -> list[dict]:
    """Every snapshot in a VM's tree (``snapshot`` property), as ``{"moid", "name"}``."""
    out, stack = [], list(getattr(info, "rootSnapshotList", None) or [])
    while stack:
        node = stack.pop()
        out.append({"moid": _moid(getattr(node, "snapshot", None)), "name": str(getattr(node, "name", ""))})
        stack.extend(getattr(node, "childSnapshotList", None) or [])
    return sorted(out, key=lambda s: str(s["moid"]))


def identity(moid: str, props: dict) -> dict:
    """What the record holds for one VM (range_inventory.recorded reads it back)."""
    devices = props.get("config.hardware.device")
    return {
        "moid": moid,
        "instance_uuid": _str(props.get("config.instanceUuid")),
        "bios_uuid": _str(props.get("config.uuid")),
        "vmx": _str(props.get("config.files.vmPathName")),
        "folder": _moid(props.get("parent")),
        "datastores": sorted(m for m in (_moid(d) for d in props.get("datastore") or []) if m),
        "disks": disks_of(devices),
        "num_cpu": props.get("config.hardware.numCPU"),
        "memory_mb": props.get("config.hardware.memoryMB"),
        "nics": nics_of(devices),
        "recorded_at": datetime.now(UTC).isoformat(),
    }


def read_identity(si, vm_id: str) -> dict:
    props, missing = read_props(si, vim.VirtualMachine, [vm_id], IDENTITY_PATHS)
    if vm_id in missing:
        raise LookupError(f"{vm_id} is not in vCenter")
    return identity(vm_id, props[vm_id])


def _vlan(config) -> int | None:
    vlans, trunk = live_vlans.expand(getattr(getattr(config, "defaultPortConfig", None), "vlan", None))
    return next(iter(vlans)) if len(vlans) == 1 and not trunk else None


def live(si, vm_ids: list[str], portgroups: list[dict]) -> dict:
    """The drift reading: each recorded VM's identity fields and snapshots, each recorded
    port group's name and VLAN (range_inventory.compare)."""
    out: dict = {"vms": {}, "portgroups": {}, "errors": []}
    if vm_ids:
        props, missing = read_props(si, vim.VirtualMachine, vm_ids, IDENTITY_PATHS)
        for vm_id in vm_ids:
            if vm_id in missing:
                out["vms"][vm_id] = {"exists": False}
                continue
            p = props[vm_id]
            out["vms"][vm_id] = {
                "exists": True, "name": _str(p.get("name")), "instance_uuid": _str(p.get("config.instanceUuid")),
                "num_cpu": p.get("config.hardware.numCPU"), "memory_mb": p.get("config.hardware.memoryMB"),
                "nics": nics_of(p.get("config.hardware.device")), "snapshots": snapshots_of(p.get("snapshot")),
            }
    dvs_ids = [pg["moid"] for pg in portgroups if pg.get("moid") and pg.get("switch_mode") != "vss"]
    vss_ids = [pg["moid"] for pg in portgroups if pg.get("moid") and pg.get("switch_mode") == "vss"]
    if dvs_ids:
        props, missing = read_props(si, vim.dvs.DistributedVirtualPortgroup, dvs_ids, PORTGROUP_PATHS)
        for moid in dvs_ids:
            out["portgroups"][moid] = {"exists": False} if moid in missing else {
                "exists": True, "name": _str(props[moid].get("name")),
                "vlan": _vlan(props[moid].get("config"))}
    if vss_ids:  # a standard port group's VLAN is per host; its existence is what is read here
        recorded_vlan = {pg["moid"]: pg.get("vlan") for pg in portgroups}
        props, missing = read_props(si, vim.Network, vss_ids, ("name",))
        for moid in vss_ids:
            out["portgroups"][moid] = {"exists": False} if moid in missing else {
                "exists": True, "name": _str(props[moid].get("name")), "vlan": recorded_vlan.get(moid)}
    return out


def all_vm_marks(si) -> dict[str, dict]:
    """{moid: MARK_PATHS} of every VM in the vCenter, in one container-view pass."""
    pcq = vmodl.query.PropertyCollector
    content = si.content
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    try:
        spec = pcq.FilterSpec(
            objectSet=[pcq.ObjectSpec(obj=view, skip=True, selectSet=[pcq.TraversalSpec(
                name="vms", path="view", skip=False, type=vim.view.ContainerView)])],
            propSet=[pcq.PropertySpec(type=vim.VirtualMachine, all=False, pathSet=list(MARK_PATHS))],
        )
        pc = content.propertyCollector
        out: dict[str, dict] = {}
        result = pc.RetrievePropertiesEx(specSet=[spec], options=pcq.RetrieveOptions())
        while result is not None:
            for obj in result.objects or []:
                out[obj.obj._moId] = {p.name: p.val for p in obj.propSet or []}
            result = pc.ContinueRetrievePropertiesEx(token=result.token) if result.token else None
        return out
    finally:
        view.Destroy()


def marked_vms(si, range_id: str, annotated_range: Callable[[str], str], tagged: set[str]) -> dict[str, dict]:
    """Every non-template VM whose annotation names ``range_id`` or (``tagged``: moids the
    range's ``tn-range`` tag is attached to) that carries its tag: {moid: {"name",
    "instance_uuid", "marks"}}."""
    out = {}
    for moid, p in all_vm_marks(si).items():
        if p.get("config.template"):
            continue
        marks = []
        if annotated_range(str(p.get("config.annotation") or "")) == range_id:
            marks.append("annotation")
        if moid in tagged:
            marks.append("tag")
        if marks:
            out[moid] = {"name": str(p.get("name") or ""), "instance_uuid": _str(p.get("config.instanceUuid")),
                         "marks": marks}
    return out


def marked_portgroups(si, dvs, range_id: str) -> dict[str, dict]:
    """The port groups on ``dvs`` whose description carries the range's owner mark (S1):
    {moid: {"name", "key", "vlan"}}."""
    if dvs is None:
        return {}
    ids = [pg._moId for pg in dvs.portgroup or []]
    props, _ = read_props(si, vim.dvs.DistributedVirtualPortgroup, ids, PORTGROUP_PATHS) if ids else ({}, set())
    mark = live_vlans.owner_mark(range_id)
    return {moid: {"name": _str(p.get("name")), "key": _str(p.get("key")), "vlan": _vlan(p.get("config"))}
            for moid, p in props.items() if description(p.get("config")) == mark}


def file_exists(si, datastore_moid: str | None, path: str, wait) -> bool:
    """Whether ``[ds] dir/file`` is on its datastore (datastore browser). Raises when it
    cannot be told (no datastore recorded, the datastore is gone or unreadable)."""
    if not datastore_moid:
        raise LookupError(f"{path}: no datastore recorded")
    folder, _, name = path.rpartition("/")
    if not folder or not name:
        raise LookupError(f"{path}: not a datastore file path")
    browser = vim.Datastore(datastore_moid, si._stub).browser
    task = browser.SearchDatastore_Task(datastorePath=folder,
                                        searchSpec=vim.host.DatastoreBrowser.SearchSpec(matchPattern=[name]))
    try:
        wait(task)
    except vim.fault.FileNotFound:
        return False  # the VM's directory itself is gone
    files = getattr(getattr(task.info, "result", None), "file", None) or []
    return any(str(getattr(f, "path", "")) == name for f in files)


def remaining(si, inv: dict, wait) -> tuple[list[dict], list[str]]:
    """Which recorded ids still exist, and what could not be checked (``inv``:
    range_inventory.recorded). A recorded moid now holding a VM with another instance UUID
    is not this range's VM (not remaining); a recorded instance UUID found under any moid is."""
    left: list[dict] = []
    not_checked: list[str] = []
    vms = inv["vms"]
    if vms:
        props, missing = read_props(si, vim.VirtualMachine, [v["moid"] for v in vms], ("name", "config.instanceUuid"))
        for vm in vms:
            if vm["moid"] not in missing:
                live_uuid = _str(props.get(vm["moid"], {}).get("config.instanceUuid"))
                if not vm["instance_uuid"] or live_uuid == vm["instance_uuid"]:
                    left.append({"kind": "vm", "id": vm["moid"], "name": vm["name"]})
                    continue
            if vm["instance_uuid"]:
                try:
                    found = si.content.searchIndex.FindByUuid(uuid=vm["instance_uuid"], vmSearch=True,
                                                             instanceUuid=True)
                except Exception as exc:  # noqa: BLE001 -- reported, not guessed
                    not_checked.append(f"VM {vm['name']}: search by instance UUID failed: {exc}")
                    continue
                if found is not None:
                    left.append({"kind": "vm", "id": found._moId, "name": vm["name"],
                                 "why": f"instance UUID {vm['instance_uuid']} is still registered"})
    for vm in vms:
        for disk in vm["disks"]:
            try:
                if file_exists(si, disk.get("datastore"), disk["path"], wait):
                    left.append({"kind": "disk", "id": disk["path"], "path": disk["path"],
                                 "datastore": disk.get("datastore"), "name": vm["name"]})
            except Exception as exc:  # noqa: BLE001
                not_checked.append(f"disk {disk['path']}: {getattr(exc, 'msg', None) or exc}")
    by_type = {"dvs": [p for p in inv["portgroups"] if p["moid"] and p.get("switch_mode") != "vss"],
               "vss": [p for p in inv["portgroups"] if p["moid"] and p.get("switch_mode") == "vss"]}
    for kind, vimtype in (("dvs", vim.dvs.DistributedVirtualPortgroup), ("vss", vim.Network)):
        if by_type[kind]:
            _, missing = read_props(si, vimtype, [p["moid"] for p in by_type[kind]], ("name",))
            left += [{"kind": "portgroup", "id": p["moid"], "name": p["name"]}
                     for p in by_type[kind] if p["moid"] not in missing]
    not_checked += [f"port group {p['name']}: no managed-object id recorded" for p in inv["portgroups"] if not p["moid"]]
    folder = inv.get("folder") or {}
    if folder.get("moid"):
        _, missing = read_props(si, vim.Folder, [folder["moid"]], ("name",))
        if folder["moid"] not in missing:
            left.append({"kind": "folder", "id": folder["moid"], "name": folder.get("name") or ""})
    return left, not_checked
