"""TrueNorth Range - the pyVmomi half of building a range on vSphere.

Placement, folders, per-range port groups, NIC wiring and guest customization. The
Automation REST API has none of these (no host capacity, no port group creation, no
NIC reconfiguration, no guest customization), so they go through the vSphere Web
Services API. Everything here takes the managed objects it works on as arguments and
builds real ``vim`` data objects, so the unit tests exercise the same specs vCenter
would receive.
"""

from __future__ import annotations

import base64
import contextlib
import secrets
from dataclasses import dataclass, field

import yaml

try:
    from pyVmomi import vim
except ImportError:  # the provisioner reports the missing SDK when it is used
    vim = None  # type: ignore[assignment]

MIB = 1024 * 1024
GIB = 1024 * MIB
# Disks are thin-provisioned, so a fresh clone uses a fraction of its size. A
# datastore must have this share of the VMs' provisioned disk free to take them.
THIN_DISK_FRACTION = 0.25
# Router/firewall appliances: no guest customization, NIC 1 = WAN, NIC 2.. = LAN zones.
APPLIANCE_PREFIXES = ("pfsense", "opnsense", "vyos")
APPLIANCE_ROLES = frozenset({"firewall", "router", "gateway"})


class PlacementError(RuntimeError):
    """No host (or datastore) can take the VM or range."""


# --------------------------------------------------------------------------- #
# Placement
# --------------------------------------------------------------------------- #


@dataclass
class HostCapacity:
    """What a host can still take. ``datastores`` maps name -> [free bytes, ref]."""

    name: str
    ref: object
    free_mem_mb: int
    threads: int
    vcpus_allocated: int
    datastores: dict[str, list] = field(default_factory=dict)

    def vcpu_headroom(self, ratio: float) -> float:
        return self.threads * ratio - self.vcpus_allocated


def host_capacity(host) -> HostCapacity | None:
    """Capacity of a connected, powered-on host outside maintenance mode, else None."""
    rt = host.runtime
    if rt.connectionState != "connected" or rt.inMaintenanceMode or getattr(rt, "powerState", "poweredOn") != "poweredOn":
        return None
    hw = host.hardware
    used_mb = host.summary.quickStats.overallMemoryUsage or 0
    vcpus = sum(
        (vm.summary.config.numCpu or 0)
        for vm in host.vm
        if vm.runtime.powerState == "poweredOn" and not vm.summary.config.template
    )
    stores = {
        ds.summary.name: [ds.summary.freeSpace, ds]
        for ds in host.datastore
        if ds.summary.accessible and getattr(ds.summary, "maintenanceMode", None) != "inMaintenance"
    }
    return HostCapacity(
        name=host.name,
        ref=host,
        free_mem_mb=hw.memorySize // MIB - used_mb,
        threads=hw.cpuInfo.numCpuThreads,
        vcpus_allocated=vcpus,
        datastores=stores,
    )


def vm_needs(vm_def: dict) -> tuple[int, int, int]:
    """(vCPUs, memory MiB, bytes of datastore space) one VM needs to be placed."""
    cores = int(vm_def.get("cores") or 2)
    mem = int(vm_def.get("memory_mb") or vm_def.get("memory") or 4096)
    disk = int(int(vm_def.get("disk_gb") or 60) * GIB * THIN_DISK_FRACTION)
    return cores, mem, disk


def _best_datastore(host: HostCapacity, disk: int) -> str | None:
    fits = [(free, name) for name, (free, _) in host.datastores.items() if free >= disk]
    return max(fits)[1] if fits else None


def _why_not(host: HostCapacity, cores: int, mem: int, disk: int, ratio: float) -> str | None:
    if host.free_mem_mb < mem:
        return f"{host.name}: {host.free_mem_mb} MiB free, needs {mem}"
    if host.vcpu_headroom(ratio) < cores:
        return f"{host.name}: {host.vcpu_headroom(ratio):g} vCPU headroom, needs {cores}"
    if _best_datastore(host, disk) is None:
        return f"{host.name}: no datastore with {disk // GIB} GiB free"
    return None


def _take(host: HostCapacity, cores: int, mem: int, disk: int) -> tuple[HostCapacity, object]:
    name = _best_datastore(host, disk)
    host.free_mem_mb -= mem
    host.vcpus_allocated += cores
    host.datastores[name][0] -= disk
    return host, host.datastores[name][1]


