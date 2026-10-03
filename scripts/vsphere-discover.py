#!/usr/bin/env python3
"""Read-only vSphere discovery for a TrueNorth deployment.

Inventories a vCenter (and the ESXi hosts behind it) and writes:

  <out>/vsphere-inventory.json   raw facts, as collected
  <out>/vsphere-inventory.md     human tables + analysis + the readiness gate

The spec for what is collected is truenorth-ai-vsphere-pack/truenorth-vsphere-discovery.md
(sections 2-5, 10-12, 14 and the section 17 readiness gate).

Usage:
    VSPHERE_PASSWORD=... .venv/bin/python scripts/vsphere-discover.py \\
        --host vcenter.lab.local --user administrator@vsphere.local --insecure \\
        [--range-vlans 100-199] [--iso-path "[esx01-local] ISO"] [--baseline old/vsphere-inventory.json]

Structure (logic lives in pure functions so it is testable with plain dicts):
    collect(si, rest) -> dict           thin: reads pyVmomi objects into plain data
    analyse(inventory) -> dict          verdicts, VLAN pool, ISO coverage, capacity, gate
    render_markdown(inventory, analysis) -> str
    main()

READ-ONLY CONTRACT
------------------
This script must never change the environment. It uses only:
  * SmartConnect / RetrieveContent / Disconnect;
  * container views and the property collector (RetrieveContents) to read properties.
    CreateContainerView makes a session-scoped view object, not an inventory object;
    the views are released when the session logs out;
  * LicenseManager.licenses and LicenseAssignmentManager.QueryAssignedLicenses;
  * CryptoManagerKmip.ListKmsClusters / the kmipServers property;
  * HostDatastoreBrowser search tasks (listed in ALLOWED_TASKS), which only list files;
  * the vSphere Automation REST API: POST /api/session (log in) and GET requests.
No *_Task outside ALLOWED_TASKS is ever invoked, and nothing named Set*/Create*/Delete*/
Destroy*/Reconfig*/PowerOn*/Remove*/Move*/Register* is called. The password is never
printed, logged or written.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

# The only vSphere tasks this script may start. Both list files on a datastore.
ALLOWED_TASKS = frozenset({"SearchDatastoreSubFolders_Task", "SearchDatastore_Task"})

GIB = 1024**3
TIB = 1024**4
EXPECTED_HOSTS = 4

# Planning assumptions (docs/vm-build-sheet.md section 1 and section 4).
VCPU_OVERCOMMIT = 4  # vCPU per physical core
ESXI_OVERHEAD_GB = 4  # per host
VCENTER_RESERVE = {"name": "vCenter (VCSA)", "vcpu": 8, "ram_gb": 32, "disk_gb": 600}
MGMT_VMS = [
    {"name": "TN-MGMT01", "vcpu": 16, "ram_gb": 96, "disk_gb": 600},
    {"name": "TN-DC01", "vcpu": 4, "ram_gb": 16, "disk_gb": 100},
    {"name": "TN-DEPOT01", "vcpu": 4, "ram_gb": 16, "disk_gb": 1100},
    {"name": "TN-BUILD01", "vcpu": 8, "ram_gb": 16, "disk_gb": 200},
    {"name": "TN-KMS01", "vcpu": 2, "ram_gb": 4, "disk_gb": 60},
]
# Per-range ceilings, one instance (vm-build-sheet section 4). None = not specified yet.
RANGE_SIZES = [
    {"name": "small-enterprise", "vms": 5, "vcpu": None, "ram_gb": None, "disk_tb": None},
    {"name": "medium-enterprise", "vms": 8, "vcpu": 20, "ram_gb": 44, "disk_tb": 1.0},
    {"name": "cloud-security", "vms": 16, "vcpu": 52, "ram_gb": 126, "disk_tb": 1.7},
    {"name": "soc-training", "vms": 23, "vcpu": 69, "ram_gb": 157, "disk_tb": 3.9},
    {"name": "red-team", "vms": 28, "vcpu": 67, "ram_gb": 149, "disk_tb": 2.1},
    {"name": "red-vs-blue", "vms": 40, "vcpu": 80, "ram_gb": 160, "disk_tb": 1.1},
    {"name": "large-enterprise", "vms": 50, "vcpu": 142, "ram_gb": 375, "disk_tb": 7.4},
]

VLAN_POOL_SIZE = 100
VLAN_POOL_MIN = 900
VLAN_POOL_MAX = 3999

# ISO coverage rules: catalogue image -> regex over the lower-cased file name.
# `need`: "initial" (spec section 10), "enabled" (catalogue enabled=yes), "optional".
# `kind`: "os" rules never match role media (SQL/Exchange/SharePoint/Office), because
# e.g. "sql_server_2022" would otherwise read as Windows Server 2022.
ROLE_MEDIA = re.compile(r"sql|exchange|sharepoint|office|visio|project")
ISO_RULES: list[dict[str, Any]] = [
    {"image": "ubuntu-lts", "label": "Ubuntu Server 24.04 LTS", "need": "initial", "kind": "os",
     "match": r"ubuntu[^a-z]*24\.?04(?!.*desktop)"},
    {"image": "srv2022", "label": "Windows Server 2022", "need": "initial", "kind": "os",
     "match": r"(server|srv|ws)[^a-z]*2022|2022[^a-z]*(server|srv)|20348|^server_eval_x64fre"},
    {"image": "srv2025", "label": "Windows Server 2025 (alternative to 2022)", "need": "optional", "kind": "os",
     "match": r"(server|srv|ws)[^a-z]*2025|2025[^a-z]*(server|srv)|26100.*server"},
    {"image": "srv2019", "label": "Windows Server 2019", "need": "enabled", "kind": "os",
     "match": r"(server|srv|ws)[^a-z]*2019|2019[^a-z]*(server|srv)|17763"},
    {"image": "srv2016", "label": "Windows Server 2016", "need": "enabled", "kind": "os",
     "match": r"(server|srv|ws)[^a-z]*2016|2016[^a-z]*(server|srv)|14393"},
    {"image": "win10-22h2", "label": "Windows 10 Enterprise 22H2", "need": "enabled", "kind": "os",
     "match": r"win(dows)?[ _.-]?10(?!\d)|19045|19044"},
    {"image": "win11-24h2", "label": "Windows 11 24H2", "need": "enabled", "kind": "os",
     "match": r"win(dows)?[ _.-]?11(?!\d)(?!.*26h1)|22631|26100(?!.*server)"},
    {"image": "win11-26h1", "label": "Windows 11 26H1", "need": "optional", "kind": "os",
     "match": r"win(dows)?[ _.-]?11(?!\d).*26h1"},
    {"image": "win7-sp1", "label": "Windows 7 SP1 (licensed media)", "need": "enabled", "kind": "os",
     "match": r"win(dows)?[ _.-]?7(?!\d)"},
    {"image": "kali", "label": "Kali Linux", "need": "enabled", "kind": "os", "match": r"kali"},
    {"image": "rocky", "label": "Rocky Linux 10 (catalogue target)", "need": "enabled", "kind": "os",
     "match": r"rocky[^a-z0-9]*10"},
    {"image": "rocky-9", "label": "Rocky Linux 9 (previous pin)", "need": "optional", "kind": "os",
     "match": r"rocky[^a-z0-9]*9"},
    {"image": "debian-13", "label": "Debian 13", "need": "optional", "kind": "os", "match": r"debian[^a-z0-9]*13"},
    {"image": "parrot", "label": "Parrot Security", "need": "optional", "kind": "os", "match": r"parrot"},
    {"image": "fedora", "label": "Fedora", "need": "optional", "kind": "os", "match": r"fedora"},
    {"image": "ubuntu-desktop", "label": "Ubuntu Desktop (any version)", "need": "optional", "kind": "os",
     "match": r"ubuntu.*desktop"},
    {"image": "pfsense", "label": "pfSense CE", "need": "enabled", "kind": "os", "match": r"pfsense|netgate"},
    {"image": "securityonion", "label": "Security Onion 2.4", "need": "enabled", "kind": "os",
     "match": r"security[ _.-]?onion"},
    {"image": "vyos", "label": "VyOS 1.4 (GAP, not approved)", "need": "optional", "kind": "os", "match": r"vyos"},
    # virtio-win is Proxmox only: Windows builds on vSphere take the pvscsi driver from the
    # VMware Tools ISO that ships on every ESXi host (productLocker), so neither is "missing".
    {"image": "virtio-win", "label": "virtio-win drivers (Proxmox only; not needed on vSphere)", "need": "optional",
     "kind": "driver", "match": r"virtio"},
    {"image": "vmware-tools", "label": "VMware Tools (ships on every host; pvscsi for Windows builds)",
     "need": "optional", "kind": "driver", "match": r"vmware[ _.-]?tools|vmtools"},
    {"image": "sql2022", "label": "SQL Server 2022 (role media)", "need": "optional", "kind": "role",
     "match": r"sql[^a-z]*server[^a-z]*2022"},
    {"image": "sql2025", "label": "SQL Server 2025 (role media)", "need": "optional", "kind": "role",
     "match": r"sql[^a-z]*server[^a-z]*2025"},
    {"image": "exchange", "label": "Exchange Server (role media)", "need": "optional", "kind": "role",
     "match": r"exchange"},
    {"image": "sharepoint", "label": "SharePoint Server (role media)", "need": "optional", "kind": "role",
     "match": r"sharepoint"},
    {"image": "office", "label": "Office (role media)", "need": "optional", "kind": "role", "match": r"office"},
    {"image": "esxi", "label": "ESXi installer (infrastructure)", "need": "optional", "kind": "infra",
     "match": r"vmvisor|esxi"},
    {"image": "vcsa", "label": "vCenter Server Appliance (infrastructure)", "need": "optional", "kind": "infra",
     "match": r"vcsa"},
]

VDS_EDITION_PATTERNS = ("enterprise plus", "enterpriseplus", "ent.plus", "entplus", "vvf",
                        "vsphere foundation", "vcf", "cloud foundation", "eval")
DRS_EDITION_PATTERNS = VDS_EDITION_PATTERNS + ("enterprise",)


# ─────────────────────────────────────────────────────────────────────────────
# Collection (thin; touches pyVmomi)
# ─────────────────────────────────────────────────────────────────────────────

def _view(content: Any, vimtype: Any) -> list[Any]:
    """Objects of one type under the root folder (session-scoped container view)."""
    view = content.viewManager.CreateContainerView(content.rootFolder, [vimtype], True)
    return list(view.view)


def _safe(fn: Any, default: Any = None) -> Any:
    try:
        value = fn()
    except Exception:  # noqa: BLE001 - discovery must keep going past one bad object
        return default
    return default if value is None else value


def _readonly_task(obj: Any, method: str, *args: Any, timeout: float = 900) -> Any:
    """Start a task only if it is on the read-only allowlist, and wait for it."""
    if method not in ALLOWED_TASKS:
        raise PermissionError(f"{method} is not on the read-only task allowlist")
    task = getattr(obj, method)(*args)
    deadline = time.monotonic() + timeout
    while task.info.state in ("queued", "running"):
        if time.monotonic() > deadline:
            raise TimeoutError(f"{method} did not finish in {timeout}s")
        time.sleep(1)
    if task.info.state != "success":
        err = task.info.error
        raise RuntimeError(getattr(err, "msg", None) or str(err))
    return task.info.result


def _vm_props(content: Any, vim: Any, vmodl: Any) -> list[dict[str, Any]]:
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    pc = vmodl.query.PropertyCollector
    traversal = pc.TraversalSpec(name="traverseEntities", path="view", skip=False, type=vim.view.ContainerView)
    obj_spec = pc.ObjectSpec(obj=view, skip=True, selectSet=[traversal])
    prop_spec = pc.PropertySpec(type=vim.VirtualMachine, all=False, pathSet=[
        "name", "config.template", "config.guestFullName", "runtime.host", "runtime.powerState"])
    result = content.propertyCollector.RetrieveContents([pc.FilterSpec(objectSet=[obj_spec], propSet=[prop_spec])])
    rows = []
    for obj in result or []:
        props = {p.name: p.val for p in obj.propSet}
        host = props.get("runtime.host")
        rows.append({
            "name": props.get("name"),
            "template": bool(props.get("config.template")),
            "guest_os": props.get("config.guestFullName"),
            "host": _safe(lambda h=host: h.name),
            "power_state": str(props.get("runtime.powerState") or ""),
        })
    return rows


def _licenses(content: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lm = content.licenseManager
    out = []
    for lic in _safe(lambda: lm.licenses, []):
        features, product, expires = [], None, None
        for prop in lic.properties or []:
            if prop.key == "feature":
                features.append(str(getattr(prop.value, "key", prop.value)))
            elif prop.key == "ProductName":
                product = str(prop.value)
            elif prop.key == "expirationDate":
                expires = str(prop.value)
        out.append({
            "name": lic.name,
            "edition_key": lic.editionKey,
            "product": product,
            "total": lic.total,
            "used": lic.used,
            "cost_unit": lic.costUnit,
            "expires": expires,
            "features": sorted(features),
            "key_suffix": (lic.licenseKey or "")[-5:],  # never store a full key
        })
    assignments = []
    lam = _safe(lambda: lm.licenseAssignmentManager)
    for a in _safe(lambda: lam.QueryAssignedLicenses(), []) if lam else []:
        assignments.append({
            "entity": a.entityDisplayName,
            "license_name": _safe(lambda a=a: a.assignedLicense.name),
            "edition_key": _safe(lambda a=a: a.assignedLicense.editionKey),
        })
    return out, assignments


def _key_providers(content: Any) -> dict[str, Any]:
    cm = _safe(lambda: content.cryptoManager)
    if cm is None:
        return {"status": "unavailable", "error": "no cryptoManager on this vCenter", "providers": []}
    try:
        clusters = cm.ListKmsClusters(includeKmsServers=True)
    except Exception:  # noqa: BLE001 - older API: fall back to the property
        clusters = _safe(lambda: cm.kmipServers, [])
    providers = []
    for c in clusters or []:
        providers.append({
            "id": _safe(lambda c=c: c.clusterId.id),
            "type": str(_safe(lambda c=c: c.managementType, "kmip")),
            "default": bool(_safe(lambda c=c: c.useAsDefault, False)),
            "servers": len(_safe(lambda c=c: c.servers, [])),
        })
    return {"status": "ok", "providers": providers}


def _host(h: Any) -> dict[str, Any]:
    hw = _safe(lambda: h.summary.hardware)
    product = _safe(lambda: h.summary.config.product)
    cfg = _safe(lambda: h.config)
    net = _safe(lambda: cfg.network)
    pnic_by_key = {p.key: p.device for p in _safe(lambda: net.pnic, [])}

    services: dict[str, list[str]] = {}
    for nc in _safe(lambda: cfg.virtualNicManagerInfo.netConfig, []):
        cand = {c.key: c.device for c in nc.candidateVnic or []}
        for sel in nc.selectedVnic or []:
            dev = cand.get(sel) or sel.rsplit("-", 1)[-1]
            services.setdefault(dev, []).append(nc.nicType)

    vmknics = []
    for v in _safe(lambda: net.vnic, []):
        vmknics.append({
            "device": v.device,
            "ip": _safe(lambda v=v: v.spec.ip.ipAddress),
            "netmask": _safe(lambda v=v: v.spec.ip.subnetMask),
            "portgroup": v.portgroup or None,
            "dvs_portgroup_key": _safe(lambda v=v: v.spec.distributedVirtualPort.portgroupKey),
            "mtu": _safe(lambda v=v: v.spec.mtu),
            "services": sorted(services.get(v.device, [])),
        })
    mgmt = next((v for v in vmknics if "management" in v["services"]), None) or next(
        (v for v in vmknics if v["device"] == "vmk0"), None)

    portgroups = [{"name": pg.spec.name, "vlan": pg.spec.vlanId, "vswitch": pg.spec.vswitchName}
                  for pg in _safe(lambda: net.portgroup, [])]
    vswitches = []
    for vs in _safe(lambda: net.vswitch, []):
        vswitches.append({
            "name": vs.name,
            "mtu": vs.mtu,
            "uplinks": [pnic_by_key.get(k, k) for k in vs.pnic or []],
            "portgroups": [{"name": p["name"], "vlan": p["vlan"]} for p in portgroups if p["vswitch"] == vs.name],
        })

    parent = _safe(lambda: h.parent)
    return {
        "name": h.name,
        "cluster": parent.name if parent is not None and type(parent).__name__.endswith("ClusterComputeResource") else None,
        "mgmt_ip": mgmt["ip"] if mgmt else None,
        "version": _safe(lambda: product.version),
        "build": _safe(lambda: product.build),
        "full_name": _safe(lambda: product.fullName),
        "vendor": _safe(lambda: hw.vendor),
        "model": _safe(lambda: hw.model),
        "cpu_model": _safe(lambda: hw.cpuModel),
        "cpu_mhz": _safe(lambda: hw.cpuMhz),
        "sockets": _safe(lambda: hw.numCpuPkgs),
        "cores": _safe(lambda: hw.numCpuCores),
        "threads": _safe(lambda: hw.numCpuThreads),
        "memory_bytes": _safe(lambda: hw.memorySize),
        "memory_used_bytes": _safe(lambda: h.summary.quickStats.overallMemoryUsage * 1024 * 1024),
        "connection_state": str(_safe(lambda: h.runtime.connectionState, "")),
        "maintenance": bool(_safe(lambda: h.runtime.inMaintenanceMode, False)),
        "pnics": [{"device": p.device, "driver": p.driver, "mac": p.mac,
                   "speed_mb": _safe(lambda p=p: p.linkSpeed.speedMb)} for p in _safe(lambda: net.pnic, [])],
        "vswitches": vswitches,
        "vmknics": vmknics,
        "dns": {
            "hostname": _safe(lambda: net.dnsConfig.hostName),
            "domain": _safe(lambda: net.dnsConfig.domainName),
            "servers": list(_safe(lambda: net.dnsConfig.address, [])),
        },
        "ntp_servers": list(_safe(lambda: cfg.dateTimeInfo.ntpConfig.server, [])),
        "hbas": [{"device": a.device, "model": a.model, "driver": a.driver, "type": type(a).__name__.rsplit(".", 1)[-1]}
                 for a in _safe(lambda: cfg.storageDevice.hostBusAdapter, [])],
    }


def _vlan_of_dvpg(spec: Any) -> dict[str, Any]:
    kind = type(spec).__name__ if spec is not None else ""
    if kind.endswith("TrunkVlanSpec"):
        return {"vlan_type": "trunk", "vlan_id": None,
                "trunk_ranges": [[r.start, r.end] for r in spec.vlanId or []]}
    if kind.endswith("PvlanSpec"):
        return {"vlan_type": "pvlan", "vlan_id": spec.pvlanId, "trunk_ranges": []}
    if kind.endswith("VlanIdSpec"):
        return {"vlan_type": "vlan", "vlan_id": spec.vlanId, "trunk_ranges": []}
    return {"vlan_type": "none", "vlan_id": None, "trunk_ranges": []}


def _dvswitches(content: Any, vim: Any) -> list[dict[str, Any]]:
    out = []
    for dvs in _view(content, vim.DistributedVirtualSwitch):
        pgs = []
        for pg in _safe(lambda dvs=dvs: dvs.portgroup, []):
            pgs.append({
                "name": pg.name,
                "key": pg.key,
                "uplink": bool(_safe(lambda pg=pg: pg.config.uplink, False)),
                **_vlan_of_dvpg(_safe(lambda pg=pg: pg.config.defaultPortConfig.vlan)),
            })
        out.append({
            "name": dvs.name,
            "version": _safe(lambda dvs=dvs: dvs.summary.productInfo.version),
            "mtu": _safe(lambda dvs=dvs: dvs.config.maxMtu),
            "uplinks": list(_safe(lambda dvs=dvs: dvs.config.uplinkPortPolicy.uplinkPortName, [])),
            "hosts": sorted(h.name for h in _safe(lambda dvs=dvs: dvs.summary.hostMember, [])),
            "portgroups": pgs,
        })
    return out


def _datastores(content: Any, vim: Any) -> tuple[list[dict[str, Any]], list[Any]]:
    out, objs = [], []
    for ds in _view(content, vim.Datastore):
        s = ds.summary
        hosts = sorted(m.key.name for m in _safe(lambda ds=ds: ds.host, [])
                       if _safe(lambda m=m: m.mountInfo.mounted, True))
        out.append({
            "name": s.name,
            "type": s.type,
            "capacity_bytes": s.capacity,
            "free_bytes": s.freeSpace,
            "accessible": bool(s.accessible),
            "local": _safe(lambda ds=ds: ds.info.vmfs.local),
            "url": s.url,
            "hosts": hosts,
            "shared": len(hosts) > 1,
        })
        objs.append(ds)
    return out, objs


def _search_isos(vim: Any, ds_objs: list[Any], iso_path: str | None) -> dict[str, Any]:
    details = vim.host.DatastoreBrowser.FileInfo.Details(fileSize=True, fileType=True, modification=True)
    spec = vim.host.DatastoreBrowser.SearchSpec(matchPattern=["*.iso", "*.ISO", "*.Iso"], details=details)
    wanted_ds = None
    if iso_path:
        m = re.match(r"\[([^\]]+)\]\s*(.*)", iso_path)
        wanted_ds = m.group(1) if m else None
    files, searched, errors = [], [], {}
    for ds in ds_objs:
        name = ds.summary.name
        if not ds.summary.accessible or (wanted_ds and name != wanted_ds):
            continue
        root = iso_path if wanted_ds else f"[{name}]"
        try:
            results = _readonly_task(ds.browser, "SearchDatastoreSubFolders_Task", root, spec)
        except Exception as exc:  # noqa: BLE001
            errors[name] = f"{type(exc).__name__}: {exc}"
            continue
        searched.append(name)
        for r in results or []:
            for f in r.file or []:
                files.append({
                    "datastore": name,
                    "folder": r.folderPath,
                    "path": f"{r.folderPath.rstrip('/')}/{f.path}" if not r.folderPath.endswith("]")
                    else f"{r.folderPath} {f.path}",
                    "file": f.path,
                    "size_bytes": f.fileSize,
                })
    return {"isos": files, "iso_search": {"hint": iso_path, "datastores_searched": searched, "errors": errors}}


def _hierarchy(content: Any, vim: Any) -> dict[str, Any]:
    def folder_path(f: Any) -> str:
        parts = []
        node = f
        while node is not None and isinstance(node, vim.Folder):
            parts.append(node.name)
            node = _safe(lambda n=node: n.parent)
        dc = node.name if node is not None else "?"
        return f"{dc}/" + "/".join(reversed(parts))

    clusters = []
    for c in _view(content, vim.ClusterComputeResource):
        clusters.append({
            "name": c.name,
            "ha_enabled": bool(_safe(lambda c=c: c.configurationEx.dasConfig.enabled, False)),
            "drs_enabled": bool(_safe(lambda c=c: c.configurationEx.drsConfig.enabled, False)),
            "drs_behavior": str(_safe(lambda c=c: c.configurationEx.drsConfig.defaultVmBehavior, "")),
            "hosts": sorted(h.name for h in c.host or []),
        })
    pools = [{"name": rp.name, "owner": _safe(lambda rp=rp: rp.owner.name), "parent": _safe(lambda rp=rp: rp.parent.name)}
             for rp in _view(content, vim.ResourcePool)]
    folders = sorted(folder_path(f) for f in _view(content, vim.Folder)
                     if "VirtualMachine" in (f.childType or []))
    return {
        "datacenters": [{"name": d.name} for d in _view(content, vim.Datacenter)],
        "clusters": clusters,
        "resource_pools": pools,
        "vm_folders": folders,
    }


def collect(si: Any, rest: dict[str, Any] | None = None, iso_path: str | None = None) -> dict[str, Any]:
    """Read everything into plain data. `rest` is the result of collect_rest()."""
    from pyVmomi import vim, vmodl

    content = si.RetrieveContent()
    about = content.about
    licenses, assignments = _licenses(content)
    datastores, ds_objs = _datastores(content, vim)
    vms = _vm_props(content, vim, vmodl)
    per_host: dict[str, int] = {}
    for vm in vms:
        if not vm["template"] and vm["host"]:
            per_host[vm["host"]] = per_host.get(vm["host"], 0) + 1
    hosts = [_host(h) for h in _view(content, vim.HostSystem)]
    for h in hosts:
        h["vm_count"] = per_host.get(h["name"], 0)

    inv: dict[str, Any] = {
        "collected_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "vcenter": {
            "full_name": about.fullName,
            "version": about.version,
            "build": about.build,
            "api_version": about.apiVersion,
            "instance_uuid": about.instanceUuid,
        },
        "licenses": licenses,
        "license_assignments": assignments,
        **_hierarchy(content, vim),
        "hosts": sorted(hosts, key=lambda h: h["name"]),
        "datastores": sorted(datastores, key=lambda d: d["name"]),
        "dvswitches": _dvswitches(content, vim),
        "vms": {
            "total": sum(1 for v in vms if not v["template"]),
            "powered_on": sum(1 for v in vms if not v["template"] and v["power_state"] == "poweredOn"),
            "per_host": per_host,
            "vcenter_vm_host": next((v["host"] for v in vms
                                     if re.search(r"vcenter|vcsa", v["name"] or "", re.I) and not v["template"]), None),
        },
        "templates": sorted(({"name": v["name"], "guest_os": v["guest_os"], "host": v["host"]}
                             for v in vms if v["template"]), key=lambda t: t["name"] or ""),
        "key_providers": _key_providers(content),
        **_search_isos(vim, ds_objs, iso_path),
    }
    rest = rest or {}
    inv["content_libraries"] = rest.get("content_libraries", {"status": "unavailable", "error": "not queried", "libraries": []})
    inv["appliance"] = rest.get("appliance", {})
    return inv


def collect_rest(host: str, user: str, password: str, verify: bool, timeout: float = 30) -> dict[str, Any]:
    """Content libraries and appliance NTP/DNS/cert via the Automation REST API. GET only."""
    import httpx

    def scrub(exc: Exception) -> str:
        return f"{type(exc).__name__}: {exc}".replace(password, "***") if password else f"{type(exc).__name__}: {exc}"

    out: dict[str, Any] = {
        "content_libraries": {"status": "unavailable", "error": None, "libraries": []},
        "appliance": {},
    }
    try:
        client = httpx.Client(base_url=f"https://{host}", verify=verify, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        out["content_libraries"]["error"] = scrub(exc)
        return out
    with client:
        try:
            r = client.post("/api/session", auth=(user, password))
            r.raise_for_status()
            client.headers["vmware-api-session-id"] = r.json()
        except Exception as exc:  # noqa: BLE001
            out["content_libraries"]["error"] = scrub(exc)
            return out

        def get(path: str, **params: Any) -> Any:
            resp = client.get(path, params=params or None)
            resp.raise_for_status()
            return resp.json()

        try:
            libs = []
            for lib_id in get("/api/content/library"):
                lib = get(f"/api/content/library/{lib_id}")
                items = []
                for item_id in get("/api/content/library/item", library_id=lib_id):
                    try:
                        it = get(f"/api/content/library/item/{item_id}")
                        items.append({"name": it.get("name"), "type": it.get("type"), "size_bytes": it.get("size")})
                    except Exception as exc:  # noqa: BLE001
                        items.append({"name": item_id, "type": None, "size_bytes": None, "error": scrub(exc)})
                libs.append({
                    "name": lib.get("name"),
                    "type": lib.get("type"),
                    "storage": [b.get("datastore_id") or b.get("storage_uri") for b in lib.get("storage_backings", [])],
                    "items": items,
                })
            out["content_libraries"] = {"status": "ok", "error": None, "libraries": libs}
        except Exception as exc:  # noqa: BLE001
            out["content_libraries"]["error"] = scrub(exc)

        for key, path in (("ntp", "/api/appliance/ntp"),
                          ("dns", "/api/appliance/networking/dns/servers"),
                          ("hostname", "/api/appliance/networking/dns/hostname"),
                          ("tls_certificate", "/api/vcenter/certificate-management/vcenter/tls")):
            try:
                val = get(path)
                if key == "tls_certificate" and isinstance(val, dict):
                    val = {k: val.get(k) for k in ("subject_dn", "issuer_dn", "valid_from", "valid_to")}
                out["appliance"][key] = val
            except Exception as exc:  # noqa: BLE001
                out["appliance"][key] = {"unavailable": scrub(exc)}
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Analysis (pure)
# ─────────────────────────────────────────────────────────────────────────────

def _usable_hosts(inv: dict[str, Any]) -> list[dict[str, Any]]:
    return [h for h in inv.get("hosts", [])
            if h.get("connection_state", "connected") == "connected" and not h.get("maintenance")]


def analyse_storage(inv: dict[str, Any], switch_mode: str | None = None) -> dict[str, Any]:
    """Storage verdict and VSPHERE_PLACEMENT.

    shared datastore on every host -> cluster; otherwise each VM lives on its host's
    local datastore: with a vDS the port groups span hosts, so a range can be spread
    across hosts (`spread`, the provisioner default); with standard vSwitches a range
    must stay on one host (`per-range-host`).
    """
    hosts = sorted(h["name"] for h in _usable_hosts(inv))
    dss = [d for d in inv.get("datastores", []) if d.get("accessible", True)]
    if not hosts or not dss:
        return {"verdict": "unknown", "placement": None, "shared_datastores": [], "per_host_datastore": {},
                "reason": "no hosts or datastores collected"}
    everywhere = [d["name"] for d in dss if len(hosts) > 1 and set(hosts) <= set(d.get("hosts", []))]
    some = [d["name"] for d in dss if len(d.get("hosts", [])) > 1]
    per_host = {}
    for h in hosts:
        mine = [d for d in dss if h in d.get("hosts", [])]
        if mine:
            per_host[h] = max(mine, key=lambda d: d.get("free_bytes") or 0)["name"]
    local_placement = "spread" if switch_mode == "vds" else "per-range-host"
    local_effect = ("vDS port groups span hosts, so each VM goes on the host with most vCPU headroom, "
                    "on that host's local datastore" if local_placement == "spread"
                    else "standard vSwitches do not span hosts, so a range's VMs must stay on one host")
    if everywhere:
        verdict, placement = "shared", "cluster"
        reason = f"{', '.join(everywhere)} mounted by all {len(hosts)} hosts"
    elif some:
        verdict, placement = "partially-shared", local_placement
        reason = f"{', '.join(some)} shared by some hosts only; no datastore reaches every host; {local_effect}"
    else:
        verdict, placement = "local-only", local_placement
        reason = f"every datastore is mounted by a single host; {local_effect}"
    return {"verdict": verdict, "placement": placement, "shared_datastores": everywhere,
            "partially_shared_datastores": [s for s in some if s not in everywhere],
            "per_host_datastore": per_host, "reason": reason}


def _licence_text(lic: dict[str, Any]) -> str:
    return " ".join(str(lic.get(k) or "") for k in ("name", "edition_key", "product")).lower()


def analyse_switch(inv: dict[str, Any]) -> dict[str, Any]:
    lics = inv.get("licenses", [])
    has_vds = any(any(p in _licence_text(lic) for p in VDS_EDITION_PATTERNS) or "dvs" in lic.get("features", [])
                  for lic in lics)
    has_drs = any(any(p in _licence_text(lic) for p in DRS_EDITION_PATTERNS) or "drs" in lic.get("features", [])
                  for lic in lics) or any(c.get("drs_enabled") for c in inv.get("clusters", []))
    existing = [d["name"] for d in inv.get("dvswitches", [])]
    eval_mode = any("eval" in _licence_text(lic) for lic in lics)
    if has_vds or existing:
        mode, verdict = "vds", "distributed switch allowed"
    elif lics:
        mode, verdict = "vss", "licence does not include the distributed switch"
    else:
        mode, verdict = "vss", "unknown (no licence data); vss is the safe default"
    notes = []
    if eval_mode:
        notes.append("an evaluation licence is in use: VDS works now but stops when the evaluation expires")
    if existing and not has_vds:
        notes.append("a VDS already exists although no licence matched the VDS pattern; confirm the edition")
    return {"mode": mode, "verdict": verdict, "has_vds": has_vds, "has_drs": has_drs,
            "existing_dvswitches": existing, "evaluation": eval_mode, "notes": notes,
            "known": bool(lics) or bool(existing)}


def analyse_vtpm(inv: dict[str, Any]) -> dict[str, Any]:
    kp = inv.get("key_providers") or {}
    if kp.get("status") != "ok":
        return {"verdict": "unknown", "available": None, "note": kp.get("error") or "key providers not queried"}
    providers = kp.get("providers", [])
    if providers:
        return {"verdict": "available", "available": True,
                "note": f"{len(providers)} key provider(s): {', '.join(str(p.get('id')) for p in providers)}"}
    return {"verdict": "none", "available": False,
            "note": "no key provider: add a Native Key Provider before building win11-24h2 (needs vTPM)"}


def used_vlans(inv: dict[str, Any]) -> tuple[list[int], list[str]]:
    """VLAN IDs in use across vSwitch and dvportgroups, plus a list of trunk notes.

    VSS VLAN 4095 and dvportgroup trunks spanning >1000 IDs mean "pass everything"
    (VGT); they are reported as trunks, not as consuming every ID.
    """
    used: set[int] = set()
    trunks: list[str] = []
    for h in inv.get("hosts", []):
        for vs in h.get("vswitches", []):
            for pg in vs.get("portgroups", []):
                v = pg.get("vlan")
                if v == 4095:
                    trunks.append(f"{h['name']}/{vs['name']}/{pg['name']}: 4095 (all VLANs)")
                elif isinstance(v, int) and 0 < v < 4095:
                    used.add(v)
    for d in inv.get("dvswitches", []):
        for pg in d.get("portgroups", []):
            if pg.get("uplink"):
                continue
            if pg.get("vlan_type") in ("vlan", "pvlan") and isinstance(pg.get("vlan_id"), int) and 0 < pg["vlan_id"] < 4095:
                used.add(pg["vlan_id"])
            for start, end in pg.get("trunk_ranges", []):
                if end - start + 1 > 1000:
                    trunks.append(f"{d['name']}/{pg['name']}: {start}-{end}")
                else:
                    used.update(range(max(start, 1), min(end, 4094) + 1))
    return sorted(used), trunks


def suggest_vlan_block(used: list[int], size: int = VLAN_POOL_SIZE,
                       low: int = VLAN_POOL_MIN, high: int = VLAN_POOL_MAX) -> tuple[int, int] | None:
    taken = set(used)
    start = low
    while start + size - 1 <= high:
        clash = [v for v in range(start, start + size) if v in taken]
        if not clash:
            return start, start + size - 1
        start = max(clash) + 1
    return None


def match_isos(isos: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    matched_files: set[str] = set()
    def base(i: dict[str, Any]) -> str:
        return (i.get("file") or i.get("path") or "").rsplit("/", 1)[-1].lower()

    for rule in ISO_RULES:
        pat = re.compile(rule["match"])
        hits = [i for i in isos if pat.search(base(i))
                and not (rule["kind"] == "os" and ROLE_MEDIA.search(base(i)))]
        matched_files.update(i.get("path") or i.get("file") for i in hits)
        rows.append({"image": rule["image"], "label": rule["label"], "need": rule["need"], "kind": rule["kind"],
                     "files": [i.get("path") or i.get("file") for i in hits]})
    by_image = {r["image"]: r for r in rows}
    missing = [r["image"] for r in rows if not r["files"] and r["need"] in ("initial", "enabled")]
    # Spec section 10: Windows Server 2022 *or* 2025 satisfies the initial need.
    if by_image["srv2025"]["files"] and "srv2022" in missing:
        missing.remove("srv2022")
    unmatched = [i.get("path") or i.get("file") for i in isos if (i.get("path") or i.get("file")) not in matched_files]
    return {
        "rows": rows,
        "missing": missing,
        "unmatched_files": unmatched,
        "ubuntu_ok": bool(by_image["ubuntu-lts"]["files"]),
        "windows_server_ok": bool(by_image["srv2022"]["files"] or by_image["srv2025"]["files"]),
    }


def analyse_capacity(inv: dict[str, Any], placement: str | None) -> dict[str, Any]:
    """Range capacity.

    The vCenter (VCSA) host is excluded from range placement altogether: it carries the
    VCSA and vCLS, and losing it to a noisy range takes the control plane down. The
    management VMs (vm-build-sheet section 1) are then placed greedily on the remaining
    hosts, most free RAM first, the same rule the lab used for TN-MGMT01/TN-DC01.
    vCPU capacity is physical cores x VCPU_OVERCOMMIT; RAM is 1:1.
    """
    hosts = _usable_hosts(inv)
    if not hosts:
        return {"known": False}
    names = sorted(h["name"] for h in hosts)
    vc_host = inv.get("vms", {}).get("vcenter_vm_host")
    excluded = vc_host if vc_host in names and len(names) > 1 else None
    if vc_host is None and len(names) > 1:
        excluded = names[0]  # VCSA not found by name: assume the first host (esx01) carries it
    range_hosts = [h for h in hosts if h["name"] != excluded]
    res_vcpu = sum(r["vcpu"] for r in MGMT_VMS)
    res_ram = sum(r["ram_gb"] for r in MGMT_VMS)
    res_disk_tb = sum(r["disk_gb"] for r in [VCENTER_RESERVE, *MGMT_VMS]) / 1024

    per_host = []
    for h in sorted(range_hosts, key=lambda h: h["name"]):
        per_host.append({"name": h["name"], "vcpu": (h.get("cores") or 0) * VCPU_OVERCOMMIT,
                         "ram_gb": (h.get("memory_bytes") or 0) / GIB - ESXI_OVERHEAD_GB, "mgmt_vms": []})
    for vm in sorted(MGMT_VMS, key=lambda v: -v["ram_gb"]):
        target = max(per_host, key=lambda h: (h["ram_gb"], -len(h["mgmt_vms"])))
        target["vcpu"] -= vm["vcpu"]
        target["ram_gb"] -= vm["ram_gb"]
        target["mgmt_vms"].append(vm["name"])
    for h in per_host:
        h["vcpu"] = max(h["vcpu"], 0)
        h["ram_gb"] = round(max(h["ram_gb"], 0.0), 1)

    seen: set[str] = set()
    total_storage = free_storage = range_free = 0
    range_names = {h["name"] for h in per_host}
    for d in inv.get("datastores", []):
        if d["name"] in seen or not d.get("accessible", True):
            continue
        seen.add(d["name"])
        total_storage += d.get("capacity_bytes") or 0
        free_storage += d.get("free_bytes") or 0
        if range_names & set(d.get("hosts", [])):
            range_free += d.get("free_bytes") or 0

    pool_vcpu = sum(h["vcpu"] for h in per_host)
    pool_ram = sum(h["ram_gb"] for h in per_host)
    # Range storage: free space on datastores a range host can reach, minus management disks
    # (the VCSA disk sits on the excluded host's datastore, so only the management VMs count).
    mgmt_disk_tb = sum(r["disk_gb"] for r in MGMT_VMS) / 1024
    storage_avail_tb = max(range_free / TIB - mgmt_disk_tb, 0)

    def fit(vcpu: float, ram: float, r: dict[str, Any]) -> int:
        return int(max(min(vcpu // r["vcpu"], ram // r["ram_gb"]), 0))

    ranges = []
    for r in RANGE_SIZES:
        if not r["vcpu"] or not r["ram_gb"]:
            ranges.append({**r, "pooled": None, "per_host": None, "one_host_down": None,
                           "concurrent": None, "storage_bound": None, "limit": None})
            continue
        pooled = fit(pool_vcpu, pool_ram, r)
        per = [fit(h["vcpu"], h["ram_gb"], r) for h in per_host]
        packed = sum(per)
        # One range host down: lose the host that contributes most.
        pooled_mode = placement in ("cluster", "spread")
        if pooled_mode:
            worst = max(per_host, key=lambda h: (h["vcpu"], h["ram_gb"]))
            down = fit(pool_vcpu - worst["vcpu"], pool_ram - worst["ram_gb"], r)
        else:
            down = packed - max(per)
        cpu_limited = pool_vcpu / r["vcpu"] <= pool_ram / r["ram_gb"]
        ranges.append({
            **r,
            "pooled": pooled,
            "per_host": packed,
            "one_host_down": max(down, 0),
            "concurrent": pooled if pooled_mode else packed,
            "storage_bound": int(storage_avail_tb // r["disk_tb"]),
            "limit": "CPU" if cpu_limited else "RAM",
        })
    return {
        "known": True,
        "hosts": len(hosts),
        "physical_cores": sum(h.get("cores") or 0 for h in hosts),
        "logical_cpus": sum(h.get("threads") or 0 for h in hosts),
        "ram_gb": round(sum((h.get("memory_bytes") or 0) for h in hosts) / GIB, 1),
        "storage_tb": round(total_storage / TIB, 2),
        "storage_free_tb": round(free_storage / TIB, 2),
        "excluded_host": excluded,
        "excluded_reason": (f"{excluded} carries the vCenter (VCSA, {VCENTER_RESERVE['vcpu']} vCPU / "
                            f"{VCENTER_RESERVE['ram_gb']} GB) and is excluded from range placement"
                            + ("" if vc_host else "; VCSA VM not found by name, assumed on the first host"))
        if excluded else None,
        "range_hosts": [h["name"] for h in per_host],
        "reserve": {"vcpu": res_vcpu, "ram_gb": res_ram, "disk_tb": round(res_disk_tb, 2),
                    "items": MGMT_VMS, "esxi_overhead_gb_per_host": ESXI_OVERHEAD_GB},
        "overcommit": f"{VCPU_OVERCOMMIT}:1 vCPU per physical core, 1:1 RAM",
        "range_vcpu": pool_vcpu,
        "range_ram_gb": round(pool_ram, 1),
        "range_storage_tb": round(storage_avail_tb, 2),
        "per_host": per_host,
        "ranges": ranges,
    }


def _gate(inv: dict[str, Any], a: dict[str, Any]) -> list[dict[str, str]]:
    hosts = inv.get("hosts", [])
    n = len(hosts)
    enough = n >= EXPECTED_HOSTS

    def all_have(key: str) -> bool:
        return bool(hosts) and all(h.get(key) for h in hosts)

    def status(ok: bool, partial: bool = False) -> str:
        return "known" if ok else ("partial" if partial else "unknown")

    isos = inv.get("isos", [])
    iso_ds = sorted({i["datastore"] for i in isos if i.get("datastore")})
    iso_vis = {d["name"]: d.get("hosts", []) for d in inv.get("datastores", []) if d["name"] in iso_ds}
    dns = all(h.get("dns", {}).get("servers") for h in hosts) if hosts else False
    ntp = all(h.get("ntp_servers") for h in hosts) if hosts else False
    cl = inv.get("content_libraries", {})
    vc = inv.get("vcenter", {})
    items = [
        ("vCenter endpoint/version.", status(bool(vc.get("version"))), f"{vc.get('full_name') or '?'}"),
        ("All four ESXi versions.", status(enough and all_have("version"), bool(hosts)),
         f"{n} host(s): " + ", ".join(f"{h['name']} {h.get('version')} ({h.get('build')})" for h in hosts)),
        ("CPU/core count for all hosts.", status(enough and all_have("cores"), bool(hosts)), f"{n} host(s)"),
        ("RAM for all hosts.", status(enough and all_have("memory_bytes"), bool(hosts)), f"{n} host(s)"),
        ("Physical NICs and link speeds.", status(enough and all_have("pnics"), bool(hosts)),
         "link speed blank = link down"),
        ("Datastores and free capacity.", status(bool(inv.get("datastores"))), f"{len(inv.get('datastores', []))} datastore(s)"),
        ("ESX-01 ISO datastore/path.", status(bool(isos)),
         ", ".join(sorted({i.get('folder') or '' for i in isos})) or "no .iso files found"),
        ("ISO datastore host visibility.", status(bool(iso_vis)),
         "; ".join(f"{k}: {', '.join(v)}" for k, v in iso_vis.items()) or "no ISO datastore"),
        ("Management network.", status(all_have("mgmt_ip"), bool(hosts)), "vmk with management tag (or vmk0)"),
        ("Candidate range network strategy.", status(a["switch"]["known"]),
         f"VSPHERE_RANGE_SWITCH_MODE={a['switch']['mode']}"),
        ("VLAN availability.",
         {"reserved": "known", "free-search": "partial"}.get(a["vlan"]["source"] or "", "unknown"),
         f"operator-reserved {a['vlan']['reserved']} is free in vSphere" if a["vlan"]["source"] == "reserved"
         else "free in vSphere only; confirm the physical switch trunks the suggested block"),
        ("DNS.", status(dns), "from host DNS config"),
        ("NTP.", status(ntp), "from host NTP config"),
        ("DHCP/IPAM plan.", "unknown", "not discoverable from vSphere; decide"),
        ("Ubuntu Server ISO availability.", status(a["iso"]["ubuntu_ok"]), "ubuntu-lts 24.04"),
        ("Windows Server ISO availability.", status(a["iso"]["windows_server_ok"]), "Server 2022 or 2025"),
        ("Content Library status.", status(cl.get("status") == "ok"),
         f"{len(cl.get('libraries', []))} librar(ies)" if cl.get("status") == "ok" else (cl.get("error") or "unavailable")),
        ("Existing templates.", status("templates" in inv), f"{len(inv.get('templates', []))} template(s)"),
        ("vCenter API access approach.", status(bool(vc.get("version"))), "SOAP API reachable with the discovery account"),
        ("TrueNorth service account plan.", "unknown", "decide; least-privilege role, not created by discovery"),
        ("Initial VM placement.", "partial" if a["storage"]["verdict"] != "unknown" else "unknown",
         "recommendation generated below; needs operator sign-off"),
        ("Management/range isolation plan.", "unknown", "decide; not discoverable from vSphere"),
    ]
    return [{"item": i, "status": s, "evidence": e} for i, s, e in items]


def parse_vlan_range(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", text or "")
    if not m:
        raise ValueError(f"VLAN range must look like 100-199, got {text!r}")
    lo, hi = int(m.group(1)), int(m.group(2))
    if not 1 <= lo <= hi <= 4094:
        raise ValueError(f"VLAN range {text!r} must be within 1-4094 and low <= high")
    return lo, hi


def analyse_vlans(inv: dict[str, Any], range_vlans: str | None = None) -> dict[str, Any]:
    """Pick VSPHERE_VLAN_POOL: the operator's reserved block if it is clean, else a free block."""
    used, trunks = used_vlans(inv)
    out: dict[str, Any] = {"used": used, "trunks": trunks, "reserved": range_vlans,
                           "reserved_clashes": [], "source": None, "suggested_pool": None}
    if range_vlans:
        lo, hi = parse_vlan_range(range_vlans)
        clashes = [v for v in used if lo <= v <= hi]
        out["reserved_clashes"] = clashes
        if not clashes:
            out.update(source="reserved", suggested_pool=f"{lo}-{hi}")
            return out
    block = suggest_vlan_block(used)
    if block:
        out.update(source="free-search", suggested_pool=f"{block[0]}-{block[1]}")
    return out


