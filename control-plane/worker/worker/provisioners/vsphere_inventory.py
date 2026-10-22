"""What vCenter holds for a range, read by managed-object id (ADR 0013 §5, range certification S5).

Blocking pyVmomi reads, run through ``asyncio.to_thread`` by the provisioner
(vsphere_api.py). Every read is a property-collector call over a list of ids (one round
trip, not one per property), and an id vCenter no longer has is reported as missing, not
raised. What the readings are compared with, and how a teardown is graded, is
``worker/range_inventory.py`` (no SDK there).

* ``identity``: what a VM is recorded as at clone (moid, instance UUID, VMX, datastores,
  disk files, folder, CPU/RAM, NICs with port group key and MAC).
* ``live``: the same fields now, for the drift check, and each recorded port group's VLAN.
* ``all_vm_marks`` / ``portgroup_marks`` / ``unidentified``: everything that carries the
  range's annotation or port group owner mark (the ``tn-range`` tag is read over REST by the
  provisioner), read once and shared by every range of a drift sweep. Marks are hints,
  never authority to delete: what is not recorded is listed as *unidentified*.
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

from ..range_inventory import vm_owned
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


def _vm_reading(p: dict) -> dict:
    return {
        "exists": True, "name": _str(p.get("name")), "instance_uuid": _str(p.get("config.instanceUuid")),
        "num_cpu": p.get("config.hardware.numCPU"), "memory_mb": p.get("config.hardware.memoryMB"),
        "nics": nics_of(p.get("config.hardware.device")), "snapshots": snapshots_of(p.get("snapshot")),
    }


def live(si, vms: list[dict], portgroups: list[dict]) -> dict:
    """The drift reading of recorded VMs and port groups (range_inventory.recorded entries;
    may be several ranges' at once): one property-collector call for every VM, one for every
    vDS port group, one for every standard network and one for the hosts they are on.

    A VM whose recorded moid is gone but whose recorded instance UUID is registered under
    another moid (re-registered, vMotion to another vCenter and back) is read there and
    marked ``moved_to``. A standard port group's VLAN is read from each host that has it;
    one it cannot read on some host is ``vlan_unreadable`` (never assumed to be the record)."""
    out: dict = {"vms": {}, "portgroups": {}, "errors": []}
    ids = [v["moid"] for v in vms]
    if ids:
        props, missing = read_props(si, vim.VirtualMachine, ids, IDENTITY_PATHS)
        moved: dict[str, str] = {}
        for v in vms:
            if v["moid"] not in missing or not v.get("instance_uuid"):
                continue
            try:
                found = si.content.searchIndex.FindByUuid(uuid=v["instance_uuid"], vmSearch=True, instanceUuid=True)
            except Exception as exc:  # noqa: BLE001 -- reported; the VM is then just "gone"
                out["errors"].append(f"VM {v.get('name') or v['moid']}: search by instance UUID failed: {exc}")
                continue
            if found is not None:
                moved[v["moid"]] = found._moId
        more = read_props(si, vim.VirtualMachine, sorted(set(moved.values())), IDENTITY_PATHS)[0] if moved else {}
        for v in vms:
            moid = v["moid"]
            if moved.get(moid) in more:
                out["vms"][moid] = {**_vm_reading(more[moved[moid]]), "moved_to": moved[moid]}
            elif moid in missing:
                out["vms"][moid] = {"exists": False}
            else:
                out["vms"][moid] = _vm_reading(props[moid])
    dvs_ids = [pg["moid"] for pg in portgroups if pg.get("moid") and pg.get("switch_mode") != "vss"]
    vss_ids = [pg["moid"] for pg in portgroups if pg.get("moid") and pg.get("switch_mode") == "vss"]
    if dvs_ids:
        props, missing = read_props(si, vim.dvs.DistributedVirtualPortgroup, dvs_ids, PORTGROUP_PATHS)
        for moid in dvs_ids:
            out["portgroups"][moid] = {"exists": False} if moid in missing else {
                "exists": True, "name": _str(props[moid].get("name")),
                "vlan": _vlan(props[moid].get("config"))}
    if vss_ids:
        out["portgroups"].update(_vss_live(si, vss_ids))
    return out


def _vss_live(si, ids: list[str]) -> dict[str, dict]:
    """Each standard port group's name and its VLAN as every host that has it reports it."""
    props, missing = read_props(si, vim.Network, ids, ("name", "host"))
    host_ids = sorted({h._moId for p in props.values() for h in p.get("host") or []})
    hosts = read_props(si, vim.HostSystem, host_ids, ("config.network.portgroup",))[0] if host_ids else {}
    out: dict[str, dict] = {}
    for moid in ids:
        if moid in missing:
            out[moid] = {"exists": False}
            continue
        name, vlans, unreadable = _str(props[moid].get("name")), set(), False
        for host in props[moid].get("host") or []:
            pgs = hosts.get(host._moId, {}).get("config.network.portgroup")
            vlan = next((int(pg.spec.vlanId or 0) for pg in pgs or [] if pg.spec.name == name), None)
            if vlan is None:
                unreadable = True
            else:
                vlans.add(vlan)
        if unreadable or not vlans:
            out[moid] = {"exists": True, "name": name, "vlan": None, "vlan_unreadable": True}
        else:  # one VLAN everywhere: that one; hosts that disagree: all of them (never the record)
            out[moid] = {"exists": True, "name": name, "vlan": vlans.pop() if len(vlans) == 1 else sorted(vlans)}
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


def portgroup_marks(si, dvs) -> dict[str, dict]:
    """Every port group on ``dvs`` with its owner mark (description), key and VLAN, in one
    call: {moid: {"name", "key", "vlan", "description"}}."""
    if dvs is None:
        return {}
    ids = [pg._moId for pg in dvs.portgroup or []]
    props, _ = read_props(si, vim.dvs.DistributedVirtualPortgroup, ids, PORTGROUP_PATHS) if ids else ({}, set())
    return {moid: {"name": _str(p.get("name")), "key": _str(p.get("key")), "vlan": _vlan(p.get("config")),
                   "description": description(p.get("config"))} for moid, p in props.items()}


def unidentified(range_id: str, inv: dict, vm_marks: dict[str, dict], pg_marks: dict[str, dict], tagged: set[str],
                 annotated_range: Callable[[str], str]) -> list[dict]:
    """What carries ``range_id``'s mark (annotation, its tn-range tag on ``tagged`` moids, the
    port group owner mark) but is not one of its recorded items (``inv``:
    range_inventory.recorded), from readings shared by many ranges (``all_vm_marks``,
    ``portgroup_marks``). Pure: listed, never deleted."""
    recorded = {v["moid"]: v for v in inv["vms"]}
    out = []
    for moid, p in sorted(vm_marks.items()):
        if p.get("config.template"):
            continue
        marks = (["annotation"] if annotated_range(str(p.get("config.annotation") or "")) == range_id else []) + \
            (["tag"] if moid in tagged else [])
        rec = recorded.get(moid)
        if not marks or (rec is not None and vm_owned(rec, _str(p.get("config.instanceUuid"))) is not False):
            continue  # unmarked, or the recorded VM itself (``remaining`` reports it if still there)
        out.append({"kind": "vm", "moid": moid, "name": str(p.get("name") or ""),
                    "instance_uuid": _str(p.get("config.instanceUuid")), "marks": marks})
    known = {p["moid"] for p in inv["portgroups"] if p["moid"]} | {p["name"] for p in inv["portgroups"] if not p["moid"]}
    mark = live_vlans.owner_mark(range_id)
    for moid, pg in sorted(pg_marks.items()):
        if pg.get("description") == mark and moid not in known and pg.get("name") not in known:
            out.append({"kind": "portgroup", "moid": moid, "name": pg.get("name"), "key": pg.get("key"),
                        "vlan": pg.get("vlan"), "marks": ["owner_mark"]})
    return out


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