def place(hosts: list[HostCapacity], vm_defs: list[dict], mode: str, ratio: float) -> list[tuple[HostCapacity, object]]:
    """Choose a (host, datastore) for every VM, in ``vm_defs`` order.

    ``spread``: each VM goes to the host with the most vCPU headroom (then free RAM)
    that fits it, and the tally moves on, so a range larger than one host spans hosts
    (the vDS port groups exist on every host). ``per-range-host``: the whole range on
    the one host with the most free RAM that fits all of it. A host fits when RAM, the
    vCPU overcommit cap (threads x ``ratio``, counting powered-on VMs) and one of its
    datastores (THIN_DISK_FRACTION of the disk) all have room.
    """
    if not hosts:
        raise PlacementError("no eligible host: all are excluded, disconnected or in maintenance mode")
    if mode == "per-range-host":
        cores = sum(vm_needs(v)[0] for v in vm_defs)
        mem = sum(vm_needs(v)[1] for v in vm_defs)
        disk = sum(vm_needs(v)[2] for v in vm_defs)
        reasons = [r for h in hosts if (r := _why_not(h, cores, mem, disk, ratio))]
        fitting = [h for h in hosts if not _why_not(h, cores, mem, disk, ratio)]
        if not fitting:
            raise PlacementError(f"no single host fits the range ({cores} vCPU, {mem} MiB): " + "; ".join(reasons))
        host = max(fitting, key=lambda h: h.free_mem_mb)
        ds = host.datastores[_best_datastore(host, disk)][1]
        _take(host, cores, mem, disk)
        return [(host, ds)] * len(vm_defs)
    out = []
    for vm_def in vm_defs:
        cores, mem, disk = vm_needs(vm_def)
        fitting = [h for h in hosts if not _why_not(h, cores, mem, disk, ratio)]
        if not fitting:
            reasons = "; ".join(r for h in hosts if (r := _why_not(h, cores, mem, disk, ratio)))
            raise PlacementError(f"no host fits VM {vm_def.get('name')!r}: {reasons}")
        best = max(fitting, key=lambda h: (h.vcpu_headroom(ratio), h.free_mem_mb))
        out.append(_take(best, cores, mem, disk))
    return out


# --------------------------------------------------------------------------- #
# Inventory: folders, port groups
# --------------------------------------------------------------------------- #


def _child(folder, name: str):
    return next((c for c in folder.childEntity if c.name == name), None)


def find_folder(dc, path: str):
    """The VM folder at ``path`` ("a/b/c") under the datacenter, or None."""
    folder = dc.vmFolder
    for part in [p for p in path.split("/") if p]:
        folder = _child(folder, part)
        if folder is None:
            return None
    return folder


def ensure_folder(dc, path: str):
    """The VM folder at ``path``, created level by level where missing."""
    folder = dc.vmFolder
    for part in [p for p in path.split("/") if p]:
        found = _child(folder, part)
        if found is None:
            try:
                found = folder.CreateFolder(part)
            except vim.fault.DuplicateName:  # created concurrently by another range's build
                found = _child(folder, part)
        folder = found
    return folder


def portgroup_name(range_id: str, physical_vlan: int) -> str:
    return f"tn-{range_id[:8]}-v{physical_vlan}"


def wants_promiscuous(network_name: str) -> bool:
    """Sensor/SPAN networks see all traffic on the segment; nothing else may."""
    name = (network_name or "").lower()
    return "monitor" in name or "span" in name


def _bool_policy(value: bool):
    return vim.BoolPolicy(inherited=False, value=value)


def dvs_portgroup_spec(name: str, vlan: int, promiscuous: bool):
    port = vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy(
        vlan=vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec(inherited=False, vlanId=vlan),
        securityPolicy=vim.dvs.VmwareDistributedVirtualSwitch.SecurityPolicy(
            inherited=False,
            allowPromiscuous=_bool_policy(promiscuous),
            forgedTransmits=_bool_policy(False),
            macChanges=_bool_policy(False),
        ),
    )
    return vim.dvs.DistributedVirtualPortgroup.ConfigSpec(
        name=name, type="earlyBinding", numPorts=16, autoExpand=True, defaultPortConfig=port
    )


def _pg_vlan(pg) -> int | None:
    vlan = getattr(getattr(pg.config.defaultPortConfig, "vlan", None), "vlanId", None)
    return vlan if isinstance(vlan, int) else None