def diff_inventory(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """What changed since a previous vsphere-inventory.json (either the wrapper or the bare inventory)."""
    old = old.get("inventory", old)

    def names(inv: dict[str, Any], key: str, field: str = "name") -> set[str]:
        return {str(x.get(field)) for x in inv.get(key, []) if isinstance(x, dict)}

    def pgs(inv: dict[str, Any]) -> set[str]:
        out = {f"{vs['name']}/{p['name']} ({p.get('vlan')})" for h in inv.get("hosts", [])
               for vs in h.get("vswitches", []) for p in vs.get("portgroups", [])}
        out |= {f"{d['name']}/{p['name']} ({p.get('vlan_id') if p.get('vlan_type') != 'trunk' else 'trunk'})"
                for d in inv.get("dvswitches", []) for p in d.get("portgroups", []) if not p.get("uplink")}
        return out

    def added_removed(a: set[str], b: set[str]) -> dict[str, list[str]]:
        return {"added": sorted(b - a), "removed": sorted(a - b)}

    changes = []
    old_hosts = {h["name"]: h for h in old.get("hosts", [])}
    for h in new.get("hosts", []):
        prev = old_hosts.get(h["name"])
        if not prev:
            continue
        for f in ("version", "build", "cores", "threads", "memory_bytes", "connection_state", "maintenance", "mgmt_ip"):
            if prev.get(f) != h.get(f):
                changes.append(f"host {h['name']}: {f} {prev.get(f)} → {h.get(f)}")
    old_ds = {d["name"]: d for d in old.get("datastores", [])}
    for d in new.get("datastores", []):
        prev = old_ds.get(d["name"])
        if prev and prev.get("hosts") != d.get("hosts"):
            changes.append(f"datastore {d['name']}: hosts {prev.get('hosts')} → {d.get('hosts')}")
        if prev and prev.get("free_bytes") and d.get("free_bytes") is not None:
            delta = (d["free_bytes"] - prev["free_bytes"]) / GIB
            if abs(delta) >= 10:
                changes.append(f"datastore {d['name']}: free {delta:+,.0f} GB")
    old_vc, new_vc = old.get("vcenter", {}), new.get("vcenter", {})
    if old_vc.get("build") != new_vc.get("build"):
        changes.append(f"vCenter build {old_vc.get('build')} → {new_vc.get('build')}")
    return {
        "baseline_collected_at": old.get("collected_at"),
        "hosts": added_removed(names(old, "hosts"), names(new, "hosts")),
        "datastores": added_removed(names(old, "datastores"), names(new, "datastores")),
        "portgroups": added_removed(pgs(old), pgs(new)),
        "templates": added_removed(names(old, "templates"), names(new, "templates")),
        "isos": added_removed(names(old, "isos", "path"), names(new, "isos", "path")),
        "licenses": added_removed(names(old, "licenses"), names(new, "licenses")),
        "changes": changes,
    }


def analyse(inv: dict[str, Any], range_vlans: str | None = None,
            baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    switch = analyse_switch(inv)
    storage = analyse_storage(inv, switch["mode"])
    vtpm = analyse_vtpm(inv)
    vlan = analyse_vlans(inv, range_vlans)
    iso = match_isos(inv.get("isos", []))
    capacity = analyse_capacity(inv, storage["placement"])
    result: dict[str, Any] = {"storage": storage, "switch": switch, "vtpm": vtpm, "vlan": vlan,
                              "iso": iso, "capacity": capacity}
    env: dict[str, str] = {"VSPHERE_RANGE_SWITCH_MODE": switch["mode"]}
    if storage["placement"]:
        env["VSPHERE_PLACEMENT"] = storage["placement"]
    if vlan["suggested_pool"]:
        env["VSPHERE_VLAN_POOL"] = vlan["suggested_pool"]
    result["env"] = env
    if baseline is not None:
        result["diff"] = diff_inventory(baseline, inv)
    gate = _gate(inv, result)
    result["gate"] = gate
    result["gate_passed"] = all(g["status"] == "known" for g in gate)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Rendering (pure)
# ─────────────────────────────────────────────────────────────────────────────

def _gb(b: Any) -> str:
    return "?" if b is None else f"{b / GIB:,.0f} GB"


def _tb(b: Any) -> str:
    return "?" if b is None else f"{b / TIB:,.2f} TB"


def _cell(v: Any) -> str:
    if v is None or v == "" or v == []:
        return "—"
    return str(v).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[Any]], align: str | None = None) -> str:
    align = align or "l" * len(headers)
    sep = ["---:" if a == "r" else "---" for a in align]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(sep) + "|"]
    lines += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    if not rows:
        lines.append("| " + " | ".join(["—"] * len(headers)) + " |")
    return "\n".join(lines)


