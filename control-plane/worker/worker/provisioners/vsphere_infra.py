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
    """The VM's network cards in device-key order, which is the guest's NIC order."""
    return sorted((d for d in devices if isinstance(d, vim.vm.device.VirtualEthernetCard)), key=lambda d: d.key)


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


def hardware_spec(vm_def: dict, devices, refs: list[dict]) -> object:
    """CPU, memory and NICs in one ConfigSpec (for a clone or a reconfigure)."""
    cores, mem, _ = vm_needs(vm_def)
    return vim.vm.ConfigSpec(numCPUs=cores, memoryMB=mem, deviceChange=nic_device_changes(devices, refs))


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


def netplan(nics: list[dict], macs: list[str]) -> dict:
    """Netplan v2 for cloud-init: static addresses per NIC, matched by MAC address."""
    ethernets: dict = {}
    default_set = False
    for i, (nic, mac) in enumerate(zip(nics, macs, strict=False)):
        eth: dict = {"match": {"macaddress": mac.lower()}}
        if nic.get("ip"):
            eth["addresses"] = [f"{nic['ip']}/{nic.get('prefix', 24)}"]
            if nic.get("gateway"):
                eth["nameservers"] = {"addresses": [nic["gateway"]]}
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
    metadata = {"instance-id": vm_def["name"], "local-hostname": name, "network": netplan(vm_def["nics"], macs)}
    userdata = f"#cloud-config\nhostname: {name}\npreserve_hostname: false\n"
    if install_user:
        userdata += yaml.safe_dump({"users": ["default", install_user]}, sort_keys=False)

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