def ensure_dvs_portgroup(dvs, name: str, vlan: int, promiscuous: bool, wait):
    """The range port group on the vDS, created if missing (a retried build reuses it).

    Refuses a VLAN some other port group already carries: the pool is meant to be
    TrueNorth's alone, and two port groups on one VLAN are one broadcast domain.
    """
    for pg in dvs.portgroup:
        if pg.name == name:
            return pg
        if _pg_vlan(pg) == vlan:
            raise RuntimeError(f"VLAN {vlan} is already used by port group {pg.name!r} on {dvs.name}")
    wait(dvs.CreateDVPortgroup_Task(dvs_portgroup_spec(name, vlan, promiscuous)))
    created = next((pg for pg in dvs.portgroup if pg.name == name), None)
    if created is None:
        raise RuntimeError(f"port group {name!r} was not found on {dvs.name} after creating it")
    return created


def remove_dvs_portgroup(dvs, name: str, wait) -> bool:
    """Destroy the port group; False if it was already gone."""
    pg = next((p for p in dvs.portgroup if p.name == name), None)
    if pg is None:
        return False
    wait(pg.Destroy_Task())
    return True


def vss_portgroup_spec(name: str, vlan: int, vswitch: str, promiscuous: bool):
    return vim.host.PortGroup.Specification(
        name=name,
        vlanId=vlan,
        vswitchName=vswitch,
        policy=vim.host.NetworkPolicy(
            security=vim.host.NetworkPolicy.SecurityPolicy(
                allowPromiscuous=promiscuous, forgedTransmits=False, macChanges=False
            )
        ),
    )


def ensure_vss_portgroup(host, name: str, vlan: int, vswitch: str, promiscuous: bool) -> None:
    with contextlib.suppress(vim.fault.AlreadyExists):  # a retried build: it is already there
        host.configManager.networkSystem.AddPortGroup(portgrp=vss_portgroup_spec(name, vlan, vswitch, promiscuous))


def remove_vss_portgroup(host, name: str) -> bool:
    try:
        host.configManager.networkSystem.RemovePortGroup(pgName=name)
    except vim.fault.NotFound:
        return False
    return True


def network_ref(net) -> dict:
    """What a NIC needs to attach to ``net``: a vDS port group or a standard network."""
    config = getattr(net, "config", None)
    dvs = getattr(config, "distributedVirtualSwitch", None)
    if dvs is not None:
        return {"kind": "dvs", "name": net.name, "id": net._moId, "key": net.key, "switch_uuid": dvs.uuid}
    return {"kind": "vss", "name": net.name, "id": net._moId}


# --------------------------------------------------------------------------- #
# NICs
# --------------------------------------------------------------------------- #


def _backing(ref: dict):
    if ref["kind"] == "dvs":
        return vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo(
            port=vim.dvs.PortConnection(switchUuid=ref["switch_uuid"], portgroupKey=ref["key"])
        )
    return vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(deviceName=ref["name"])


def nic_cards(devices) -> list:
    """The VM's network cards in the guest's NIC order: by unit number (ethernetN is unit
    N+7), then device key. Not by key alone: a server may give an added card a key below
    the template's (govmomi's vcsim gives 205 after 4000), which put the training address
    on the noise management NIC when MACs were matched in key order."""
    def order(card):
        unit = getattr(card, "unitNumber", None)
        return (unit if isinstance(unit, int) else 1 << 30, card.key)

    return sorted((d for d in devices if isinstance(d, vim.vm.device.VirtualEthernetCard)), key=order)


def nic_device_changes(devices, refs: list[dict]) -> list:
    """Device specs that leave the VM with exactly one NIC per ref, in order.

    Existing cards (from the template) are re-pointed, missing ones are added as
    vmxnet3, and surplus template cards are removed, so no NIC stays on whatever
    network the template was built on.
    """
    cards = nic_cards(devices)
    changes = []
    for i, ref in enumerate(refs):
        connect = vim.vm.device.VirtualDevice.ConnectInfo(startConnected=True, allowGuestControl=True, connected=True)
        if i < len(cards):
            card = cards[i]
            card.backing = _backing(ref)
            card.connectable = connect
            op = vim.vm.device.VirtualDeviceSpec.Operation.edit
        else:
            card = vim.vm.device.VirtualVmxnet3(
                key=-(i + 1), backing=_backing(ref), connectable=connect, addressType="generated"
            )
            op = vim.vm.device.VirtualDeviceSpec.Operation.add
        changes.append(vim.vm.device.VirtualDeviceSpec(operation=op, device=card))
    for card in cards[len(refs):]:
        changes.append(vim.vm.device.VirtualDeviceSpec(operation=vim.vm.device.VirtualDeviceSpec.Operation.remove,
                                                       device=card))
    return changes