def _network_rows(inv: dict[str, Any]) -> list[list[Any]]:
    mgmt_pgs = {v.get("portgroup") for h in inv.get("hosts", []) for v in h.get("vmknics", [])
                if "management" in v.get("services", [])}
    seen: dict[tuple[str, Any], set[str]] = {}
    for h in inv.get("hosts", []):
        for vs in h.get("vswitches", []):
            for pg in vs.get("portgroups", []):
                seen.setdefault((pg["name"], pg.get("vlan")), set()).add(h["name"])
    rows = []
    for (name, vlan), hs in sorted(seen.items(), key=lambda kv: str(kv[0][0])):
        purpose = "Management (vmk)" if name in mgmt_pgs else "TBD"
        rows.append([f"{name} (VSS)", vlan, purpose, ", ".join(sorted(hs)), "TBD",
                     "Management" if name in mgmt_pgs else "TBD"])
    for d in inv.get("dvswitches", []):
        for pg in d.get("portgroups", []):
            if pg.get("uplink"):
                continue
            vlan = (pg.get("vlan_id") if pg.get("vlan_type") != "trunk"
                    else "trunk " + ",".join(f"{s}-{e}" for s, e in pg.get("trunk_ranges", [])))
            rows.append([f"{pg['name']} ({d['name']})", vlan, "TBD", ", ".join(d.get("hosts", [])), "TBD", "TBD"])
    return rows