def disk_grow_changes(devices, disk_gb: int) -> list:
    """Grow the first virtual disk to ``disk_gb`` (never shrink); empty when it is big enough."""
    disk = next((d for d in devices if isinstance(d, vim.vm.device.VirtualDisk)), None)
    want_kb = int(disk_gb or 0) * 1024 * 1024
    if disk is None or not want_kb or int(disk.capacityInKB or 0) >= want_kb:
        return []
    disk.capacityInKB = want_kb
    if getattr(disk, "capacityInBytes", None):
        disk.capacityInBytes = want_kb * 1024
    return [vim.vm.device.VirtualDeviceSpec(operation=vim.vm.device.VirtualDeviceSpec.Operation.edit, device=disk)]


def hardware_spec(vm_def: dict, devices, refs: list[dict]) -> object:
    """CPU, memory and NICs in one ConfigSpec (for a clone or a reconfigure). A VM with
    Windows Server roles also gets its system disk grown to the roles' floor (render.py);
    the role install then extends C: into it (vsphere_roles.py)."""
    cores, mem, _ = vm_needs(vm_def)
    changes = nic_device_changes(devices, refs)
    if vm_def.get("roles"):
        changes += disk_grow_changes(devices, vm_def.get("disk_gb") or 0)
    return vim.vm.ConfigSpec(numCPUs=cores, memoryMB=mem, deviceChange=changes)


# --------------------------------------------------------------------------- #
# Guest customization
# --------------------------------------------------------------------------- #


def os_family(vm_def: dict) -> str:
    """``windows``, ``appliance`` (router/firewall: left alone) or ``linux``."""
    os_name = str(vm_def.get("os") or vm_def.get("template_name") or "").lower()
    if "win" in os_name:
        return "windows"
    if os_name.startswith(APPLIANCE_PREFIXES) or str(vm_def.get("role", "")).lower() in APPLIANCE_ROLES:
        return "appliance"
    return "linux"


FIREWALL_PREFIXES = ("pfsense", "opnsense")


def pick_edge(vm_defs: list[dict]) -> dict | None:
    """The range's edge appliance, which takes the WAN uplink: a node flagged ``edge``
    (or ``wan``) in the topology, else the first firewall (role ``firewall`` or a
    pfSense/OPNsense image), else the first other router appliance, else None."""
    appliances = [v for v in vm_defs if os_family(v) == "appliance"]
    flagged = [v for v in appliances if v.get("edge") or v.get("wan")]
    if flagged:
        return flagged[0]

    def is_firewall(v: dict) -> bool:
        os_name = str(v.get("os") or v.get("template_name") or "").lower()
        return str(v.get("role", "")).lower() == "firewall" or os_name.startswith(FIREWALL_PREFIXES)

    return next((v for v in appliances if is_firewall(v)), appliances[0] if appliances else None)


def hostname(vm_def: dict, limit: int = 63) -> str:
    raw = str(vm_def.get("node_id") or vm_def.get("name") or "vm")
    clean = "".join(c if c.isalnum() or c == "-" else "-" for c in raw).strip("-")
    return (clean or "vm")[:limit].rstrip("-")


def netplan(nics: list[dict], macs: list[str], dns: list[str] | None = None,
            search: list[str] | None = None) -> dict:
    """Netplan v2 for cloud-init: static addresses per NIC, matched by MAC address.

    A NIC with a gateway resolves through ``dns`` (the range's DNS servers, when the
    renderer knows them: background-noise agents) or else its gateway. A NIC without a
    gateway (the noise management NIC) gets its address and nothing else."""
    ethernets: dict = {}
    default_set = False
    for i, (nic, mac) in enumerate(zip(nics, macs, strict=False)):
        eth: dict = {"match": {"macaddress": mac.lower()}}
        if nic.get("ip"):
            eth["addresses"] = [f"{nic['ip']}/{nic.get('prefix', 24)}"]
            if nic.get("gateway"):
                eth["nameservers"] = {"addresses": list(dns or [nic["gateway"]])}
                if search:
                    eth["nameservers"]["search"] = list(search)
                if not default_set:  # one default route, through the first NIC that has one
                    eth["routes"] = [{"to": "default", "via": nic["gateway"]}]
                    default_set = True
        else:
            eth["dhcp4"] = True
        ethernets[f"nic{i}"] = eth
    return {"version": 2, "ethernets": ethernets}


def linux_guestinfo(vm_def: dict, macs: list[str], install_user: dict | None = None) -> list:
    """extraConfig for cloud-init's VMware datasource: hostname and static IPs.

    ``install_user`` (a cloud-config ``users`` entry, vsphere_guest.cloud_init_user) adds
    the ephemeral deploy-time install account next to the image's default user. The
    provisioner rewrites the userdata without it once the install is over.
    """
    name = hostname(vm_def)
    metadata = {"instance-id": vm_def["name"], "local-hostname": name, "network": netplan(vm_def["nics"], macs, vm_def.get("dns"),
                                                                     vm_def.get("dns_search"))}
    userdata = f"#cloud-config\nhostname: {name}\npreserve_hostname: false\n"
    # ``cloud_config``: more cloud-config from the worker for this VM (gs-core: its service
    # account, bootstrap script and first-boot command; worker/greyspace_host.py).
    extra = dict(vm_def.get("cloud_config") or {})
    users = [u for u in [install_user, *extra.pop("users", [])] if u]
    if users:
        userdata += yaml.safe_dump({"users": ["default", *users]}, sort_keys=False)
    if extra:
        userdata += yaml.safe_dump(extra, sort_keys=False)

    def b64(text: str) -> str:
        return base64.b64encode(text.encode()).decode()

    return [
        vim.option.OptionValue(key="guestinfo.metadata", value=b64(yaml.safe_dump(metadata, sort_keys=False))),
        vim.option.OptionValue(key="guestinfo.metadata.encoding", value="base64"),
        vim.option.OptionValue(key="guestinfo.userdata", value=b64(userdata)),
        vim.option.OptionValue(key="guestinfo.userdata.encoding", value="base64"),
    ]


def windows_customization(vm_def: dict, password: str | None = None):
    """Sysprep spec: computer name, workgroup RANGE, a static IP per NIC.

    The local Administrator password is random per VM and is neither logged nor
    stored: there is no credential store for range VMs yet, so templates must carry
    their own range accounts. The provisioner passes ``password`` in (still random,
    still in memory only) when it installs software in the guest afterwards.
    """
    c = vim.vm.customization
    password = password or secrets.token_urlsafe(18) + "!9a"  # meets Windows complexity rules
    identity = c.Sysprep(
        guiUnattended=c.GuiUnattended(
            autoLogon=False, autoLogonCount=0, timeZone=85, password=c.Password(plainText=True, value=password)
        ),
        userData=c.UserData(
            computerName=c.FixedName(name=hostname(vm_def, 15)), fullName="TrueNorth", orgName="TrueNorth",
            productId="",
        ),
        identification=c.Identification(joinWorkgroup="RANGE"),
    )
    adapters = []
    for nic in vm_def["nics"]:
        if nic.get("ip"):
            ip = c.IPSettings(ip=c.FixedIp(ipAddress=nic["ip"]), subnetMask=nic.get("netmask", "255.255.255.0"))
            if nic.get("gateway"):
                ip.gateway = [nic["gateway"]]
                ip.dnsServerList = [nic["gateway"]]
        else:
            ip = c.IPSettings(ip=c.DhcpIpGenerator())
        adapters.append(c.AdapterMapping(adapter=ip))
    dns = [n["gateway"] for n in vm_def["nics"] if n.get("gateway")][:1]
    return c.Specification(
        identity=identity, globalIPSettings=c.GlobalIPSettings(dnsServerList=dns), nicSettingMap=adapters
    )