def render_markdown(inv: dict[str, Any], a: dict[str, Any]) -> str:
    vc = inv.get("vcenter", {})
    out: list[str] = []
    w = out.append
    w("# vSphere Inventory — TrueNorth discovery\n")
    w(f"Collected {inv.get('collected_at', '?')} from `{vc.get('host', '?')}` by `scripts/vsphere-discover.py` "
      "(read-only). Raw data: `vsphere-inventory.json`. Spec: "
      "`truenorth-ai-vsphere-pack/truenorth-vsphere-discovery.md`.\n")

    # Recommendations first: this is what the operator needs.
    w("## Recommended settings\n")
    w("```bash")
    for k, v in a["env"].items():
        w(f"{k}={v}")
    w("```\n")
    s, sw, vt, vl = a["storage"], a["switch"], a["vtpm"], a["vlan"]
    w(f"- **Storage:** {s['verdict']} — {s.get('reason', '')}.")
    if s.get("per_host_datastore"):
        w("  Largest-free datastore per host: " + ", ".join(f"{h} → {d}" for h, d in s["per_host_datastore"].items()) + ".")
    w(f"- **Switch:** {sw['mode']} — {sw['verdict']}. DRS licensed: {'yes' if sw['has_drs'] else 'no/unknown'}.")
    for n in sw.get("notes", []):
        w(f"  - {n}")
    w(f"- **vTPM:** {vt['verdict']} — {vt['note']}.")
    if vl["source"] == "reserved":
        w(f"- **VLANs:** {len(vl['used'])} ID(s) in use ({', '.join(map(str, vl['used'])) or 'none'}); the "
          f"operator-reserved block `{vl['reserved']}` is free in vSphere, so it is the pool.")
    else:
        if vl["reserved"]:
            w(f"- **VLANs:** reserved block `{vl['reserved']}` clashes with in-use ID(s) "
              f"{', '.join(map(str, vl['reserved_clashes']))}; falling back to a free-block search.")
        w(f"- **VLANs:** {len(vl['used'])} ID(s) in use; suggested pool `{vl['suggested_pool'] or 'none free'}` "
          f"({VLAN_POOL_SIZE} contiguous IDs in {VLAN_POOL_MIN}–{VLAN_POOL_MAX}). The physical switch must trunk "
          "these to every host — vSphere cannot see that.")
    w(f"- **Readiness gate:** {'PASSED' if a['gate_passed'] else 'NOT PASSED'} "
      f"({sum(g['status'] == 'known' for g in a['gate'])}/{len(a['gate'])} known).\n")

    w("## 1. vCenter\n")
    app = inv.get("appliance", {})
    w(_table(["Field", "Value"], [
        ["Endpoint", vc.get("host")], ["Product", vc.get("full_name")], ["Version", vc.get("version")],
        ["Build", vc.get("build")], ["API version", vc.get("api_version")],
        ["Datacenters", ", ".join(d["name"] for d in inv.get("datacenters", []))],
        ["Appliance hostname", json.dumps(app.get("hostname")) if app.get("hostname") is not None else None],
        ["Appliance NTP", json.dumps(app.get("ntp")) if app.get("ntp") is not None else None],
        ["Appliance DNS", json.dumps(app.get("dns")) if app.get("dns") is not None else None],
        ["TLS certificate", json.dumps(app.get("tls_certificate")) if app.get("tls_certificate") is not None else None],
    ]))
    w("\n### Licences\n")
    w(_table(["Name", "Edition", "Used / Total", "Unit", "Expires", "Key …", "Features"],
             [[lic.get("name"), lic.get("edition_key"), f"{lic.get('used')} / {lic.get('total')}", lic.get("cost_unit"),
               lic.get("expires"), lic.get("key_suffix"), ", ".join(lic.get("features", [])[:12])]
              for lic in inv.get("licenses", [])]))
    if inv.get("license_assignments"):
        w("\n" + _table(["Entity", "Licence", "Edition"],
                        [[x.get("entity"), x.get("license_name"), x.get("edition_key")]
                         for x in inv["license_assignments"]]))
    w("\n### Clusters\n")
    w(_table(["Cluster", "HA", "DRS", "DRS behaviour", "Hosts"],
             [[c["name"], "on" if c.get("ha_enabled") else "off", "on" if c.get("drs_enabled") else "off",
               c.get("drs_behavior"), ", ".join(c.get("hosts", []))] for c in inv.get("clusters", [])]))
    w("\n### Resource pools\n")
    w(_table(["Pool", "Owner", "Parent"],
             [[p.get("name"), p.get("owner"), p.get("parent")] for p in inv.get("resource_pools", [])]))
    w("\n### VM folders\n")
    w("\n".join(f"- `{f}`" for f in inv.get("vm_folders", [])) or "- none")

    w("\n## 2. ESXi hosts\n")
    hosts = inv.get("hosts", [])
    w(_table(["Host", "CPU", "Cores", "RAM", "Free RAM", "NICs", "Storage", "ESXi"],
             [[h["name"], f"{h.get('sockets')}× {h.get('cpu_model')}", f"{h.get('cores')} ({h.get('threads')} threads)",
               _gb(h.get("memory_bytes")),
               _gb((h.get("memory_bytes") or 0) - (h.get("memory_used_bytes") or 0)) if h.get("memory_bytes") else None,
               ", ".join(f"{p['device']}@{p.get('speed_mb') or 'down'}" for p in h.get("pnics", [])),
               ", ".join(d["name"] for d in inv.get("datastores", []) if h["name"] in d.get("hosts", [])),
               f"{h.get('version')} ({h.get('build')})"] for h in hosts], "llrrrlll"))
    w("\n" + _table(["Host", "Mgmt IP", "Vendor / model", "Cluster", "State", "Maintenance", "VMs", "DNS", "NTP", "HBAs"],
                    [[h["name"], h.get("mgmt_ip"), f"{h.get('vendor')} {h.get('model')}", h.get("cluster"),
                      h.get("connection_state"), "yes" if h.get("maintenance") else "no", h.get("vm_count"),
                      ", ".join(h.get("dns", {}).get("servers", [])), ", ".join(h.get("ntp_servers", [])),
                      ", ".join(f"{b['device']} {b.get('model')}" for b in h.get("hbas", []))] for h in hosts]))
    w("\n### Physical NICs\n")
    w(_table(["Host", "NIC", "Speed (Mb)", "Driver", "MAC"],
             [[h["name"], p["device"], p.get("speed_mb") or "down", p.get("driver"), p.get("mac")]
              for h in hosts for p in h.get("pnics", [])]))
    w("\n### Standard vSwitches\n")
    w(_table(["Host", "vSwitch", "MTU", "Uplinks", "Port groups (VLAN)"],
             [[h["name"], vs["name"], vs.get("mtu"), ", ".join(vs.get("uplinks", [])),
               ", ".join(f"{p['name']} ({p.get('vlan')})" for p in vs.get("portgroups", []))]
              for h in hosts for vs in h.get("vswitches", [])]))
    w("\n### VMkernel adapters\n")
    w(_table(["Host", "vmk", "IP", "Netmask", "Port group", "MTU", "Services"],
             [[h["name"], v["device"], v.get("ip"), v.get("netmask"), v.get("portgroup") or v.get("dvs_portgroup_key"),
               v.get("mtu"), ", ".join(v.get("services", []))] for h in hosts for v in h.get("vmknics", [])]))

    w("\n## 3. Storage\n")
    w(_table(["Datastore", "Type", "Local", "Capacity", "Free", "Accessible", "Hosts", "Shared"],
             [[d["name"], d.get("type"), {True: "yes", False: "no"}.get(d.get("local"), "?"), _tb(d.get("capacity_bytes")),
               _tb(d.get("free_bytes")), "yes" if d.get("accessible") else "no", ", ".join(d.get("hosts", [])),
               "yes" if len(d.get("hosts", [])) > 1 else "no"] for d in inv.get("datastores", [])], "lllrrlll"))

    w("\n## 4. Networking\n")
    w(_table(["Network / Port Group", "VLAN", "Purpose", "Hosts", "Routed?", "Candidate Use"],
             _network_rows(inv), "lrllll"))
    w("\n### Distributed switches\n")
    w(_table(["Switch", "Version", "MTU", "Uplinks", "Hosts", "Port groups"],
             [[d["name"], d.get("version"), d.get("mtu"), ", ".join(d.get("uplinks", [])), ", ".join(d.get("hosts", [])),
               len([p for p in d.get("portgroups", []) if not p.get("uplink")])] for d in inv.get("dvswitches", [])]))
    w(f"\nVLAN IDs in use: {', '.join(map(str, vl['used'])) or 'none'}.")
    if vl["trunks"]:
        w("Trunk (pass-all) port groups: " + "; ".join(vl["trunks"]) + ".")

    w("\n## 5. ISO media\n")
    search = inv.get("iso_search", {})
    w(f"Searched: {', '.join(search.get('datastores_searched', [])) or 'none'}"
      + (f" (hint `{search['hint']}`)" if search.get("hint") else "") + ".")
    for ds, err in (search.get("errors") or {}).items():
        w(f"- search failed on `{ds}`: {err}")
    w("\n" + _table(["Datastore", "Path", "Size"],
                    [[i["datastore"], i.get("path"), _gb(i.get("size_bytes"))] for i in inv.get("isos", [])], "llr"))
    w("\n### ISO coverage against the catalogue\n")
    w(_table(["Image", "Kind", "Need", "Matched ISO"],
             [[f"{r['image']} — {r['label']}", r.get("kind"), r["need"],
               "<br>".join(r["files"]) or ("**MISSING**" if r["need"] != "optional" else "—")]
              for r in a["iso"]["rows"]]))
    w(f"\nMissing (initial/enabled): {', '.join(a['iso']['missing']) or 'none'}.")
    if a["iso"]["unmatched_files"]:
        w("Unrecognised ISO files: " + ", ".join(f"`{f}`" for f in a["iso"]["unmatched_files"]) + ".")

    w("\n## 6. Templates and Content Libraries\n")
    w(_table(["Template", "Guest OS", "Host"],
             [[t.get("name"), t.get("guest_os"), t.get("host")] for t in inv.get("templates", [])]))
    vms = inv.get("vms", {})
    w(f"\nVMs (non-template): {vms.get('total', '?')} ({vms.get('powered_on', '?')} powered on). "
      f"vCenter VM found on: {vms.get('vcenter_vm_host') or 'not identified'}.")
    cl = inv.get("content_libraries", {})
    w(f"\nContent Library API: **{cl.get('status', 'unavailable')}**" + (f" — {cl['error']}" if cl.get("error") else ""))
    if cl.get("libraries"):
        w("\n" + _table(["Library", "Type", "Items"],
                        [[lib.get("name"), lib.get("type"),
                          ", ".join(f"{i.get('name')} ({i.get('type')})" for i in lib.get("items", []))]
                         for lib in cl["libraries"]]))
    kp = inv.get("key_providers", {})
    w(f"\nKey providers: **{kp.get('status', 'unavailable')}** — "
      + (", ".join(f"{p.get('id')} ({p.get('type')}{', default' if p.get('default') else ''})"
                   for p in kp.get("providers", [])) or kp.get("error") or "none"))

    w("\n## 7. Capacity plan\n")
    c = a["capacity"]
    if not c.get("known"):
        w("Unknown — no usable hosts collected.")
    else:
        w(_table(["Measure", "Value"], [
            ["Usable hosts", c["hosts"]], ["Physical cores", c["physical_cores"]], ["Logical CPUs", c["logical_cpus"]],
            ["Total RAM", f"{c['ram_gb']:,.0f} GB"], ["Total storage", f"{c['storage_tb']} TB"],
            ["Free storage", f"{c['storage_free_tb']} TB"],
            ["Excluded from ranges", c.get("excluded_host") or "none"],
            ["Range hosts", ", ".join(c["range_hosts"])],
            ["Management VMs", f"{c['reserve']['vcpu']} vCPU / {c['reserve']['ram_gb']} GB RAM "
                               f"(+ VCSA on the excluded host); {c['reserve']['disk_tb']} TB disk"],
            ["ESXi overhead", f"{ESXI_OVERHEAD_GB} GB RAM per host"],
            ["Overcommit", c["overcommit"]],
            ["Left for ranges", f"{c['range_vcpu']} vCPU / {c['range_ram_gb']:,.0f} GB RAM / {c['range_storage_tb']} TB"],
        ]))
        if c.get("excluded_reason"):
            w(f"\n**Note:** {c['excluded_reason']}.")
        w("\n" + _table(["Range host", "Management VMs placed", "vCPU left", "RAM left"],
                        [[h["name"], ", ".join(h["mgmt_vms"]), h["vcpu"], f"{h['ram_gb']:,.0f} GB"]
                         for h in c["per_host"]], "llrr"))
        mode = s.get("placement") or "unknown"
        how = {"cluster": "pooled across the range hosts (shared storage)",
               "spread": "pooled across the range hosts: each VM lands on the host with most vCPU headroom, "
                         "on its local datastore, joined by vDS port groups",
               "per-range-host": "packed per host: every VM of a range on one host"}.get(mode, "unknown")
        w(f"\n**Concurrent Ranges** uses the `{mode}` placement — {how}. Limit shows whether CPU or RAM runs "
          "out first. *Per-host packed* is what fits if each range had to stay on one host (shown for "
          "reference). Storage-bound is against free space at the thin-provisioned ceiling, so it "
          "understates what instant clones allow.\n")
        w(_table(["Range Size", "VMs", "vCPU", "RAM", "Storage", "Concurrent Ranges", "Limit", "Pooled",
                  "Per-host packed", "1 host down", "Storage-bound"],
                 [[r["name"], r["vms"], r["vcpu"], f"{r['ram_gb']} GB" if r["ram_gb"] else None,
                   f"{r['disk_tb']} TB" if r["disk_tb"] else None, r.get("concurrent"), r.get("limit"), r["pooled"],
                   r["per_host"], r["one_host_down"], r["storage_bound"]] for r in c["ranges"]], "lrrrrrlrrrr"))

    if a.get("diff"):
        d = a["diff"]
        w(f"\n## Changes since baseline ({d.get('baseline_collected_at') or 'unknown date'})\n")
        rows = [[k, ", ".join(v["added"]), ", ".join(v["removed"])] for k, v in d.items()
                if isinstance(v, dict) and (v["added"] or v["removed"])]
        w(_table(["Area", "Added", "Removed"], rows))
        if d["changes"]:
            w("")
            w("\n".join(f"- {x}" for x in d["changes"]))

    w("\n## 8. Deployment readiness gate (spec §17)\n")
    for g in a["gate"]:
        box = "x" if g["status"] == "known" else " "
        w(f"- [{box}] {g['item']} — *{g['status']}*: {_cell(g['evidence'])}")
    w("")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only vSphere discovery for TrueNorth.")
    p.add_argument("--host", required=True, help="vCenter hostname or IP")
    p.add_argument("--user", required=True, help="e.g. administrator@vsphere.local (read-only role is enough)")
    p.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    p.add_argument("--out", default="docs/deployment", help="output directory (default: docs/deployment)")
    p.add_argument("--iso-path", default=None,
                   help='limit the ISO search, e.g. "[datastore1] ISO" (default: search every datastore)')
    p.add_argument("--range-vlans", default=None, metavar="LO-HI",
                   help="VLAN block reserved for TrueNorth ranges and already trunked, e.g. 100-199. Used as "
                        "VSPHERE_VLAN_POOL when no in-use VLAN falls inside it; otherwise a free block is searched")
    p.add_argument("--baseline", default=None, metavar="JSON",
                   help="previous vsphere-inventory.json; adds a 'changes since baseline' section")
    args = p.parse_args(argv)
    if args.range_vlans:
        try:
            parse_vlan_range(args.range_vlans)
        except ValueError as exc:
            p.error(str(exc))
    baseline = json.loads(Path(args.baseline).read_text()) if args.baseline else None

    password = os.environ.get("VSPHERE_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")

    from pyVim.connect import Disconnect, SmartConnect

    print(f"Connecting to {args.host} as {args.user} (read-only discovery)...", file=sys.stderr)
    si = SmartConnect(host=args.host, user=args.user, pwd=password, disableSslCertValidation=args.insecure)
    try:
        print("Querying REST API (content libraries, appliance)...", file=sys.stderr)
        rest = collect_rest(args.host, args.user, password, verify=not args.insecure)
        print("Collecting inventory (ISO search can take a few minutes)...", file=sys.stderr)
        inv = collect(si, rest, iso_path=args.iso_path)
    finally:
        Disconnect(si)
    del password
    inv["vcenter"]["host"] = args.host

    analysis = analyse(inv, range_vlans=args.range_vlans, baseline=baseline)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "vsphere-inventory.json").write_text(json.dumps({"inventory": inv, "analysis": analysis},
                                                           indent=2, default=str) + "\n")
    (out / "vsphere-inventory.md").write_text(render_markdown(inv, analysis))
    print(f"Wrote {out / 'vsphere-inventory.json'} and {out / 'vsphere-inventory.md'}", file=sys.stderr)
    for k, v in analysis["env"].items():
        print(f"{k}={v}")
    print(f"readiness gate: {'PASSED' if analysis['gate_passed'] else 'NOT PASSED'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