# --------------------------------------------------------------------------- #
# Port mirroring (the templates' ``action: mirror`` rules)
# --------------------------------------------------------------------------- #
#
# A mirror rule ("copy zone X's traffic to the sensors on zone Y") becomes two vDS
# port-mirroring sessions per destination zone Y:
#
#   tn-<range8>-<Y>     mixedDestMirror ("Distributed Port Mirroring (legacy)"). Sources:
#                       the dvPorts of every range NIC on the source zones. Destinations:
#                       the sensor NICs' dvPorts on Y AND one vDS uplink, the copy for the
#                       uplink tagged with a per-range RSPAN VLAN.
#   tn-<range8>-<Y>-rx  remoteMirrorDest ("Remote Mirroring Destination"). Source: frames
#                       arriving on the RSPAN VLAN. Destinations: the same sensor dvPorts.
#
# Why not a single dvPortMirror session: dvPortMirror and mixedDestMirror only deliver to
# destination ports on the same host as the source port, so with spread placement (or
# after any vMotion) a sensor sees only the VMs that happen to share its host. The
# uplink copy carries every other host's traffic across the physical network on the
# RSPAN VLAN, and the -rx session hands it to the sensor on whichever host it runs. A
# sensor's own host is covered by the local destination (the physical switch does not
# send a frame back out the port it came in on, so nothing arrives twice). ERSPAN
# (encapsulatedRemoteMirrorSource) needs the sensor reachable by IP from the hosts'
# vmkernel network, which an isolated range VLAN is not.
#
# The RSPAN VLAN comes from the range VLAN pool (already trunked to every host), one per
# destination zone so two TAPs never share a VLAN. The physical switch must not learn
# MAC addresses on it (Cisco: ``remote-span`` VLAN), or it stops flooding the copies
# once it has learnt a MAC from them; see vmware-site-runbook.md §7.

# Logical keys for RSPAN VLANs in a range's VLAN reservation (networks[].vlan_id):
# never a template VLAN id, which is a real 802.1Q id (1-4094).
RSPAN_LOGICAL_BASE = 100000
MIRROR_LOCAL_TYPE = "mixedDestMirror"
MIRROR_REMOTE_DEST_TYPE = "remoteMirrorDest"


def _zone_label(zone: str) -> str:
    clean = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(zone)).strip("-")
    return clean or "zone"


def mirror_session_name(range_id: str, zone: str) -> str:
    return f"tn-{range_id[:8]}-{_zone_label(zone)}"


def mirror_session_prefix(range_id: str) -> str:
    """Every session a range owns starts with this (destroy removes them by it)."""
    return f"tn-{range_id[:8]}-"


def mirror_rules(template: dict) -> list[dict]:
    """The template's ``network.firewall_rules`` entries with ``action: mirror``."""
    net = template.get("network") if isinstance(template.get("network"), dict) else {}
    return [r for r in (net.get("firewall_rules") or [])
            if isinstance(r, dict) and str(r.get("action") or "").lower() == "mirror"]


def plan_mirrors(rules: list[dict], networks: list[dict]) -> tuple[list[dict], list[str]]:
    """Mirror rules grouped by destination zone, against the rendered networks.

    Returns ([{dst, dst_vlan, sources: [(zone, logical vlan)], rspan_key, rules}], notes).
    ``src: "*"`` (or ``any``) means every zone except the mirror destinations themselves.
    A rule naming a zone the template does not define is skipped with a note.
    """
    by_name = {str(n.get("name")): int(n["vlan_id"]) for n in networks
               if n.get("vlan_id") is not None and int(n["vlan_id"]) < RSPAN_LOGICAL_BASE}
    notes: list[str] = []
    dests = {str(r.get("dst")) for r in rules if str(r.get("dst")) in by_name}
    plans: dict[str, dict] = {}
    for n, rule in enumerate(rules):
        name = str(rule.get("name") or f"mirror{n + 1}")
        src, dst = str(rule.get("src") or "*"), str(rule.get("dst") or "")
        if dst not in by_name:
            notes.append(f"mirror rule {name!r}: unknown destination zone {dst!r} (skipped)")
            continue
        if src in ("*", "any"):
            sources = [z for z in by_name if z not in dests]
        elif src in by_name and src != dst:
            sources = [src]
        else:
            notes.append(f"mirror rule {name!r}: unknown source zone {src!r} (skipped)")
            continue
        plan = plans.setdefault(dst, {"dst": dst, "dst_vlan": by_name[dst], "sources": [],
                                      "rspan_key": RSPAN_LOGICAL_BASE + by_name[dst], "rules": []})
        plan["rules"].append(name)
        for zone in sources:
            if (zone, by_name[zone]) not in plan["sources"]:
                plan["sources"].append((zone, by_name[zone]))
    return list(plans.values()), notes


def vspan_sessions(range_id: str, plan: dict, src_ports: list[str], dst_ports: list[str],
                   uplink: str | None, rspan_vlan: int, direction: str = "received") -> list:
    """The two sessions (see above) for one destination zone, as vim VspanSession objects.

    ``direction``: which side of each source dvPort to copy. ``received`` (default) is
    what the port receives from its VM, so every frame on a zone is copied once, from
    the port that sent it; ``transmitted`` is what the port delivers to its VM;
    ``both`` copies each unicast frame twice (sender and receiver are both sources).
    Without ``uplink`` only the local session is made: sensors see their own host only.
    """
    v = vim.dvs.VmwareDistributedVirtualSwitch
    src = v.VspanPorts(portKey=list(src_ports))
    name = mirror_session_name(range_id, plan["dst"])
    note = f"TrueNorth range {range_id[:8]}: {', '.join(z for z, _ in plan['sources'])} -> {plan['dst']}"
    local = v.VspanSession(
        name=name, description=note, enabled=True, sessionType=MIRROR_LOCAL_TYPE,
        sourcePortReceived=src if direction in ("received", "both") else None,
        sourcePortTransmitted=src if direction in ("transmitted", "both") else None,
        destinationPort=v.VspanPorts(portKey=list(dst_ports), uplinkPortName=[uplink] if uplink else []),
        # The uplink copy carries the RSPAN tag only (no double tag); sensors get it untagged.
        encapsulationVlanId=rspan_vlan if uplink else None, stripOriginalVlan=True,
        # A sensor's mirror NIC may also be its management NIC (soc-training).
        normalTrafficAllowed=True,
    )
    if not uplink:
        return [local]
    remote = v.VspanSession(
        name=f"{name}-rx", description=note + " (from other hosts)", enabled=True,
        sessionType=MIRROR_REMOTE_DEST_TYPE,
        sourcePortReceived=v.VspanPorts(vlans=[rspan_vlan]),
        destinationPort=v.VspanPorts(portKey=list(dst_ports)),
        stripOriginalVlan=True, normalTrafficAllowed=True,
    )
    return [local, remote]


def _vspan_existing(dvs) -> dict:
    return {s.name: s for s in (getattr(getattr(dvs, "config", None), "vspanSession", None) or [])}


def _reconfigure_vspan(dvs, ops: list, wait) -> None:
    v = vim.dvs.VmwareDistributedVirtualSwitch
    spec = v.ConfigSpec(configVersion=dvs.config.configVersion, vspanConfigSpec=ops)
    wait(dvs.ReconfigureDvs_Task(spec))


def ensure_vspan_sessions(dvs, sessions: list, wait) -> None:
    """Create the sessions on the vDS in one reconfigure; one that exists (a retried
    build) is replaced in place (``edit`` with its key)."""
    v = vim.dvs.VmwareDistributedVirtualSwitch
    existing = _vspan_existing(dvs)
    ops = []
    for session in sessions:
        found = existing.get(session.name)
        if found is not None:
            session.key = found.key
        ops.append(v.VspanConfigSpec(vspanSession=session, operation="edit" if found is not None else "add"))
    if ops:
        _reconfigure_vspan(dvs, ops, wait)


def remove_vspan_sessions(dvs, prefix: str, wait) -> int:
    """Remove every session whose name starts with ``prefix``; 0 when there are none."""
    v = vim.dvs.VmwareDistributedVirtualSwitch
    doomed = [s for name, s in _vspan_existing(dvs).items() if name.startswith(prefix)]
    if not doomed:
        return 0
    ops = [v.VspanConfigSpec(vspanSession=v.VspanSession(key=s.key, name=s.name), operation="remove")
           for s in doomed]
    _reconfigure_vspan(dvs, ops, wait)
    return len(doomed)


def nic_port_keys(devices) -> list[tuple[str | None, str | None]]:
    """(port group key, dvPort key) of each NIC in guest NIC order; (None, None) off a vDS.

    On an earlyBinding port group vCenter picks the port when the NIC is connected and
    writes its key into the backing; that key is what a mirror session names.
    """
    out = []
    for card in nic_cards(devices):
        port = getattr(card.backing, "port", None)
        out.append((getattr(port, "portgroupKey", None), getattr(port, "portKey", None)))
    return out
