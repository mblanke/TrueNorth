"""TrueNorth Range - VMware vSphere provisioner.

Builds an isolated range: per-range port groups on physical VLANs from a pool
reserved for ranges, a VM folder per range, host/datastore placement, then one VM
per rendered node, either deployed from a Content Library OVF item (Automation
REST API, ``/api``, vSphere 7.0U2+) or cloned from an inventory VM template, wired
to its port groups and customized (cloud-init guestinfo for Linux, Sysprep for
Windows) before it is powered on.

The REST API has no host capacity, port group, NIC reconfiguration, guest
customization or snapshot operations, so those go through the vSphere Web Services
API with pyVmomi, VMware's own SDK (see vsphere_infra.py for the build pieces).

Range VMs only ever attach to the range's own ``tn-<range8>-v<vlan>`` port groups,
or, for a VM with no VLAN at all, to VSPHERE_NETWORK, or (the edge firewall's WAN NIC
only) to VSPHERE_RANGE_UPLINK_NETWORK. None may be a network named in
VSPHERE_MGMT_NETWORK: a range VM on the management network is a range that is not
isolated.

Software named in a node's ``services`` is installed after power-on through VMware
guest operations (vsphere_guest.py), from the depot behind the uplink. A Security Onion
VM is set up as a standalone node monitoring the range's mirror zone, offline from its
ISO (vsphere_securityonion.py).

Each pfSense or OPNsense VM gets a per-range config.xml (worker/pfsense_config.py: zone
gateways, WAN, NAT, the template's firewall rules, depot-only egress) in its guestinfo
before its first power-on; the template's boot script applies it. Each VyOS VM gets the
same policy as configuration commands (worker/vyos_config.py) in cloud-init user data.
Each of the three also gets a hash (never the password) of its range's own admin password
(worker/appliance_admin.py, issue #248), and its template-login check is an SSH attempt or
the appliance's acknowledgement (``_appliance_check_sync``).

A lab session's network is the exception to per-range port groups: its segment names a
pre-created, isolated ``port_group`` leased from LAB_PORT_GROUPS. Those NICs attach to
the leased port group (only one on that list), no VLAN is reserved for them, and a
destroy leaves the port group alone (the lease owns it).

REST dialect, detected at login: ``POST /api/session`` (vSphere 7.0U2+) is the normal
path. Where it is absent (404: older vCenters, and govmomi's vcsim) the provisioner logs
in through the legacy ``/rest/com/vmware/cis/session`` instead and then does every VM
operation (power, delete, Tools, guest IP) through the Web Services API, and clones
inventory templates only: the legacy ``/rest`` Content Library and VM calls are not
implemented here.

Every VM a build creates carries its full range id in its annotation (RANGE_TAG), so
``find_vms`` can find a range's VMs although their names hold only the first 8 hex
digits of it (``<range8>-<node>``).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from urllib.parse import urlparse

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

try:
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim, vmodl
except ImportError:  # only the snapshot operations need it
    Disconnect = SmartConnect = vim = vmodl = None  # type: ignore[assignment]

from .. import (
    appliance_admin,
    pfsense_config,
    range_inventory,
    software_catalogue,
    uplink_pool,
    vlan_pool,
    vyos_config,
)
from . import template_copies, vsphere_products, vsphere_readiness, vsphere_roles
from . import vsphere_credcheck as credcheck
from . import vsphere_customization as customization
from . import vsphere_guest as guest
from . import vsphere_infra as infra
from . import vsphere_inventory as inventory
from . import vsphere_macs as macs
from . import vsphere_securityonion as seconion
from . import vsphere_tags as tags
from . import vsphere_vlans as live_vlans
from .base import AllocationNeed, BaseProvisioner, ConnectionBindingError, LiveUsage
from .results import (
    DestroyResult,
    HealthResult,
    MetricsResult,
    ProvisionResult,
    RestoreResult,
    SnapshotDeleteResult,
    SnapshotResult,
    StartResult,
    StopResult,
    outcome,
)

logger = logging.getLogger(__name__)


class RangeNetworkConflictError(RuntimeError):
    """A range port group collided with the live switch just before it was created
    (``_prepare_sync``). ``conflict``: ProvisionResult.conflict."""

    def __init__(self, message: str, conflict: dict):
        super().__init__(message)
        self.conflict = conflict

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
VSPHERE_URL: str = os.environ.get("VSPHERE_URL", "https://vcenter.local")
VSPHERE_USERNAME: str = os.environ.get("VSPHERE_USERNAME", "administrator@vsphere.local")
VSPHERE_PASSWORD: str = os.environ.get("VSPHERE_PASSWORD", "")
VSPHERE_DATACENTER: str = os.environ.get("VSPHERE_DATACENTER", "")
VSPHERE_CLUSTER: str = os.environ.get("VSPHERE_CLUSTER", "")
VSPHERE_DATASTORE: str = os.environ.get("VSPHERE_DATASTORE", "")
# Only for a VM that has no VLAN at all (a hand-written single-NIC smoke test). Empty
# means such a VM fails. Never the management network: see VSPHERE_MGMT_NETWORK.
VSPHERE_NETWORK: str = os.environ.get("VSPHERE_NETWORK", "")
# Port group carrying the background-noise management VLAN. One per vCenter, tagged with
# the noise VLAN (template default 4001), reachable from the noise controller and nothing
# else. A Linux agent VM (a rendered VM with ``mgmt``) gets one more VMXNET3 NIC on it,
# with its reserved address and no route.
VSPHERE_NOISE_NETWORK: str = os.environ.get("VSPHERE_NOISE_NETWORK", "TN-Noise-Mgmt")
# Networks no range VM may ever attach to (comma-separated names).
VSPHERE_MGMT_NETWORK: str = os.environ.get("VSPHERE_MGMT_NETWORK", "dPG-TN-MGMT")
# Empty, or no item named like the template: clone the inventory VM template instead.
VSPHERE_CONTENT_LIBRARY: str = os.environ.get("VSPHERE_CONTENT_LIBRARY", "")
VSPHERE_VERIFY_SSL: bool = os.environ.get("VSPHERE_VERIFY_SSL", "false").lower() == "true"
VSPHERE_CONCURRENCY: int = int(os.environ.get("VSPHERE_CONCURRENCY", "4"))
VSPHERE_TOOLS_TIMEOUT: int = int(os.environ.get("VSPHERE_TOOLS_TIMEOUT", "120"))
# Readiness (vsphere_readiness.py): how long after power-on a Linux or Windows VM has for
# VMware Tools to report every planned static address (Sysprep reboots count against it).
# A VM that does not get there fails the range instead of it being reported ready.
# VSPHERE_GUEST_IP_CHECK=off only records what Tools saw (vcsim, where Tools never runs).
VSPHERE_GUEST_IP_TIMEOUT: int = int(os.environ.get("VSPHERE_GUEST_IP_TIMEOUT", "600"))
VSPHERE_GUEST_IP_CHECK: bool = os.environ.get("VSPHERE_GUEST_IP_CHECK", "require").lower() != "off"
# The slice of VMware's manual MAC range (00:50:56:00:00:00-00:50:56:3F:FF:FF) range NICs
# get their reserved, explicit MACs from (vsphere_macs.py). TrueNorth's alone.
VSPHERE_MAC_POOL: str = os.environ.get("VSPHERE_MAC_POOL", "")
# Per vCenter task (snapshot, clone, reconfigure). Keep it under Celery's visibility
# timeout (celery_app.py, 3600s).
VSPHERE_SNAPSHOT_TIMEOUT: int = int(os.environ.get("VSPHERE_SNAPSHOT_TIMEOUT", "1800"))
# Range networking: "vds" (port groups on VSPHERE_RANGE_DVS, present on every host) or
# "vss" (a standard port group on VSPHERE_RANGE_VSWITCH of each host that runs a VM).
VSPHERE_RANGE_SWITCH_MODE: str = os.environ.get("VSPHERE_RANGE_SWITCH_MODE", "vds").lower()
VSPHERE_RANGE_DVS: str = os.environ.get("VSPHERE_RANGE_DVS", "vDS-10G")
VSPHERE_RANGE_VSWITCH: str = os.environ.get("VSPHERE_RANGE_VSWITCH", "vSwitch1")
# Physical VLANs reserved for ranges, trunked to every host ("100-199" or "100-149,160").
VSPHERE_VLAN_POOL: str = os.environ.get("VSPHERE_VLAN_POOL", "100-199")
# VLANs never handed to a range even inside the pool (the site's own: management, build,
# depot uplink), installer tn_vsphere_reserved_vlans. Empty: none. Range certification S1.
VSPHERE_RESERVED_VLANS: str = os.environ.get("VSPHERE_RESERVED_VLANS", "")
# "spread" (each VM on the host with most vCPU headroom), "per-range-host" (whole range
# on one host) or "cluster" (cluster resource pool + VSPHERE_DATASTORE, DRS places).
VSPHERE_PLACEMENT: str = os.environ.get("VSPHERE_PLACEMENT", "spread").lower()
# A resource pool (by name, anywhere under the cluster) range VMs go into. Empty: the
# cluster's root pool.
VSPHERE_RESOURCE_POOL: str = os.environ.get("VSPHERE_RESOURCE_POOL", "")
# Range VM folders: <this>/<range-name>-<key> (vsphere_tags.range_label, key = 12 hex digits
# of the range id), found again by that suffix here and, as <id8>, under the earlier
# truenorth/ranges root. Never empty or "/": the provisioner refuses to start (range_root).
VSPHERE_RANGE_FOLDER: str = os.environ.get("VSPHERE_RANGE_FOLDER", "TrueNorth/Ranges")
# vCenter tags (tn-type, tn-os, tn-template, tn-range) on range VMs; best effort.
VSPHERE_TAGS: bool = os.environ.get("VSPHERE_TAGS", "true").lower() != "false"
# Per tagging call (seconds); the first slow or failed call stops tagging for the build.
VSPHERE_TAG_TIMEOUT: float = float(os.environ.get("VSPHERE_TAG_TIMEOUT", "5"))
# vCPU overcommit cap: a host takes VMs while powered-on vCPUs <= threads x this.
VSPHERE_MAX_VCPU_PER_THREAD: float = float(os.environ.get("VSPHERE_MAX_VCPU_PER_THREAD", "4"))
# Hosts never used for range VMs (comma-separated), e.g. the one running the VCSA.
VSPHERE_EXCLUDE_HOSTS: str = os.environ.get("VSPHERE_EXCLUDE_HOSTS", "")
# The edge firewall's WAN uplink (NIC 0) to the depot network. Empty: ranges are fully
# isolated (no WAN NIC, no deploy-time installs). Lab: dPG-TN-SVC, 10.30.32.0/24.
VSPHERE_RANGE_UPLINK_NETWORK: str = os.environ.get("VSPHERE_RANGE_UPLINK_NETWORK", "")
VSPHERE_RANGE_UPLINK_POOL: str = os.environ.get("VSPHERE_RANGE_UPLINK_POOL", "")
VSPHERE_RANGE_UPLINK_GATEWAY: str = os.environ.get("VSPHERE_RANGE_UPLINK_GATEWAY", "")
VSPHERE_RANGE_UPLINK_PREFIX: int = int(os.environ.get("VSPHERE_RANGE_UPLINK_PREFIX", "24"))
# The software depot (TN-DEPOT01) as range guests reach it through the uplink. Empty:
# no deploy-time installs. The feed and proxy default to Nexus on the depot.
TN_DEPOT_URL: str = os.environ.get("TN_DEPOT_URL", "").rstrip("/")
TN_DEPOT_CHOCO_FEED: str = os.environ.get("TN_DEPOT_CHOCO_FEED", "")
TN_DEPOT_APT_PROXY: str = os.environ.get("TN_DEPOT_APT_PROXY", "")
# TCP ports of the depot the range pfSense lets the zones reach through the WAN (Nexus,
# apt-cacher-ng); everything else outbound on the WAN is blocked.
TN_DEPOT_PORTS: str = os.environ.get("TN_DEPOT_PORTS", "8081,3142")
# Per VM, for all of its installs together.
TN_SOFTWARE_INSTALL_TIMEOUT: int = int(os.environ.get("TN_SOFTWARE_INSTALL_TIMEOUT", "1800"))
# Per Linux VM with range accounts (ADR 0012): how long to wait for cloud-init to have
# applied them before their hashes are scrubbed from the VM's guestinfo anyway.
VSPHERE_ACCOUNT_SCRUB_TIMEOUT: int = int(os.environ.get("VSPHERE_ACCOUNT_SCRUB_TIMEOUT", "300"))
# Per VM, for its Windows Server roles together (features, reboots, forest promotion).
VSPHERE_ROLE_TIMEOUT: int = int(os.environ.get("VSPHERE_ROLE_TIMEOUT", "1800"))
# No new work starts once a provision has run this long: Celery redelivers a task that is
# still unacknowledged after its visibility timeout (celery_app.py, 3600s), and a second
# copy of a range build is the one thing worse than a slow one.
VSPHERE_PROVISION_BUDGET: int = int(os.environ.get("VSPHERE_PROVISION_BUDGET", "3300"))
# How long the template-login check waits for an appliance's admin-password acknowledgement
# (appliance_admin.py, issue #248; a pfSense first boot applies its config and reboots once);
# never past the budget above.
RANGE_APPLIANCE_ACK_TIMEOUT: float = float(os.environ.get("RANGE_APPLIANCE_ACK_TIMEOUT", "600") or 600)
# Socket timeout (seconds) of the session the scheduled metrics read opens: connect and
# every SOAP reply. Keep it well under the metrics run's budget (worker.tasks).
VSPHERE_METRICS_TIMEOUT: int = int(os.environ.get("VSPHERE_METRICS_TIMEOUT", "15"))
# Port mirroring for the templates' ``action: mirror`` rules (vsphere_infra, "Port
# mirroring"). The vDS uplink that carries mirrored copies to the sensors' hosts on the
# RSPAN VLAN (empty: the vDS's first uplink), and which side of each source port to copy
# (received | transmitted | both); see vmware-site-runbook.md §7 to check it on site.
VSPHERE_MIRROR_UPLINK: str = os.environ.get("VSPHERE_MIRROR_UPLINK", "")
VSPHERE_MIRROR_DIRECTION: str = os.environ.get("VSPHERE_MIRROR_DIRECTION", "received").lower()

# What collect_metrics reads, for every VM of a range in one PropertyCollector call.
METRIC_PATHS = (
    "runtime.powerState",
    "guest.toolsRunningStatus",
    "summary.quickStats.overallCpuUsage",
    "summary.quickStats.guestMemoryUsage",
    "summary.quickStats.uptimeSeconds",
    "summary.runtime.maxCpuUsage",
    "summary.config.memorySizeMB",
)
# What the range-ownership check (_screen) reads before a delete, power or snapshot.
OWNER_PATHS = ("name", "config.annotation", "config.instanceUuid")


def warn_not_offline(vm_name: str, specs) -> list[str]:
    """Log a warning for each spec that is not offline-ready; returns their names.

    Not offline-ready means the catalogue does not say ``offline: true``: the Chocolatey
    package may download its vendor installer at install time, which fails in a range
    with no internet (content/choco internalizes such packages)."""
    names = [s.name for s in specs if not getattr(s, "offline", True)]
    for name in names:
        logger.warning("VM %s: installing %s, which is not offline-ready (its package may download the "
                       "vendor installer at install time and fail with no internet); internalize it in "
                       "content/choco, then set offline: true in the software catalogue", vm_name, name)
    return names


def _names(csv: str) -> set[str]:
    return {n.strip() for n in (csv or "").split(",") if n.strip()}


# A range VM's annotation carries this and its full range id (see find_vms).
RANGE_TAG = "truenorth-range:"
_RANGE_TAG_RE = re.compile(re.escape(RANGE_TAG) + r"\s*([0-9A-Za-z][0-9A-Za-z-]*)")
# Web Services power states as the REST API names them.
_SOAP_POWER = {"poweredOn": "POWERED_ON", "poweredOff": "POWERED_OFF", "suspended": "SUSPENDED"}


def endpoint_url(host: str, port=None) -> str:
    """``https://host[:port]`` for a connection row: REST and pyVmomi (``_connect``) both use it.

    No port for 443. Nor for 8006: until 2026-10-11 the API stored 8006 (Proxmox's port) for
    any connection created without one (app/hypervisor_ports.py), and no vCenter listens
    there, so on a vSphere row it means "not given". A port already in ``host`` wins. A bare
    IPv6 address is bracketed (``fd00::10`` -> ``https://[fd00::10]``). Raises ValueError for
    an address no URL can carry. The API's copy is app/hypervisor_backends/vsphere.base_url
    (tests/contracts keeps the two equal)."""
    url = str(host or "").strip()
    if "://" in url and not urlparse(url).hostname:
        raise ValueError(f"not a usable vCenter address: {url!r}")  # a scheme and nothing else
    url = url.rstrip("/")
    if "://" not in url:
        bare = url[1:-1] if url.startswith("[") and url.endswith("]") else url
        with contextlib.suppress(ValueError):
            if ipaddress.ip_address(bare).version == 6:
                url = f"[{bare}]"  # a bare IPv6 address
        url = f"https://{url}"
    parsed = urlparse(url)
    try:
        explicit = parsed.port
    except ValueError as exc:
        raise ValueError(f"not a usable vCenter address: {url!r}") from exc
    if not parsed.hostname:
        raise ValueError(f"not a usable vCenter address: {url!r}")
    try:
        port = int(port or 0)
    except (TypeError, ValueError):
        port = 0
    if port in (0, 443, 8006) or explicit is not None:
        return url
    return parsed._replace(netloc=f"{parsed.netloc}:{port}").geturl()


def endpoint_key(url: str) -> tuple[str, int]:
    """(host, port) of an endpoint URL, for comparing two spellings of one vCenter."""
    try:
        parsed = urlparse(url if "://" in str(url) else endpoint_url(url))
        return (parsed.hostname or "").lower(), parsed.port or 443
    except ValueError:
        return str(url), 0


def range_annotation(range_id: str) -> str:
    return f"TrueNorth range VM. Deleted with its range. {RANGE_TAG}{range_id}"


def annotated_range(annotation: str | None) -> str:
    """The range id a VM's annotation names, or ''."""
    m = _RANGE_TAG_RE.search(annotation or "")
    return m.group(1) if m else ""


def lab_port_groups() -> set[str]:
    """The pre-created, isolated port groups lab sessions may lease (LAB_PORT_GROUPS)."""
    return _names(os.environ.get("LAB_PORT_GROUPS", ""))


class TaskTimeoutError(TimeoutError):
    """A vCenter task did not finish within the wait's overall timeout."""


# Longest single WaitForUpdatesEx call. Each call returns at the latest after this many
# seconds, so the overall deadline is checked at least this often.
TASK_POLL_SECONDS = 30


def _wait_task(task, si, timeout: float, *, clock: Callable[[], float] = time.monotonic,
               poll_seconds: int = TASK_POLL_SECONDS) -> None:
    """Wait for a vCenter task to finish, for at most ``timeout`` seconds; raise its fault.

    On a property collector of its own: the session's collector hands each update to
    whichever WaitForUpdates call collects it first. Builds wait from several threads at
    once (one per VM), so on a shared collector one thread takes another's task
    completion and then waits for an update that never comes. Found against vcsim.

    And bounded: pyVim's WaitForTask checks ``maxWaitTime`` only between updates, so a
    task that never updates again held the worker forever. Here every WaitForUpdatesEx
    call carries ``maxWaitSeconds`` (it returns None when nothing changed), and the
    deadline is checked between calls. A task still running at the deadline raises
    TaskTimeoutError; it is left running in vCenter (cancelling a half-done clone or
    snapshot is not obviously safer than letting it finish).
    """
    deadline = clock() + timeout
    pc = si.content.propertyCollector.CreatePropertyCollector()
    try:
        pcq = vmodl.query.PropertyCollector
        pc.CreateFilter(pcq.FilterSpec(
            objectSet=[pcq.ObjectSpec(obj=task, skip=False)],
            propSet=[pcq.PropertySpec(type=vim.Task, all=False, pathSet=["info.state", "info.error"])],
        ), False)
        version, state, error = "", None, None
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                raise TaskTimeoutError(
                    f"vCenter task {getattr(task, '_moId', task)} still {state or 'queued'} after {timeout:g}s")
            wait = max(1, min(int(poll_seconds), int(remaining + 0.999)))
            update = pc.WaitForUpdatesEx(version, pcq.WaitOptions(maxWaitSeconds=wait))
            if update is None:
                continue
            version = update.version
            for fs in update.filterSet or []:
                for obj in fs.objectSet or []:
                    for change in obj.changeSet or []:
                        if change.name == "info.state":
                            state = change.val
                        elif change.name == "info.error":
                            error = change.val
            if state == "success":
                return
            if state == "error":
                if error is None:
                    error = task.info.error
                raise error if isinstance(error, BaseException) else RuntimeError(f"vCenter task failed: {error}")
    finally:
        with contextlib.suppress(Exception):
            pc.DestroyPropertyCollector()


def _named_snapshots(vm, name: str) -> list:
    """Every snapshot of ``vm`` called ``name``, oldest first, from the whole tree."""
    found = []
    stack = list(vm.snapshot.rootSnapshotList) if vm.snapshot else []
    while stack:
        node = stack.pop()
        if node.name == name:
            found.append(node)
        stack.extend(node.childSnapshotList or [])
    return sorted(found, key=lambda node: node.createTime)


def _read_vm_metrics(si, vm_ids: list[str], paths=METRIC_PATHS) -> tuple[dict[str, dict], set[str]]:
    """``paths`` (METRIC_PATHS) of every VM in ``vm_ids``, in one RetrievePropertiesEx call.

    Returns {vm_id: {path: value}} and the VMs vCenter does not have. One call per range,
    not one per VM per property: pyVmomi's attribute access (``vm.summary``) is a round
    trip each, and the whole summary is far more than these numbers. A VM deleted outside
    TrueNorth makes vCenter fail the whole call with ManagedObjectNotFound naming it; it
    is dropped and the call repeated, so the other VMs are still read.
    """
    pcq = vmodl.query.PropertyCollector
    pc = si.content.propertyCollector
    remaining, missing = list(vm_ids), set()
    while remaining:
        spec = pcq.FilterSpec(
            objectSet=[pcq.ObjectSpec(obj=vim.VirtualMachine(v, si._stub), skip=False) for v in remaining],
            propSet=[pcq.PropertySpec(type=vim.VirtualMachine, all=False, pathSet=list(paths))],
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
        # Not in the answer at all: gone too, as far as metrics go.
        return props, missing | {v for v in remaining if v not in props}
    return {}, missing


def _inventory_props(si, paths) -> dict[str, dict]:
    """``paths`` of every VM (templates included) in the vCenter, {vm id: {path: value}}, in
    one property-collector pass over a container view (paged by vCenter), not a round trip
    per VM per property."""
    pcq = vmodl.query.PropertyCollector
    content = si.content
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    try:
        spec = pcq.FilterSpec(
            objectSet=[pcq.ObjectSpec(obj=view, skip=True, selectSet=[pcq.TraversalSpec(
                name="vms", path="view", skip=False, type=vim.view.ContainerView)])],
            propSet=[pcq.PropertySpec(type=vim.VirtualMachine, all=False, pathSet=list(paths))],
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


def _not_ours(range_id: str, vm: dict, seen: dict, where: str) -> str | None:
    """None when what vCenter holds under ``vm``'s recorded id is this range's VM, else a note.

    The range-ownership rule (see VsphereAPIProvisioner._screen). A VM whose instance UUID
    was recorded at clone (range certification S5) is the range's exactly when that UUID is
    still the one under its moid, renamed or re-annotated or not: the record decides, never a
    mark. (Trust: ``config.instanceUuid`` can be set only with vCenter privileges to edit a
    VM's configuration, i.e. by someone who could delete the VM outright; the pairing with the
    recorded moid guards against TrueNorth's own mistakes, not a vCenter administrator.) Older records: its annotation names the range (RANGE_TAG), or, built before the
    tag (2026-10-07), it carries no tag and still has its recorded ``<range8>-<node>`` name.
    ``seen`` holds OWNER_PATHS."""
    name = str(seen.get("name") or "")
    owner = annotated_range(str(seen.get("config.annotation") or ""))
    recorded = (vm.get("identity") or {}).get("instance_uuid") if isinstance(vm.get("identity"), dict) else None
    if recorded:
        live = seen.get("config.instanceUuid")
        if range_inventory.vm_owned({"instance_uuid": recorded}, live):
            return None
        return (f"VM {vm.get('name') or vm.get('vm_id')}: {vm.get('vm_id')} on {where} is {name!r} with instance "
                f"UUID {live or 'unknown'}, not the recorded {recorded}")
    legacy = not owner and name == vm.get("name") and name.startswith(f"{range_id[:8]}-")
    if owner == range_id or legacy:
        return None
    whose = f"range {owner}" if owner else "no TrueNorth range"
    return f"VM {vm.get('name') or vm.get('vm_id')}: {vm.get('vm_id')} on {where} is {name!r} of {whose}, not this range's"


def _str_or_none(value) -> str | None:
    return None if value is None else str(value)


def _screened(vm: dict) -> dict:
    """What ``_screen`` needs of a recorded VM: its id, name and (S5) recorded identity, so
    every operation applies the same ownership rule (``_not_ours``)."""
    out = {"vm_id": vm.get("vm_id"), "name": vm.get("name", "")}
    if isinstance(vm.get("identity"), dict):
        out["identity"] = vm["identity"]
    return out


def _vm_metrics(vm: dict, props: dict | None) -> dict:
    """One MetricsResult VM entry from the properties read (None: vCenter has no such VM).

    pyVmomi turns an unset quickStats number into 0, so a running VM whose host sent no
    stats (vcsim always; vCenter for a host not reporting) reads CPU 0, memory 0, uptime 0.
    That is no reading, not an idle VM: those three are then None."""
    def val(path):
        v = (props or {}).get(path)
        return str(v) if isinstance(v, str) else v  # pyVmomi enums are str subclasses

    stats = ("summary.quickStats.overallCpuUsage", "summary.quickStats.guestMemoryUsage",
             "summary.quickStats.uptimeSeconds")
    blank = val("runtime.powerState") == "poweredOn" and not any(val(p) for p in stats)
    cpu, mem, up = (None, None, None) if blank else (val(p) for p in stats)
    return {
        "vm_id": vm.get("vm_id", ""),
        "name": vm.get("name", ""),
        "power_state": val("runtime.powerState") if props is not None else "notFound",
        "tools_status": val("guest.toolsRunningStatus"),
        "cpu_usage_mhz": cpu,
        "cpu_capacity_mhz": val("summary.runtime.maxCpuUsage"),
        "memory_active_mb": mem,
        "memory_configured_mb": val("summary.config.memorySizeMB"),
        "uptime_seconds": up,
    }


class VsphereAPIProvisioner(BaseProvisioner):
    """VMware vSphere provisioner (Automation REST API + pyVmomi).

    The REST session token is created on first use and renewed once on a 401 (vCenter
    sessions expire after 30 idle minutes, well inside a long build). Credentials come
    from the range's own tenant's vSphere HypervisorConnection (else a shared one with no
    tenant), which the worker passes to the constructor for every operation on a range
    (worker.base_tasks._get_backend -> worker/connection_binding.py). A build records
    which one (``connection_record``) and every later operation uses that one. With no
    such connection: the VSPHERE_* environment, whole; the two are never mixed.
    """

    def __init__(self, credentials: dict | None = None) -> None:
        self._base_url = VSPHERE_URL.rstrip("/")
        self._username = VSPHERE_USERNAME
        self._password = VSPHERE_PASSWORD
        self._datacenter = VSPHERE_DATACENTER
        self._cluster = VSPHERE_CLUSTER
        self._datastore = VSPHERE_DATASTORE
        self._network = VSPHERE_NETWORK
        self._noise_network = VSPHERE_NOISE_NETWORK.strip()
        self._mgmt_networks = _names(VSPHERE_MGMT_NETWORK)
        self._content_library = VSPHERE_CONTENT_LIBRARY
        self._verify_ssl = VSPHERE_VERIFY_SSL
        self._semaphore = asyncio.Semaphore(VSPHERE_CONCURRENCY)
        self._tools_timeout = VSPHERE_TOOLS_TIMEOUT
        self._guest_ip_timeout = VSPHERE_GUEST_IP_TIMEOUT
        self._guest_ip_check = VSPHERE_GUEST_IP_CHECK
        self._mac_pool = VSPHERE_MAC_POOL
        self._session_token: str | None = None
        self._login_loop = None
        self._login_gate: asyncio.Lock | None = None
        self._concurrency = max(1, VSPHERE_CONCURRENCY)
        self._snapshot_timeout = VSPHERE_SNAPSHOT_TIMEOUT
        self._switch_mode = VSPHERE_RANGE_SWITCH_MODE
        self._dvs_name = VSPHERE_RANGE_DVS
        self._vswitch = VSPHERE_RANGE_VSWITCH
        self._vlan_pool = VSPHERE_VLAN_POOL
        self._reserved_vlans = VSPHERE_RESERVED_VLANS
        self._placement = VSPHERE_PLACEMENT
        self._resource_pool = VSPHERE_RESOURCE_POOL.strip()
        self._range_folder = infra.range_root(VSPHERE_RANGE_FOLDER)  # empty or "/": refused, loudly
        self._tags_enabled = VSPHERE_TAGS
        self._tag_timeout = VSPHERE_TAG_TIMEOUT
        self._tagger: tags.TagClient | None = None  # one per build (provision)
        self._vcpu_ratio = VSPHERE_MAX_VCPU_PER_THREAD
        self._exclude_hosts = _names(VSPHERE_EXCLUDE_HOSTS)
        self._uplink_network = VSPHERE_RANGE_UPLINK_NETWORK.strip()
        self._uplink_pool = VSPHERE_RANGE_UPLINK_POOL
        self._uplink_gateway = VSPHERE_RANGE_UPLINK_GATEWAY.strip()
        self._uplink_prefix = VSPHERE_RANGE_UPLINK_PREFIX
        self._depot_url = TN_DEPOT_URL
        self._choco_feed = TN_DEPOT_CHOCO_FEED or (f"{TN_DEPOT_URL}:8081/repository/chocolatey/" if TN_DEPOT_URL
                                                   else "")
        self._apt_proxy = TN_DEPOT_APT_PROXY or TN_DEPOT_URL
        self._depot_ports = tuple(int(p) for p in TN_DEPOT_PORTS.replace(" ", "").split(",") if p)
        self._install_timeout = TN_SOFTWARE_INSTALL_TIMEOUT
        self._role_timeout = VSPHERE_ROLE_TIMEOUT
        self._scrub_timeout = VSPHERE_ACCOUNT_SCRUB_TIMEOUT
        self._budget = VSPHERE_PROVISION_BUDGET
        self._budget_end = 0.0  # absolute monotonic deadline for new build work (set in provision)
        self._appliance_ack_timeout = RANGE_APPLIANCE_ACK_TIMEOUT
        self._guest_poll = 5.0
        self._guest_put = None  # a guest file-transfer PUT for tests; None = real HTTPS to the ESXi host
        self._library_id: str | None = None
        self._library_looked_up = False
        self._transport = None  # an httpx transport for tests; None = the network
        self._metrics_timeout = VSPHERE_METRICS_TIMEOUT
        self._mirror_uplink = VSPHERE_MIRROR_UPLINK.strip()
        self._mirror_direction = VSPHERE_MIRROR_DIRECTION
        self._scope: dict | None = None  # set inside session(): the run's shared pyVmomi login
        # "api" (/api, vSphere 7.0U2+) or "rest" (legacy /rest session only: older vCenters,
        # vcsim). Known after the first login; see _authenticate.
        self._dialect = "api"
        # The pyVmomi session VM operations use in the "rest" dialect (_soap_scope, provision).
        self._soap_si = None
        self._connection_id: str | None = None  # the HypervisorConnection in use; None: the environment
        if credentials:
            self.use_credentials(credentials)

    def use_credentials(self, creds: dict) -> None:
        """Take endpoint and login from a HypervisorConnection row (worker.db_ops.hypervisor_creds).

        All or nothing: host, username and password come from the row alone. Until
        2026-10-11 a missing one fell back to VSPHERE_*, so a tenant's connection saved
        without a password sent the platform's VSPHERE_PASSWORD, as Basic auth, to the
        host the tenant chose. An incomplete row is refused instead (ConnectionBindingError).
        Only the datacenter name may still come from the environment."""
        host = str(creds.get("host") or "").strip()
        username, password = str(creds.get("username") or ""), str(creds.get("password") or "")
        missing = [name for name, value in (("host", host), ("username", username), ("password", password))
                   if not value]
        if missing:
            ref = creds.get("connection_id") or creds.get("name") or host or "?"
            raise ConnectionBindingError(
                f"vSphere connection {ref} has no {', '.join(missing)}: set it on the connection "
                "(Infrastructure > Hypervisors). The platform's own vCenter credentials are never used for it."
            )
        try:
            self._base_url = endpoint_url(host, creds.get("port"))
        except ValueError as exc:
            raise ConnectionBindingError(f"vSphere connection {creds.get('connection_id') or host}: {exc}") from exc
        self._username, self._password = username, password
        self._datacenter = creds.get("datacenter") or self._datacenter
        self._verify_ssl = bool(creds.get("verify_ssl", True))  # the row's own; secure when absent
        self._connection_id = str(creds["connection_id"]) if creds.get("connection_id") else None
        self._session_token = None

    def connection_record(self) -> dict:
        """The vCenter this instance logs in to: recorded on the range at build
        (worker/connection_binding.py), so destroy, power and snapshots go to the same one."""
        return {"id": self._connection_id, "host": self._base_url}

    # ------------------------------------------------------------------ #
    # HTTP helpers
    # ------------------------------------------------------------------ #

    def _client(self) -> httpx.AsyncClient:
        if httpx is None:
            raise RuntimeError("httpx is required for VsphereAPIProvisioner")
        return httpx.AsyncClient(
            base_url=self._base_url, verify=self._verify_ssl, timeout=60.0, transport=self._transport
        )

    async def _authenticate(self) -> str:
        """Create a vCenter session and return the session token; sets ``_dialect``.

        ``POST /api/session`` first. A 404 there means the server has no ``/api`` session
        endpoint at all (vCenter before 7.0U2, govmomi's vcsim), not bad credentials
        (401): log in through the legacy ``/rest/com/vmware/cis/session``, whose token is
        wrapped as ``{"value": token}``, and switch to the "rest" dialect.
        """
        if httpx is None:
            raise RuntimeError("httpx is required for VsphereAPIProvisioner")
        async with httpx.AsyncClient(
            base_url=self._base_url,
            verify=self._verify_ssl,
            timeout=30.0,
            transport=self._transport,
        ) as client:
            resp = await client.post(
                "/api/session",
                auth=(self._username, self._password),
            )
            if resp.status_code == 404:
                legacy = await client.post("/rest/com/vmware/cis/session", auth=(self._username, self._password))
                legacy.raise_for_status()
                body = legacy.json()
                token = body.get("value") if isinstance(body, dict) else None
                if not isinstance(token, str) or not token:
                    raise RuntimeError(f"vCenter legacy session endpoint returned no token: {body!r}")
                if self._dialect != "rest":
                    logger.info("vCenter at %s has no /api/session; using the legacy /rest session and "
                                "the Web Services API for VM operations", self._base_url)
                self._dialect = "rest"
                return token
            resp.raise_for_status()
            self._dialect = "api"
            # vCenter returns the token as a JSON string
            return resp.json()

    def _login_lock(self) -> asyncio.Lock:
        """One login at a time, per event loop (provision() and friends each run in a new one)."""
        loop = asyncio.get_running_loop()
        if self._login_loop is not loop:
            self._login_loop, self._login_gate = loop, asyncio.Lock()
        return self._login_gate

    async def _get_session(self) -> str:
        """Return a valid session token, creating one if needed.

        A build's VMs reach their first REST call together; without the lock each logged in
        on its own, leaving vCenter with one session per VM (sessions are capped per vCenter)."""
        if not self._session_token:
            async with self._login_lock():
                if not self._session_token:
                    self._session_token = await self._authenticate()
        return self._session_token

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, *, prefix: str = "/api", **kwargs):
        """One REST call; on a 401 (session expired) log in again and retry once."""
        for attempt in (1, 2):
            token = await self._get_session()
            resp = await client.request(method, f"{prefix}{path}", headers={"vmware-api-session-id": token}, **kwargs)
            if resp.status_code != 401 or attempt == 2:
                break
            logger.info("vCenter session expired; logging in again")
            self._session_token = None
        resp.raise_for_status()
        return resp.json() if resp.content else None

    async def _api_get(self, client: httpx.AsyncClient, path: str) -> dict | list:
        return await self._request(client, "GET", path)

    async def _api_post(self, client: httpx.AsyncClient, path: str, **kwargs) -> dict | list | str | None:
        return await self._request(client, "POST", path, **kwargs)

    async def _api_delete(self, client: httpx.AsyncClient, path: str) -> None:
        await self._request(client, "DELETE", path)

    def _tag_client(self, client: httpx.AsyncClient, deadline: float | None = None) -> tags.TagClient:
        """CIS tagging over this REST session, in its dialect (call after a login); each call
        bounded by VSPHERE_TAG_TIMEOUT, none after ``deadline`` (monotonic)."""
        async def call(method: str, path: str, body: dict | None):
            return await self._request(client, method, path, prefix="",
                                       **({"json": body} if body is not None else {}))

        return tags.TagClient(call, self._dialect, timeout=self._tag_timeout, deadline=deadline)

    async def _tag_vm(self, vm_def: dict, vm_id: str) -> None:
        """tn-type/tn-os/tn-template/tn-range on a range VM. Best effort: a VM is never
        failed (or deleted) because vCenter would not tag it."""
        if self._tagger is None or not vm_id:
            return
        label = vm_def.get("_label") or tags.range_key(vm_def["_range_id"])
        await self._tagger.apply(vm_id, tags.range_vm_tags(vm_def, label))  # bounded; never raises

    # ------------------------------------------------------------------ #
    # Content Library
    # ------------------------------------------------------------------ #

    async def _find_library_item(self, client: httpx.AsyncClient, template_name: str) -> str | None:
        """The library item named ``template_name``, or None (clone the inventory template)."""
        if not self._library_looked_up:
            self._library_looked_up = True
            await self._get_session()  # the dialect is known after the login
            if self._content_library and self._dialect == "rest":
                logger.warning("Content Library %r needs the /api of vSphere 7.0U2+, which %s does not serve; "
                               "cloning inventory templates", self._content_library, self._base_url)
            elif self._content_library:
                libs = await self._api_post(client, "/content/library?action=find", json={"name": self._content_library})
                self._library_id = libs[0] if libs else None
                if not libs:
                    logger.warning("Content Library %r not found; cloning inventory templates", self._content_library)
        if not self._library_id:
            return None
        items = await self._api_post(
            client, "/content/library/item?action=find", json={"name": template_name, "library_id": self._library_id}
        )
        return items[0] if items else None

    # ------------------------------------------------------------------ #
    # VM operations
    # ------------------------------------------------------------------ #

    async def _deploy_ovf(self, client: httpx.AsyncClient, library_item_id: str, vm_def: dict, site: dict) -> str:
        """Deploy a VM from a Content Library OVF item onto its placed host. Returns the VM id.

        Every network the OVF declares is mapped to the VM's first range port group,
        so no NIC comes up on the network the OVF was exported from; the reconfigure
        that follows sets each NIC explicitly.
        """
        name = vm_def["name"]
        host, datastore = vm_def["_placement"]
        target = {"resource_pool_id": site["pool"]._moId, "folder_id": site["folder"]._moId}
        if host is not None:
            target["host_id"] = host.ref._moId
        filtered = await self._api_post(
            client, f"/vcenter/ovf/library-item/{library_item_id}?action=filter", json={"target": target}
        )
        # The first range NIC: an edge firewall's NIC 0 is its WAN, and the OVF must not
        # come up on the uplink before the reconfigure.
        nic0 = next((ref for nic, ref in zip(vm_def["nics"], vm_def["_refs"], strict=True)
                     if not nic.get("uplink")), vm_def["_refs"][0])["id"]
        spec: dict = {
            "target": target,
            "deployment_spec": {
                "name": name,
                "accept_all_EULA": True,
                "default_datastore_id": datastore._moId,
                "storage_provisioning": "thin",
            },
        }
        networks = (filtered or {}).get("networks") or []
        if networks:
            spec["deployment_spec"]["network_mappings"] = dict.fromkeys(networks, nic0)
        logger.info("Deploying VM %r from library item %s", name, library_item_id)
        result = await self._api_post(client, f"/vcenter/ovf/library-item/{library_item_id}?action=deploy", json=spec)
        if not result.get("succeeded"):
            raise RuntimeError(f"OVF deploy failed for {name!r}: {result.get('error', {})}")
        return result["resource_id"]["id"]

    # VM operations go over REST in the "api" dialect and over the Web Services API in the
    # "rest" one (no /api: older vCenters, vcsim); see _authenticate.

    @contextlib.asynccontextmanager
    async def _soap_scope(self, *, always: bool = False):
        """In the "rest" dialect, one pyVmomi session for this call's VM operations; with
        ``always`` in either dialect (the range-ownership check, ``_screen``, reads VM
        annotations through it).

        Call it after a login (the dialect is known then). provision() sets ``_soap_si`` to
        its build session itself; an enclosing scope is reused, and so is a scheduled run's
        login (``session()``)."""
        if self._soap_si is not None or (self._dialect != "rest" and not always):
            yield
            return
        if SmartConnect is None:
            raise RuntimeError("pyvmomi is required for VM operations on a vCenter without /api")
        if self._scope is not None:  # inside session(): the run's one login, closed at its end
            self._soap_si = await asyncio.to_thread(self._scope_si, self._scope)
            try:
                yield
            finally:
                self._soap_si = None
            return
        si = await asyncio.to_thread(self._connect)
        self._soap_si = si
        try:
            yield
        finally:
            self._soap_si = None
            with contextlib.suppress(Exception):
                await asyncio.to_thread(Disconnect, si)

    def _soap(self):
        if self._soap_si is None:
            raise RuntimeError("no vCenter Web Services session for a VM operation (use _soap_scope)")
        return self._soap_si

    def _owners_sync(self, vm_ids: list[str]) -> tuple[dict[str, dict], set[str]]:
        return _read_vm_metrics(self._soap(), vm_ids, OWNER_PATHS)

    async def _screen(self, range_id: str, vms: list[dict]) -> tuple[list[dict], list[str], set[str]]:
        """The recorded VMs to act on, a note for each one left alone, and the recorded ids
        vCenter does not have. Inside ``_soap_scope``.

        A recorded ``vm-NN`` is a managed-object id, unique only within one vCenter, so a
        delete, power or snapshot by bare id on the wrong vCenter hits someone else's VM.
        A VM is acted on only when what vCenter has under that id is this range's: its
        annotation names the range (RANGE_TAG), or (built before the tag, 2026-10-07) it
        carries no tag and still has its recorded ``<range8>-<node>`` name. Anything else
        is skipped and logged. A VM vCenter no longer has, or one with no recorded id, is
        passed through: each operation already handles those (gone counts as deleted;
        power and snapshots report them).

        Finding one of the range's own VMs here proves the endpoint: a range with nothing
        recorded then gets it recorded (``on_connection_confirmed``)."""
        ids = [vm["vm_id"] for vm in vms if vm.get("vm_id")]
        if not ids:
            return list(vms), [], set()
        props, missing = await asyncio.to_thread(self._owners_sync, ids)
        keep: list[dict] = []
        notes: list[str] = []
        confirmed = False
        for vm in vms:
            vm_id = vm.get("vm_id")
            if not vm_id or vm_id in missing:
                keep.append(vm)
                continue
            note = _not_ours(range_id, vm, props.get(vm_id, {}), self._base_url)
            if note is None:
                keep.append(vm)
                confirmed = True
                continue
            notes.append(f"{note}; left alone")
        for note in notes:
            logger.warning("range %s: %s", range_id, note)
        if confirmed and self.on_connection_confirmed is not None:
            try:
                await asyncio.to_thread(self.on_connection_confirmed, range_id)
            except Exception as exc:  # noqa: BLE001 — the operation goes on; the next one records it
                logger.warning("range %s: could not record its vCenter %s: %s", range_id, self._base_url, exc)
        return keep, notes, missing

    async def _confirm(self, range_id: str, provision_output: dict) -> None:
        """For a range with nothing recorded, check its VMs are here (and so record it).
        Best effort: a scheduled read is not failed by it."""
        if self.on_connection_confirmed is None or provision_output.get("connection"):
            return
        try:
            async with self._soap_scope(always=True):
                await self._screen(range_id, provision_output.get("vms", []))
        except Exception as exc:  # noqa: BLE001
            logger.debug("range %s: ownership check skipped: %s", range_id, exc)

    def _soap_power_state_sync(self, vm_id: str) -> str:
        state = str(self._vm(self._soap(), vm_id).runtime.powerState)
        return _SOAP_POWER.get(state, state.upper() or "UNKNOWN")

    def _soap_power_sync(self, vm_id: str, action: str) -> None:
        si = self._soap()
        vm = self._vm(si, vm_id)
        state = str(vm.runtime.powerState)
        if action == "start":
            if state != "poweredOn":
                self._wait(vm.PowerOnVM_Task(), si)
        elif state != "poweredOff":  # a suspended VM powers off too
            self._wait(vm.PowerOffVM_Task(), si)

    def _soap_delete_sync(self, vm_id: str) -> None:
        si = self._soap()
        try:
            vm = self._vm(si, vm_id)
            if str(vm.runtime.powerState) != "poweredOff":
                self._wait(vm.PowerOffVM_Task(), si)
            self._wait(vm.Destroy_Task(), si)
        except vmodl.fault.ManagedObjectNotFound:
            return  # already gone counts as deleted: a retry is safe

    def _soap_tools_sync(self, vm_id: str) -> bool:
        return str(self._vm(self._soap(), vm_id).guest.toolsRunningStatus) == "guestToolsRunning"

    def _soap_ips_sync(self, vm_id: str) -> list[str]:
        guest_info = self._vm(self._soap(), vm_id).guest
        found: list[str] = []
        for nic in getattr(guest_info, "net", None) or []:
            for addr in getattr(getattr(nic, "ipConfig", None), "ipAddress", None) or []:
                ip = str(addr.ipAddress)
                if ":" not in ip and str(getattr(addr, "state", "preferred")) == "preferred":
                    found.append(ip)
        ip = getattr(guest_info, "ipAddress", None)
        if ip and ":" not in str(ip) and str(ip) not in found:
            found.append(str(ip))
        return found

    async def _power_state(self, client: httpx.AsyncClient, vm_id: str) -> str:
        """POWERED_ON, POWERED_OFF or SUSPENDED (REST names, whichever API answered)."""
        if self._dialect == "rest":
            return await asyncio.to_thread(self._soap_power_state_sync, vm_id)
        power = await self._api_get(client, f"/vcenter/vm/{vm_id}/power")
        return str((power or {}).get("state", "UNKNOWN")) if isinstance(power, dict) else "UNKNOWN"

    async def _power_action(self, client: httpx.AsyncClient, vm_id: str, action: str) -> None:
        """Start or stop a VM; a VM already in that state is left alone (so retries are safe)."""
        if self._dialect == "rest":
            await asyncio.to_thread(self._soap_power_sync, vm_id, action)
            return
        if await self._power_state(client, vm_id) == ("POWERED_ON" if action == "start" else "POWERED_OFF"):
            return
        await self._api_post(client, f"/vcenter/vm/{vm_id}/power?action={action}")

    async def _delete_vm(self, client: httpx.AsyncClient, vm_id: str) -> None:
        """Power off (if running) then delete a VM. A VM that is already gone counts as deleted."""
        if self._dialect == "rest":
            await asyncio.to_thread(self._soap_delete_sync, vm_id)
            return
        try:
            await self._power_action(client, vm_id, "stop")
            await self._api_delete(client, f"/vcenter/vm/{vm_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:  # gone between the calls, or before them
                raise

    async def _wait_tools(self, client: httpx.AsyncClient, vm_id: str, timeout: float | None = None) -> bool:
        """Wait (``timeout``, default VSPHERE_TOOLS_TIMEOUT) for VMware Tools to report
        running inside the VM. Asks at least once, however short the timeout."""
        deadline = time.monotonic() + (self._tools_timeout if timeout is None else timeout)
        while True:
            try:
                if self._dialect == "rest":
                    if await asyncio.to_thread(self._soap_tools_sync, vm_id):
                        return True
                else:
                    tools = await self._api_get(client, f"/vcenter/vm/{vm_id}/tools")
                    if tools.get("run_state") == "RUNNING":
                        return True
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(5.0, max(0.0, deadline - time.monotonic())))

    async def _guest_ipv4s(self, client: httpx.AsyncClient, vm_id: str) -> list[str] | None:
        """Every IPv4 address VMware Tools reports in the guest; None when it cannot be read."""
        try:
            if self._dialect == "rest":
                return await asyncio.to_thread(self._soap_ips_sync, vm_id)
            guest_nics = await self._api_get(client, f"/vcenter/vm/{vm_id}/guest/networking/interfaces")
            return [ip for iface in guest_nics for addr in (iface.get("ip") or {}).get("ip_addresses", [])
                    if (ip := addr.get("ip_address", "")) and ":" not in ip
                    and addr.get("state", "PREFERRED") == "PREFERRED"]
        except Exception:
            return None

    async def _await_guest_network(self, client: httpx.AsyncClient, vm_def: dict, vm_id: str) -> dict:
        """What a powered-on VM's guest reports: Tools running, and its IPv4 addresses once
        every planned static one is there (vsphere_readiness.planned_ips), polled until
        VSPHERE_GUEST_IP_TIMEOUT. A VM with nothing to verify (an appliance, no static
        address, VSPHERE_GUEST_IP_CHECK=off) waits VSPHERE_TOOLS_TIMEOUT for Tools, as
        before. Never past the build budget, but always one look."""
        planned = vsphere_readiness.planned_ips(vm_def)
        checked = self._guest_ip_check and bool(planned) and vsphere_readiness.verifiable(vm_def)
        now, powered_on_at = time.monotonic(), time.time()
        full = self._guest_ip_timeout if checked else self._tools_timeout
        timeout = min(full, max(0.0, self._budget_end - now)) if self._budget_end else full
        deadline = now + timeout
        seen: list[str] = []
        tools_ready = await self._wait_tools(client, vm_id, timeout)
        while tools_ready:
            seen = await self._guest_ipv4s(client, vm_id) or seen
            if not checked or set(planned) <= set(seen) or time.monotonic() >= deadline:
                break
            await asyncio.sleep(min(self._guest_poll, max(0.0, deadline - time.monotonic())))
        # ``cut``: the build budget, not VSPHERE_GUEST_IP_TIMEOUT, ended the wait. The rest of
        # the window is the reconcile task's (``deadline``, epoch seconds), as for a long setup.
        return {"tools_ready": tools_ready, "seen": seen, "timeout": round(timeout), "checked": checked,
                "cut": checked and timeout < full, "deadline": powered_on_at + full}

    async def _await_customization(self, si, vm, vm_def: dict, powered_on: float, powered_on_at: float,
                                   since: float) -> dict:
        """#255: a Windows VM's guest customization (Sysprep) outcome after power-on
        (vsphere_customization.read), polled until it finishes or the readiness window
        (VSPHERE_GUEST_IP_TIMEOUT from ``powered_on``, monotonic) ends; never past the build
        budget, but always one look. VSPHERE_GUEST_IP_CHECK=off: the one look only.
        ``since``: when the power-on was asked for (epoch); a success that ended before it
        is not this boot's. Sysprep's message is scrubbed of the VM's passwords."""
        full = self._guest_ip_timeout
        end = min(powered_on + full, self._budget_end) if self._budget_end else powered_on + full
        checked = self._guest_ip_check
        secrets_ = [c.password.reveal() for c in (vm_def.get("_guest"), vm_def.get("_sysprep"),
                                                  *(vm_def.get("_accounts") or [])) if c is not None]
        while True:
            seen = await asyncio.to_thread(lambda: customization.read(si.content, vm, since=since, secrets=secrets_))
            if seen["status"] in customization.FINISHED or not checked or time.monotonic() >= end:
                break
            await asyncio.sleep(min(self._guest_poll, max(0.0, end - time.monotonic())))
        # ``cut``: the build budget, not the window, ended the wait (the reconcile task settles it).
        return {**seen, "checked": checked, "timeout": round(max(0.0, end - powered_on)), "since": since,
                "cut": checked and end < powered_on + full, "deadline": powered_on_at + full}

    # ------------------------------------------------------------------ #
    # Snapshots: vSphere Web Services API through pyVmomi
    # ------------------------------------------------------------------ #
    # This used to POST /vcenter/vm/{vm}/snapshot, a path the Automation REST API does
    # not have. Checked against VMware's generated vmware-vcenter 8.0.3.0 bindings:
    # nothing under /vcenter/vm/{vm} touches snapshots. Every call failed, and the
    # worker then stored the result as a good snapshot anyway.
    #
    # REST VM ids ("vm-42") are managed-object ids, so the vm_id that provision()
    # recorded addresses the same VM here.

    def _connect(self, timeout: float | None = None):
        """A pyVmomi session; ``timeout`` bounds connect and every reply (socket timeout)."""
        if SmartConnect is None:
            raise RuntimeError("pyvmomi is required for vSphere snapshots (pip install pyvmomi)")
        url = urlparse(self._base_url)
        extra = {"httpConnectionTimeout": timeout} if timeout else {}
        return SmartConnect(
            host=url.hostname,
            port=url.port or 443,
            user=self._username,
            pwd=self._password,
            disableSslCertValidation=not self._verify_ssl,
            **extra,
        )

    @contextmanager
    def _vim(self):
        """A pyVmomi session: the enclosing ``_soap_scope``'s when there is one, else a new one."""
        if self._soap_si is not None:
            yield self._soap_si
            return
        si = self._connect()
        try:
            yield si
        finally:
            Disconnect(si)

    def _fan_out(self, si, vm_ids: list[str], submit: Callable,
                 results: dict[str, str] | None = None) -> tuple[dict[str, str], set[str]]:
        """Start ``submit(vm)``'s tasks, a batch of VSPHERE_CONCURRENCY VMs at a time.

        vCenter runs a batch side by side, so a 20-VM range does not take twenty
        snapshots' worth of wall-clock, without handing vCenter all twenty at once.
        Returns the VMs that failed with the reason, and the VMs whose tasks started.
        ``results``: filled with each VM's task result id (a new snapshot's moid).
        """
        failed: dict[str, str] = {}
        submitted: set[str] = set()
        for i in range(0, len(vm_ids), self._concurrency):
            started: list[tuple[str, object]] = []
            for vm_id in vm_ids[i : i + self._concurrency]:
                try:
                    tasks = submit(vim.VirtualMachine(vm_id, si._stub))
                except Exception as exc:
                    failed[vm_id] = getattr(exc, "msg", None) or str(exc)
                    continue
                started += [(vm_id, task) for task in tasks]
                if tasks:
                    submitted.add(vm_id)
            for vm_id, task in started:
                try:
                    # Bounded, and well inside Celery's one-hour visibility timeout: a
                    # task stuck in vCenter must not hold the worker until the broker
                    # redelivers the job to a second worker.
                    _wait_task(task, si, self._snapshot_timeout)
                except Exception as exc:
                    failed.setdefault(vm_id, getattr(exc, "msg", None) or str(exc))
                    continue
                if results is not None:  # task.info is a round trip: only when asked for
                    made = getattr(getattr(getattr(task, "info", None), "result", None), "_moId", None)
                    if made:
                        results[vm_id] = str(made)
        return failed, submitted

    def _snapshot_sync(self, vm_ids: list[str], name: str,
                       taken: dict[str, str] | None = None) -> tuple[dict[str, str], set[str]]:
        with self._vim() as si:
            return self._fan_out(
                si,
                vm_ids,
                lambda vm: [
                    vm.CreateSnapshot_Task(
                        name=name, description="TrueNorth range snapshot", memory=False, quiesce=False
                    )
                ],
                taken,
            )

    def _restore_sync(self, vm_ids: list[str], name: str, power_on: bool) -> tuple[dict[str, str], set[str]]:
        """Revert, then power on. The second value is every VM a revert started on."""

        def revert(vm):
            found = _named_snapshots(vm, name)
            if not found:  # raised before any task starts, so the VM is untouched
                raise LookupError(f"no snapshot named {name!r}")
            return [found[-1].snapshot.RevertToSnapshot_Task()]

        def power(vm):
            return [] if vm.runtime.powerState == "poweredOn" else [vm.PowerOnVM_Task()]

        with self._vim() as si:
            failed, reverted = self._fan_out(si, vm_ids, revert)
            if power_on:
                # The snapshots are taken without memory, so a revert leaves the VM off.
                failed.update(self._fan_out(si, [v for v in vm_ids if v not in failed], power)[0])
            return failed, reverted

    def _delete_snapshot_sync(self, vm_ids: list[str], name: str) -> tuple[dict[str, str], set[str]]:
        failed: dict[str, str] = {}
        with self._vim() as si:
            # One copy per VM per pass: vCenter refuses a second snapshot task on a VM
            # that is still running one. A VM without the snapshot counts as cleaned.
            for _ in range(5):
                pending = [
                    v for v in vm_ids if v not in failed and _named_snapshots(vim.VirtualMachine(v, si._stub), name)
                ]
                if not pending:
                    break
                failed.update(
                    self._fan_out(
                        si,
                        pending,
                        lambda vm: [_named_snapshots(vm, name)[0].snapshot.RemoveSnapshot_Task(removeChildren=False)],
                    )[0]
                )
        return failed, set()

    async def _vim_op(self, range_id: str, provision_output: dict, fn: Callable,
                      *args) -> tuple[int, list[str], set[str]]:
        """Run a blocking pyVmomi operation over the range's VMs (only those ``_screen``
        finds are the range's; any other is an error, and untouched).

        Returns how many VMs it succeeded on, the errors, and the VMs it started a
        task on (for restore: the ones that may have changed).
        """
        vms = provision_output.get("vms", [])
        vm_ids = [vm["vm_id"] for vm in vms if vm.get("vm_id")]
        # A VM with no recorded id cannot be addressed, so the operation cannot cover
        # the whole range. That is a failure, not something to skip over quietly.
        errors = [f"VM {vm.get('name')}: no vm_id recorded" for vm in vms if not vm.get("vm_id")]
        touched: set[str] = set()
        if not vm_ids:
            return len(vms) - len(errors), errors, touched
        try:
            # One pyVmomi session for the check and the operation (``fn``'s _vim() reuses it).
            async with self._soap_scope(always=True):
                keep, skipped, _ = await self._screen(range_id, vms)
                errors += skipped
                vm_ids = [vm["vm_id"] for vm in keep if vm.get("vm_id")]
                if vm_ids:
                    try:
                        failed, touched = await asyncio.to_thread(fn, vm_ids, *args)
                    except Exception as exc:  # no VM was touched
                        failed = dict.fromkeys(vm_ids, str(exc))
                    errors += [f"VM {vm_id}: {msg}" for vm_id, msg in failed.items()]
        except Exception as exc:  # could not connect, or read whose VMs they are: nothing was touched
            return 0, errors + [f"could not check the VMs are this range's: {self._redact(exc)}"], touched
        return len(vms) - len(errors), errors, touched

    # ------------------------------------------------------------------ #
    # Range build: pyVmomi (blocking; run through asyncio.to_thread)
    # ------------------------------------------------------------------ #

    def _wait(self, task, si) -> None:
        _wait_task(task, si, self._snapshot_timeout)

    @staticmethod
    def _vm(si, vm_id: str):
        """The VM a REST id ("vm-42", a managed-object id) names."""
        return vim.VirtualMachine(vm_id, si._stub)

    @staticmethod
    def _find(si, vimtype, name: str):
        """The first managed object of ``vimtype`` called ``name``, or None."""
        view = si.content.viewManager.CreateContainerView(si.content.rootFolder, [vimtype], True)
        try:
            return next((obj for obj in view.view if obj.name == name), None)
        finally:
            view.Destroy()

    def _datacenter_and_cluster(self, si):
        dc = self._find(si, vim.Datacenter, self._datacenter)
        if dc is None:
            raise RuntimeError(f"Datacenter not found: {self._datacenter!r}")
        cluster = self._find(si, vim.ClusterComputeResource, self._cluster)
        if cluster is None:
            raise RuntimeError(f"Cluster not found: {self._cluster!r}")
        return dc, cluster

    def _excluded(self, host_name: str) -> bool:
        """VSPHERE_EXCLUDE_HOSTS names a host by FQDN or short name, either way round:
        ``esx01`` excludes ``esx01.truenorth.lab`` and ``esx01.truenorth.lab`` excludes a
        host vCenter lists as ``esx01`` (case-insensitive). Short names are unique per cluster."""
        return template_copies.host_short_name(host_name) in {template_copies.host_short_name(e)
                                                              for e in self._exclude_hosts if e}

    def _eligible_hosts(self, cluster) -> list[infra.HostCapacity]:
        found = (infra.host_capacity(h) for h in cluster.host if not self._excluded(h.name))
        return [h for h in found if h is not None]

    def _guard(self, ref: dict) -> dict:
        if ref["name"] in self._mgmt_networks:
            raise RuntimeError(f"refusing to attach a range VM to management network {ref['name']!r}")
        return ref

    def _check_macs(self, si, dc, range_id: str, vm_defs: list[dict]) -> None:
        """Refuse the build, before any clone, when a VM anywhere in the vCenter, on any
        network, already has a MAC reserved for this range (vsphere_macs.py): a VM given a
        manual MAC from the slice by hand, another TrueNorth install using the same slice, or
        a VM that outlived its range after an abandon. One property-collector query over every
        VM; only MACs inside VSPHERE_MAC_POOL are kept.

        This range's own VMs are skipped: those its annotation names (RANGE_TAG) or that sit
        in its own folder. A redelivered or retried build finds the VMs its first attempt
        made with the same reserved MACs; they are not someone else's (a clone of the same
        name then fails on its own, naming the VM)."""
        wanted = {nic["mac"]: macs.holder(v["name"], i)
                  for v in vm_defs for i, nic in enumerate(v["nics"]) if nic.get("mac")}
        if not wanted:
            return
        pool = macs.parse_pool(self._mac_pool)
        # Its folder by id suffix (vsphere_tags): <root>/<name>-<id8> today, <id8> under the
        # earlier truenorth/ranges root, or one an operator or vcenter-organize.sh moved.
        own_folders = {f._moId for f in infra.find_range_folders(dc, self._range_roots(), range_id)}
        in_use: dict[str, str] = {}
        for vm_id, got in self._inventory_sync(si, ("name", "parent", "config.annotation",
                                                    "config.hardware.device")).items():
            owner = annotated_range(got.get("config.annotation"))
            # In its folder and not another range's: an earlier build's <id8> folder is shared
            # by every range of that id8, so a VM annotated for another range is never exempt.
            if owner == range_id or (not owner and getattr(got.get("parent"), "_moId", None) in own_folders):
                continue
            for card in infra.nic_cards(got.get("config.hardware.device") or []):
                if (mac := macs.normalize(getattr(card, "macAddress", None))) and mac in pool:
                    in_use.setdefault(mac, f"VM {got.get('name') or vm_id!r} ({vm_id}), which is not this range's")
        if clashes := macs.collisions(wanted, in_use):
            raise RuntimeError("; ".join(clashes) + ". VSPHERE_MAC_POOL must be this TrueNorth install's alone (each "
                               "install on a shared vCenter needs its own slice): change those VMs' MACs or remove "
                               "them, then build again")

    @staticmethod
    def _inventory_sync(si, paths) -> dict[str, dict]:
        return _inventory_props(si, paths)

    @staticmethod
    def _source_names(vm_defs: list[dict]) -> tuple[set[str], set[str]]:
        """(exact names, copy bases) of the inventory templates the build may clone (ADR 0013
        §2): each pinned VM's promoted build's copies (and the build's own template, for
        cluster placement), nothing else of its image. A VM the transition flag lets through
        unpromoted: its plain template and its host-local copies (``<base>@<host>-<date>``),
        so it still clones from its own host's disk (``_transition_copy``)."""
        names: set[str] = set()
        bases: set[str] = set()
        for v in vm_defs:
            build = v["template_build"]
            if build.get("promoted"):
                names.update((build.get("copies") or {}).values())
                names.add(build.get("template_name") or "")
            else:
                names.add(v["template_name"])
                bases.add(v["template_name"])
        return {n for n in names if n}, bases

    @staticmethod
    def _stores_of(vm, host: infra.HostCapacity) -> frozenset:
        """Names of ``host``'s datastores the template ``vm`` is on."""
        ids = {str(getattr(d, "_moId", "")) for d in getattr(vm, "datastore", None) or []}
        return frozenset(n for n, (_, ref) in host.datastores.items() if str(getattr(ref, "_moId", "")) in ids)

    def _transition_copy(self, templates: dict, vm_def: dict, host: infra.HostCapacity) -> tuple[str, frozenset]:
        """TRANSITION ONLY (an unpromoted VM): the newest copy of its template on ``host``'s
        own datastores, unverified (no build record to check it against), and those
        datastores; ("", empty) when the host has none and the plain template is cloned."""
        base = vm_def["template_name"]
        local = {n: st for n, vm in templates.items() if template_copies.is_copy_of(n, base)
                 and (st := self._stores_of(vm, host))}
        name = template_copies.newest_local(local, base, host.name) or ""
        return name, local.get(name, frozenset())

    @staticmethod
    def _plan_templates(vm_defs: list[dict], warnings: list[str]) -> None:
        """Every VM must carry the build it is pinned to (worker/template_pins.py, ADR 0013 §2),
        before anything is built. A VM let through unpromoted by the transition flag says so."""
        if missing := [v["name"] for v in vm_defs if not isinstance(v.get("template_build"), dict)]:
            raise RuntimeError(f"VMs {missing} are not pinned to a promoted template build (worker/template_pins.py); "
                               "nothing was built")
        for v in vm_defs:
            if not v["template_build"].get("promoted"):
                warnings.append(f"VM {v['name']}: template {v['template_name']!r} is NOT a promoted build (allowed by "
                                f"{v['template_build'].get('allowed_by') or 'the transition flag'}); promote a build "
                                "on the Golden Images page")

    def _copy_check(self, templates: dict, dupes: set[str], vm_def: dict, host: infra.HostCapacity) -> frozenset | str:
        """Placement's view of a pinned VM on ``host`` (vsphere_infra.CopyCheck): the names of
        the host's datastores holding the promoted build's copy for that host, or why the
        host cannot take the VM. A VM let through unpromoted may go anywhere (empty set)."""
        build = vm_def["template_build"]
        if not build.get("promoted"):
            return self._transition_copy(templates, vm_def, host)[1]  # preferred, never required
        short = template_copies.host_short_name(host.name)
        what = f"build {build['build_id']} of {build['catalogue_id']}"
        name = (build.get("copies") or {}).get(short)
        if not name:
            return f"{what} records no copy for host {short}"
        if name in dupes:
            return f"more than one template is named {name!r}"
        if (vm := templates.get(name)) is None:
            return f"{what}'s copy {name!r} is not an inventory template"
        annotation = getattr(getattr(vm, "config", None), "annotation", None)
        if problem := template_copies.stamp_problem(annotation, build["catalogue_id"], build["build_id"],
                                                    build["checksum"]):
            return f"copy {name!r} {problem}"
        return self._stores_of(vm, host) or f"copy {name!r} is not on a datastore of {host.name}"

    def _pinned_source(self, templates: dict, dupes: set[str], vm_def: dict) -> str:
        """The exact template the placed VM clones (ADR 0013 §2), checked once more against
        its placement: the promoted build's copy on its host; with cluster placement (no
        host) the build's own template, stamped as that build. A failure names the VM."""
        build = vm_def["template_build"]
        host, _ = vm_def["_placement"]
        if not build.get("promoted"):  # transition: its own host's copy when there is one
            return (self._transition_copy(templates, vm_def, host)[0] if host is not None else "") \
                or vm_def["template_name"]
        if host is not None:
            if isinstance(got := self._copy_check(templates, dupes, vm_def, host), str):
                raise RuntimeError(f"VM {vm_def['name']} on {host.name}: {got}")
            return build["copies"][template_copies.host_short_name(host.name)]
        name = build.get("template_name") or ""
        vm = None if name in dupes else templates.get(name)
        problem = ("is not an inventory template (or is not unique)" if vm is None else
                   template_copies.stamp_problem(getattr(vm.config, "annotation", None), build["catalogue_id"],
                                                 build["build_id"], build["checksum"]))
        if problem:
            raise RuntimeError(f"VM {vm_def['name']}: build {build['build_id']} of {build['catalogue_id']}: template "
                               f"{name!r} {problem} (VSPHERE_PLACEMENT=cluster clones the build's own template)")
        return name

    def _prepare_sync(self, si, range_id: str, vm_defs: list[dict], networks: list[dict], physical: dict) -> dict:
        """Placement, the range's VM folder and port groups, and the inventory templates.

        Sets ``_placement`` ((HostCapacity | None, datastore)) and ``_refs`` (one network
        ref per NIC) on every vm_def; returns the shared ``site`` (``notes``: what the build
        will lack, reported as errors).
        """
        site_notes: list[str] = []
        dc, cluster = self._datacenter_and_cluster(si)
        hosts = self._eligible_hosts(cluster)
        wanted, bases = self._source_names(vm_defs)
        view = si.content.viewManager.CreateContainerView(dc.vmFolder, [vim.VirtualMachine], True)
        templates: dict = {}
        dupes: set[str] = set()
        try:
            # Exactly the named templates (ADR 0013 §2: the promoted builds' copies), in any
            # folder but _retired (old hand-made templates moved aside: never a source).
            for vm in view.view:
                if (vm.name in wanted or getattr(template_copies.parse(vm.name), "base", None) in bases) \
                        and vm.config.template \
                        and getattr(getattr(vm, "parent", None), "name", None) != tags.RETIRED_FOLDER:
                    if vm.name in templates:
                        dupes.add(vm.name)  # two templates of one name: neither is provably the build
                    templates[vm.name] = vm
        finally:
            view.Destroy()
        if self._placement == "cluster":
            datastore = self._find(si, vim.Datastore, self._datastore)
            if datastore is None:
                raise RuntimeError(f"Datastore not found: {self._datastore!r}")
            placements = [(None, datastore)] * len(vm_defs)
        else:
            placements = infra.place(hosts, vm_defs, self._placement, self._vcpu_ratio,
                                     copies=lambda v, h: self._copy_check(templates, dupes, v, h))
        for vm_def, placed in zip(vm_defs, placements, strict=True):
            vm_def["_placement"] = placed
            vm_def["_source"] = self._pinned_source(templates, dupes, vm_def)

        refs: dict = {}
        names = {n.get("vlan_id"): n.get("name", "") for n in networks}
        dvs = None
        pg_hosts: list = []
        if not physical:
            pass  # nothing to create (a lab session on leased port groups only)
        elif self._switch_mode == "vss":
            # Every host a VM may run on: the placed ones, or all of them when DRS places.
            pg_hosts = list({id(h): h.ref for h, _ in placements if h}.values()) or [h.ref for h in hosts]
        else:
            dvs = self._find(si, vim.DistributedVirtualSwitch, self._dvs_name)
            if dvs is None:
                raise RuntimeError(f"Distributed switch not found: {self._dvs_name!r}")
        created: list[dict] = []  # the port groups this call made (a conflict removes them again)
        # The live switch, again, right before creating (range certification S1): ONE read for
        # the step (vss: of exactly the hosts the port groups go on, and are undone on). A VLAN
        # or name that appeared since allocation stops the build here; one that appears while
        # this step runs is caught by ensure_dvs_portgroup's own check, the same way.
        uses, switch = self._switch_usage(si, pg_hosts) if physical else ([], "")
        for logical, vlan in sorted(physical.items()):
            pg = infra.portgroup_name(range_id, vlan)
            promiscuous = infra.wants_promiscuous(names.get(logical, ""))
            try:
                existed = live_vlans.check(uses, pg, vlan, range_id, switch)
                if self._switch_mode == "vss":
                    for host in pg_hosts:
                        infra.ensure_vss_portgroup(host, pg, vlan, self._vswitch, promiscuous)
                    net = next((n for n in pg_hosts[0].network if n.name == pg), None)
                    if net is None:
                        raise RuntimeError(f"port group {pg!r} not visible on {pg_hosts[0].name} after creating it")
                else:
                    net = infra.ensure_dvs_portgroup(dvs, pg, vlan, promiscuous, lambda t: self._wait(t, si),
                                                     live_vlans.owner_mark(range_id))
            except live_vlans.PortgroupConflictError as exc:
                raise self._conflict(si, range_id, physical, created, exc, pg_hosts) from exc
            if not existed:
                created.append({"portgroup": pg, "physical_vlan": vlan, "switch_mode": self._switch_mode})
            refs[logical] = self._guard(infra.network_ref(net))
            for recorded in networks:  # its ids, for teardown and drift (range certification S5)
                if recorded.get("portgroup") == pg:
                    recorded.update(moid=refs[logical]["id"], key=refs[logical].get("key"))

        uplink = None
        if any(nic.get("uplink") for v in vm_defs for nic in v["nics"]):
            net = self._find(si, vim.Network, self._uplink_network)
            if net is None:
                raise RuntimeError(f"uplink network VSPHERE_RANGE_UPLINK_NETWORK={self._uplink_network!r} was not found")
            uplink = self._guard(infra.network_ref(net))
        fallback = None
        if any(nic.get("vlan") is None and not (nic.get("uplink") or nic.get("noise") or nic.get("port_group"))
               for v in vm_defs for nic in v["nics"]):
            net = self._find(si, vim.Network, self._network) if self._network else None
            if net is None:
                raise RuntimeError(f"a VM has no VLAN and the fallback network VSPHERE_NETWORK={self._network!r} "
                                   "was not found")
            fallback = self._guard(infra.network_ref(net))
        noise = None
        if any(nic.get("noise") for v in vm_defs for nic in v["nics"]):
            net = self._find(si, vim.Network, self._noise_network) if self._noise_network else None
            if net is None:  # the range still comes up; its noise agents cannot be reached
                for vm_def in vm_defs:
                    vm_def["nics"] = [n for n in vm_def["nics"] if not n.get("noise")]
                    vm_def.pop("_mgmt_ip", None)
                site_notes.append(f"noise management port group VSPHERE_NOISE_NETWORK={self._noise_network!r} "
                                  "was not found; background-noise agents will be unreachable")
            else:
                noise = self._guard(infra.network_ref(net))
        leased: dict = {}
        for name in sorted({n["port_group"] for v in vm_defs for n in v["nics"] if n.get("port_group")}):
            self._check_leased(name)
            net = self._find(si, vim.Network, name)
            if net is None:
                raise RuntimeError(f"lab port group {name!r} (LAB_PORT_GROUPS) was not found")
            leased[name] = self._guard(infra.network_ref(net))
        for vm_def in vm_defs:
            vm_def["_refs"] = [
                uplink if n.get("uplink")
                else noise if n.get("noise")
                else leased[n["port_group"]] if n.get("port_group")
                else refs[n["vlan"]] if n.get("vlan") is not None
                else fallback
                for n in vm_def["nics"]
            ]

        self._check_macs(si, dc, range_id, vm_defs)
        label = vm_defs[0].get("_label") if vm_defs else None
        folder = infra.ensure_range_folder(dc, self._range_folder, range_id, label or tags.range_key(range_id))
        return {"dc": dc, "cluster": cluster, "pool": self._pool(cluster), "folder": folder, "templates": templates,
                "dvs": dvs, "notes": site_notes}

    def _conflict(self, si, range_id: str, physical: dict, created: list[dict],
                  exc: live_vlans.PortgroupConflictError, pg_hosts: list) -> RangeNetworkConflictError:
        """Undo the port groups this step created, then say which VLANs the range no longer
        needs: every planned one but those whose port group of this range still exists
        (an earlier attempt's, which the range's destroy removes). The colliding VLAN is
        recorded as external only when the port group in the way really carries it."""
        note = ""
        try:
            dvs = None
            for net in created:  # only these: never a port group this step did not make
                if net["switch_mode"] == "vss":
                    for host in pg_hosts:  # the hosts it was created on, VLAN checked
                        infra.remove_vss_portgroup(host, net["portgroup"], net["physical_vlan"])
                    continue
                dvs = dvs or self._find(si, vim.DistributedVirtualSwitch, self._dvs_name)
                infra.remove_dvs_portgroup(dvs, net["portgroup"], lambda t: self._wait(t, si), net["physical_vlan"])
        except Exception as undo:  # noqa: BLE001 — reported; the planned output still lists them
            note = f"; removing the port groups this step created failed: {undo}"
        try:
            uses, _ = self._switch_usage(si, pg_hosts)
            kept = {v for v in physical.values()
                    if any(u.name == infra.portgroup_name(range_id, v) for u in uses)
                    and live_vlans.conflict(uses, infra.portgroup_name(range_id, v), v, range_id, "") is None}
        except Exception:  # noqa: BLE001 — keep every reservation rather than guess
            kept = set(physical.values())
        conflict = {"domain": self._domain("vlans"), "source": self._vlan_source(),
                    "vlans": {str(exc.vlan): exc.used_by} if exc.carried else {},
                    "release": sorted((str(v) for v in set(physical.values()) - kept), key=int)}
        return RangeNetworkConflictError(f"{exc}{note}", conflict)

    def _pool(self, cluster):
        """VSPHERE_RESOURCE_POOL (by name, anywhere under the cluster), else the cluster's root pool."""
        root = cluster.resourcePool
        if not self._resource_pool:
            return root
        queue = list(getattr(root, "resourcePool", None) or [])
        while queue:
            pool = queue.pop(0)
            if pool.name == self._resource_pool:
                return pool
            queue.extend(getattr(pool, "resourcePool", None) or [])
        raise RuntimeError(f"Resource pool not found under cluster {self._cluster!r}: {self._resource_pool!r}")

    @staticmethod
    def _check_leased(name: str) -> None:
        """Only the configured lab pool, whatever a template says: a range must never be able
        to name an arbitrary datacenter network (management, another tenant's)."""
        if name not in lab_port_groups():
            raise RuntimeError(f"port group {name!r} is not a lab network (LAB_PORT_GROUPS)")

    def _clone_sync(self, si, site: dict, vm_def: dict):
        """Clone the inventory VM template onto the placed host and datastore, NICs wired, powered off.

        Exactly ``_source`` (``_pinned_source``): the promoted build's copy on the placed host,
        or, let through unpromoted by the transition flag, the image's plain template."""
        source = vm_def["_source"]
        template = site["templates"].get(source)
        if template is None:
            raise RuntimeError(
                f"template {source!r} is neither a Content Library item nor an inventory VM template"
            )
        host, datastore = vm_def["_placement"]
        config = infra.hardware_spec(vm_def, template.config.hardware.device, vm_def["_refs"])
        self._tag(config, vm_def)
        self._drop_ovf_env(config, vm_def, template)
        spec = vim.vm.CloneSpec(
            location=vim.vm.RelocateSpec(pool=site["pool"], datastore=datastore, host=host.ref if host else None),
            powerOn=False,
            template=False,
            config=config,
        )
        build = vm_def["template_build"]
        if build.get("promoted"):
            logger.info("Cloning %r from %r (promoted build %s of %s) on %s", vm_def["name"], source,
                        build["build_id"], build["catalogue_id"], host.name if host else "the cluster")
        else:
            logger.warning("Cloning %r from UNPROMOTED template %r on %s (%s)", vm_def["name"], source,
                           host.name if host else "the cluster", build.get("allowed_by"))
        task = template.CloneVM_Task(folder=site["folder"], name=vm_def["name"], spec=spec)
        self._wait(task, si)
        vm = task.info.result
        if vm_def.get("_range_id") and annotated_range(getattr(vm.config, "annotation", None)) != vm_def["_range_id"]:
            # The clone spec's annotation did not land (govmomi's vcsim ignores it; vCenter
            # applies it): set it now, while the VM is still off and nothing else runs on it.
            self._wait(vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(annotation=range_annotation(vm_def["_range_id"]))), si)
        return vm

    @staticmethod
    def _tag(spec, vm_def: dict) -> None:
        """The full range id in the VM's annotation, set as the VM is created, so find_vms
        finds a VM even when a build died before recording it."""
        if vm_def.get("_range_id"):
            spec.annotation = range_annotation(vm_def["_range_id"])

    @staticmethod
    def _drop_ovf_env(spec, vm_def: dict, source) -> None:
        """Drop the OVF/vApp config from a Linux VM built from ``source``.

        tmpl-ubuntu-2404 comes from Ubuntu's cloud-image OVA: a vApp ProductSection with
        the com.vmware.guestInfo transport. vCenter then publishes guestinfo.ovfEnv at
        power-on, cloud-init's OVF datasource sorts ahead of VMware's, and the VM boots
        with the OVA's empty defaults, ignoring the static IPs in guestinfo.metadata.
        Same as scripts/lab/vim_helper.py vapp-off for the management VMs. Only while
        the VM is powered off (clone spec, or the reconfigure before first power-on).
        """
        if infra.os_family(vm_def) == "linux" and getattr(source.config, "vAppConfig", None) is not None:
            spec.vAppConfigRemoved = True

    def _reconfigure_sync(self, si, vm, vm_def: dict) -> None:
        """CPU, memory and NICs of an OVF-deployed VM, and (Linux) no OVF environment.

        A VM deployed from the cloud-image OVA's library item carries the OVA's vApp
        ProductSection just as an inventory clone of it does (see _drop_ovf_env)."""
        spec = infra.hardware_spec(vm_def, vm.config.hardware.device, vm_def["_refs"])
        self._tag(spec, vm_def)
        self._drop_ovf_env(spec, vm_def, vm)
        self._wait(vm.ReconfigVM_Task(spec=spec), si)

    def _customize_sync(self, si, vm, vm_def: dict) -> None:
        """Hostname and static IPs, applied while the VM is still powered off.

        Linux: cloud-init guestinfo metadata, NICs matched by MAC. Windows: Sysprep
        guest customization. pfSense / OPNsense: the per-range config.xml (``_pfsense``,
        planned in ``_plan_appliances``) in ``guestinfo.tn.pfsense.*`` /
        ``guestinfo.tn.opnsense.*``, with the NICs' MACs, for the template's boot script to
        apply. VyOS: the configuration commands (``_vyos``) as cloud-init user data in
        ``guestinfo.userdata``, each ethN pinned to its MAC. Router NIC order is WAN first
        (the uplink, when there is one), then the zones.

        A VM with software to install carries ``_guest`` credentials: the Windows
        Administrator password Sysprep sets, or the Linux install user cloud-init creates.

        The range's accounts (ADR 0012, ADR 0013 §3; ``_accounts``, worker/range_accounts.py):
        Windows gets its Administrator account's password (declared, or the implicit staff
        login the API adds), else a random one; never the template password or a site-wide
        default. Linux gets its accounts as cloud-init users, the build user's included (the
        implicit staff login), which replaces the template password it was built with.
        ``vsphere_credcheck`` then proves the template password no longer logs in.
        ``CustomizeVM_Task`` only applies the Windows spec: Sysprep runs after power-on, and
        its outcome is checked on every Windows VM (``_await_customization``, #255).

        Every NIC's MAC is read back first and must be the reserved one the clone spec set
        (vsphere_macs.py): the guest config is keyed to it, so a NIC that came out with
        any other MAC fails the VM here, before it is configured or powered on.
        """
        family = infra.os_family(vm_def)
        creds: guest.GuestCredentials | None = vm_def.get("_guest")
        nic_macs = [str(card.macAddress) for card in infra.nic_cards(vm.config.hardware.device)]
        if wrong := macs.mismatches(vm_def, nic_macs):
            raise RuntimeError(f"its NICs do not have their reserved MACs ({'; '.join(wrong)}); "
                               "not writing a guest network config keyed to the wrong MACs")
        if family == "linux":
            user = guest.cloud_init_user(creds) if creds else None
            # Security Onion: its accounts are set by its setup (vsphere_securityonion.py).
            accounts = ([] if seconion.is_securityonion(vm_def)
                        else guest.account_users(vm_def.get("_accounts") or [])[0])
            login = {"ssh_pwauth": True} if accounts else None
            # ``guestinfo``: more keys the guest reads itself (gs-core's bundle chunks).
            more = [vim.option.OptionValue(key=k, value=v) for k, v in (vm_def.get("guestinfo") or {}).items()
                    if str(k).startswith("guestinfo.tn.")]
            spec = vim.vm.ConfigSpec(extraConfig=infra.linux_guestinfo(vm_def, nic_macs, user, accounts, login) + more)
            self._wait(vm.ReconfigVM_Task(spec=spec), si)
        elif family == "windows":
            password = creds.password.reveal() if creds else guest.admin_password(vm_def)
            spec = infra.windows_customization(vm_def, password)
            # The password Sysprep sets (random when none was given): masked in any message (#255).
            vm_def["_sysprep"] = guest.GuestCredentials(
                "windows", "Administrator", guest.Secret(spec.identity.guiUnattended.password.value))
            self._wait(vm.CustomizeVM_Task(spec=spec), si)
        elif vm_def.get("_pfsense") is not None or vm_def.get("_vyos") is not None:
            # The range's admin login as a hash only (appliance_admin.py, issue #248).
            admin = vm_def.get("_appliance_admin")
            admin_hash = admin.hash.reveal() if admin is not None else None
            if vm_def.get("_pfsense") is not None:
                values = pfsense_config.guestinfo(vm_def["_pfsense"], nic_macs, admin_hash)
            else:
                values = vyos_config.guestinfo(vm_def["_vyos"], nic_macs, vm_def["name"], admin_hash)
            spec = vim.vm.ConfigSpec(extraConfig=[vim.option.OptionValue(key=k, value=v) for k, v in values.items()])
            self._wait(vm.ReconfigVM_Task(spec=spec), si)
        # Any other appliance boots with its template config (the runbook, §4.1a, says what
        # that config must hold).

    def _range_roots(self) -> list[str]:
        """Where range folders are looked for: VSPHERE_RANGE_FOLDER, then the earlier root."""
        return list(dict.fromkeys([self._range_folder, *tags.LEGACY_RANGE_ROOTS]))

    def _teardown_sync(self, si, range_id: str, networks: list[dict], mirrors: bool = False,
                       notes: list[str] | None = None) -> int:
        """Remove the range's mirror sessions, port groups and (empty) VM folder. Missing
        ones are fine. Sessions go by name prefix (``tn-<range8>-``), so ones a build made
        but never recorded go too; they go first, while their port groups still exist.

        A vDS port group whose moid was recorded (range certification S5) is removed only
        while the one of that name is still that moid; otherwise it is left and noted."""
        dc, cluster = self._datacenter_and_cluster(si)
        removed = 0
        dvs = None
        if mirrors or any(n.get("switch_mode", self._switch_mode) != "vss" for n in networks):
            dvs = self._find(si, vim.DistributedVirtualSwitch, self._dvs_name)
            if dvs is not None:
                removed += infra.remove_vspan_sessions(dvs, infra.mirror_session_prefix(range_id),
                                                       lambda t: self._wait(t, si))
            elif mirrors:
                raise RuntimeError(f"Distributed switch not found: {self._dvs_name!r}")
        for net in networks:
            name = net["portgroup"]
            if net.get("switch_mode", self._switch_mode) == "vss":
                removed += any([infra.remove_vss_portgroup(h, name, net.get("physical_vlan")) for h in cluster.host])
                continue
            dvs = dvs or self._find(si, vim.DistributedVirtualSwitch, self._dvs_name)
            if dvs is None:
                raise RuntimeError(f"Distributed switch not found: {self._dvs_name!r}")
            live = next((p for p in dvs.portgroup if p.name == name), None) if net.get("moid") else None
            if live is not None and getattr(live, "_moId", None) != net["moid"]:
                if notes is not None:
                    notes.append(f"port group {name}: now {live._moId}, not the recorded {net['moid']}; left alone")
                continue
            removed += infra.remove_dvs_portgroup(dvs, name, lambda t: self._wait(t, si), net.get("physical_vlan"))
        for folder in infra.find_range_folders(dc, self._range_roots(), range_id):
            if not folder.childEntity:  # by id8: today's <name>-<id8> and earlier <id8> folders
                self._wait(folder.Destroy_Task(), si)
        return removed

    @staticmethod
    def _vm_plan(range_id: str, vm_def: dict) -> dict:
        """A copy of the rendered VM with its final name and a ``nics`` list.

        render.py already names VMs ``<range8>-<node>``; a VM from a hand-written
        template gets that prefix here, once. A VM without ``nics`` (older renders,
        hand-written templates) has one NIC on its ``vlan_id``, or none: the fallback.
        """
        plan = dict(vm_def)
        prefix = f"{range_id[:8]}-"
        plan["name"] = vm_def["name"] if str(vm_def["name"]).startswith(prefix) else prefix + str(vm_def["name"])
        plan.setdefault("template_name", "ubuntu-2404-cloud")
        plan["_range_id"] = range_id
        plan["nics"] = [dict(n) for n in plan.get("nics") or []]  # the uplink is added to this copy
        if not plan.get("nics"):
            plan["nics"] = [{
                "vlan": vm_def.get("vlan_id"), "ip": vm_def.get("ip", ""), "prefix": vm_def.get("prefix", 24),
                "netmask": vm_def.get("netmask", "255.255.255.0"), "gateway": vm_def.get("gateway", ""),
            }]
            if vm_def.get("port_group"):
                plan["nics"][0]["port_group"] = vm_def["port_group"]
        mgmt = vm_def.get("mgmt") if isinstance(vm_def.get("mgmt"), dict) else None
        # Linux only until the Windows agent exists (an unconfigured NIC is just something
        # odd for a Student to find); appliances never run an agent.
        if mgmt and mgmt.get("ip") and infra.os_family(plan) == "linux":
            prefix = int(mgmt.get("prefix", 24))
            plan["nics"].append({
                "noise": True, "vlan": None, "network": "noise_mgmt", "ip": mgmt["ip"], "prefix": prefix,
                "netmask": str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask),
                "gateway": "",  # on-link to the noise controller, routed nowhere
            })
            plan["_mgmt_ip"] = mgmt["ip"]
        return plan

    @staticmethod
    def _logical_vlans(vm_defs: list[dict]) -> list[int]:
        """The template VLANs that need a physical VLAN and a port group of the range's own:
        every NIC's, except NICs on a leased lab port group."""
        return sorted({int(n["vlan"]) for v in vm_defs for n in v["nics"]
                       if n.get("vlan") is not None and not n.get("port_group")})

    # ------------------------------------------------------------------ #
    # BaseProvisioner implementation
    # ------------------------------------------------------------------ #

    async def provision(
        self,
        range_id: str,
        template: dict,
        allocations: dict,
    ) -> ProvisionResult:
        """Build the range. ``allocations["physical_vlans"]`` maps each template (logical)
        VLAN to the physical VLAN the worker reserved for it (``allocation_needs``). A VLAN
        with no reservation is refused, never picked here: only the reservation table,
        under its lock, decides who holds a VLAN.

        With VSPHERE_RANGE_UPLINK_NETWORK set, the edge firewall gets a WAN NIC first, at
        ``allocations["uplink_ip"]``, reserved the same way. Software in the nodes'
        ``services`` is installed after power-on (``_install_software``).

        Nothing is left behind on failure: when no VM could be built, the VMs and port
        groups this call made are removed again, so a retry starts clean.

        The vCenter and login are the constructor's only (worker/connection_binding.py). A
        ``credentials`` key in the template is ignored: until 2026-10-11 it replaced them, so a
        template author could send a build, and its recorded connection, to any host.
        """
        start = time.monotonic()
        # No new build work starts after this point. VSPHERE_PROVISION_BUDGET from the
        # provision start, and never past the task's ``finish_by`` (worker/tasks.py: the
        # Celery soft limit minus a reserve, same monotonic clock), so a long setup is
        # started and left ``running`` for the reconcile task, not waited on to the soft
        # limit (which would fail the task and store nothing).
        self._budget_end = start + self._budget
        if (finish_by := allocations.get("finish_by")) is not None:
            self._budget_end = min(self._budget_end, float(finish_by))
        errors: list[str] = []
        warnings: list[str] = []
        vms_out: list[dict] = []
        not_ready: list[str] = []
        vm_defs = [self._vm_plan(range_id, v) for v in template.get("vms", [])]
        label = tags.range_label(allocations.get("range_name"), range_id)  # folder + tn-range tag
        for vm_def in vm_defs:
            vm_def["_label"] = label
        networks = [dict(n) for n in template.get("networks", [])]
        physical: dict[int, int] = {}
        uplink: dict | None = None
        mirrors: list[dict] = []
        conflict: dict | None = None
        recorded: dict | None = None  # the range-wide inventory (range certification S5)

        try:
            if SmartConnect is None:
                raise RuntimeError("pyvmomi is required to build vSphere ranges (pip install pyvmomi)")
            self._plan_templates(vm_defs, warnings)  # pinned builds (ADR 0013), before anything is built
            uplink = self._plan_uplink(vm_defs, allocations)
            self._plan_accounts(vm_defs, warnings)
            self._plan_software(vm_defs, uplink, warnings)
            self._plan_roles(vm_defs)
            self._plan_appliances(vm_defs, template, networks, warnings)
            mirror_plans = self._plan_mirrors(template, networks, warnings)
            for name in {n["port_group"] for v in vm_defs for n in v["nics"] if n.get("port_group")}:
                self._check_leased(name)  # before anything is built
            logical = self._logical_vlans(vm_defs)
            rspan_keys = [p["rspan_key"] for p in mirror_plans]
            physical = {int(k): int(v) for k, v in (allocations.get("physical_vlans") or {}).items()}
            if missing := [v for v in logical + rspan_keys if v not in physical]:
                raise RuntimeError(f"VLANs {missing} have no reservation (worker: allocation_needs, network_reservations)")
            macs.assign(vm_defs, allocations.get("nic_macs") or {})  # one reserved MAC per NIC, the WAN NIC's too
            for plan in mirror_plans:  # an RSPAN VLAN: reserved like the zones', but no port group
                plan["rspan_vlan"] = physical[plan["rspan_key"]]
                networks.append({"name": f"rspan-{plan['dst']}", "vlan_id": plan["rspan_key"],
                                 "physical_vlan": plan["rspan_vlan"], "rspan": True})
            physical = {k: v for k, v in physical.items() if k in logical}
            for net in networks:
                if net.get("vlan_id") in physical:
                    net["physical_vlan"] = physical[net["vlan_id"]]
                    net["portgroup"] = infra.portgroup_name(range_id, net["physical_vlan"])
                    net["switch_mode"] = self._switch_mode
            # Port groups with no template network behind them (a hand-written vlan_id).
            listed = {n.get("vlan_id") for n in networks}
            networks += [
                {"name": f"vlan{lv}", "vlan_id": lv, "physical_vlan": pv,
                 "portgroup": infra.portgroup_name(range_id, pv), "switch_mode": self._switch_mode}
                for lv, pv in physical.items() if lv not in listed
            ]

            await self._get_session()  # REST login first: it decides the dialect of the VM operations
            with self._vim() as si:
                self._soap_si = si  # the "rest" dialect's VM operations share the build session
                try:
                    site = await asyncio.to_thread(self._prepare_sync, si, range_id, vm_defs, networks, physical)
                    errors += site.get("notes", [])
                    recorded = await asyncio.to_thread(self._range_inventory_sync, site, physical, allocations)
                    async with self._client() as client:
                        # No tagging in the budget's last two minutes: they are the build's.
                        self._tagger = (self._tag_client(client, deadline=self._budget_end - 120)
                                        if self._tags_enabled else None)
                        results = await asyncio.gather(
                            *(self._provision_one_vm(client, si, site, v) for v in vm_defs), return_exceptions=True
                        )
                    for vm_def, res in zip(vm_defs, results, strict=True):
                        if isinstance(res, Exception):
                            errors.append(f"VM {vm_def['name']}: {self._redact(res, vm_def)}")
                            continue
                        vms_out.append(res)
                        if note := res.pop("warning", ""):
                            warnings.append(f"VM {vm_def['name']}: {note}")
                        if reason := res.pop("not_ready", ""):  # built, but its guest network is not up
                            errors.append(f"VM {vm_def['name']}: {reason}")
                            not_ready.append(vm_def["name"])
                    if mirror_plans and vms_out:
                        mirrors = await asyncio.to_thread(self._mirror_sync, si, site, range_id, mirror_plans,
                                                          vm_defs, vms_out, errors, warnings)

                    # One semaphore for every guest software install, product VMs included,
                    # so VSPHERE_CONCURRENCY is respected across both paths (not one each).
                    software_gate = asyncio.Semaphore(self._concurrency)
                    # No guest work on a VM already not ready: its network is not up, so its
                    # installs and setups could only time out and bury the reason in errors.
                    usable = [o for o in vms_out if o["name"] not in not_ready]
                    if skipped := sorted(not_ready):
                        warnings.append(f"roles, software and setups skipped on VMs not ready: {', '.join(skipped)}")

                    async def roles_then_software() -> None:
                        await self._install_roles(si, vm_defs, usable, start, errors, warnings)
                        # Exchange 2019 / SharePoint (SE, 2019) / MECM images: after every role, so the
                        # range's DC is promoted; their own software runs after the domain join.
                        names = {v["name"] for v in vm_defs if vsphere_products.needs_setup(v)}
                        products = [o for o in usable if o["name"] in names]
                        await asyncio.gather(
                            self._install_software(si, vm_defs, [o for o in usable if o["name"] not in names],
                                                   start, errors, warnings, gate=software_gate),
                            self._setup_products(si, str(template.get("name") or range_id), vm_defs, products,
                                                 usable, start, errors, warnings, software_gate))

                    # Security Onion setup runs in its guest on its own; start it with the rest.
                    await asyncio.gather(roles_then_software(), self._setup_securityonion(
                        si, vm_defs, usable, networks, mirror_plans, start, warnings))
                    await self._verify_accounts(si, vm_defs, usable, start, warnings)
                    await self._check_template_logins(si, vm_defs, usable, errors, warnings, not_ready)
                except RangeNetworkConflictError as exc:  # nothing built; this step's port groups already removed
                    errors.append(str(exc))
                    conflict = exc.conflict
                except Exception as exc:
                    errors.append(self._redact(exc, *vm_defs))
                if not vms_out and conflict is None:
                    await self._rollback(si, range_id, networks, errors)
        except Exception as exc:
            errors.append(self._redact(exc, *vm_defs))
        finally:
            self._soap_si = None
            self._tagger = None
            for vm_def in vm_defs:  # the guest passwords end with this call
                for key in ("_guest", "_accounts", "_scrub", "_will_set", "_proven", "_sysprep", "_appliance_admin"):
                    vm_def.pop(key, None)

        built = {v["name"] for v in vms_out}
        if uplink and uplink["vm"] not in built:
            uplink = None  # the edge firewall was not built (its address stays reserved until destroy)
        return ProvisionResult(
            status=outcome(len(vms_out), errors) if vm_defs else ("failed" if errors else "ok"),
            vms=vms_out,
            networks=networks,
            duration_seconds=time.monotonic() - start,
            errors=errors,
            warnings=warnings,
            uplink=uplink,
            mirrors=mirrors,
            not_ready=[n for n in not_ready if n in built],
            conflict=conflict,
            inventory=recorded if vms_out else None,
        )

    # ------------------------------------------------------------------ #
    # WAN uplink and deploy-time software
    # ------------------------------------------------------------------ #

    @staticmethod
    def _redact(exc, *vm_defs: dict) -> str:
        # A vSphere fault often has an empty str(); its msg or its type still says what failed.
        text = str(exc) or getattr(exc, "msg", None) or type(exc).__name__
        secrets_ = ([v.get(k) for v in vm_defs for k in ("_guest", "_sysprep", "_appliance_admin")]
                    + [a for v in vm_defs for a in v.get("_accounts") or []])
        text = guest.redact(text, *secrets_)
        for v in vm_defs:  # an appliance's admin hash is not a password, but it is not for logs either
            if (admin := v.get("_appliance_admin")) is not None:
                text = text.replace(admin.hash.reveal(), "***")
        return text

    @staticmethod
    def _domain(what: str) -> str:
        """The space a reservation must be unique in: ``<site>:vlans`` or ``<site>:uplink``.

        ``<site>`` is VSPHERE_ALLOCATION_DOMAIN, default ``vsphere``: one VLAN space and one
        uplink space for every vSphere range. It is never derived from how the vCenter is
        addressed (IP or name, env or the Hypervisors page) or from switch names: a domain
        that changed with those let two ranges hold one VLAN. Give two sites their own
        value only when their ranges cannot share a VLAN segment or an uplink subnet."""
        return f"{os.getenv('VSPHERE_ALLOCATION_DOMAIN', '').strip() or 'vsphere'}:{what}"

    def allocation_needs(self, range_id: str, template: dict) -> list[AllocationNeed]:
        """Physical VLANs for every logical VLAN (and RSPAN VLAN) the build will use, and
        the edge firewall's uplink address: the same planning ``provision`` does."""
        vm_defs = [self._vm_plan(range_id, v) for v in template.get("vms", [])]
        logical = self._logical_vlans(vm_defs)
        rules = infra.mirror_rules(template) if self._switch_mode != "vss" else []  # as _plan_mirrors
        plans = infra.plan_mirrors(rules, [dict(n) for n in template.get("networks", [])])[0] if rules else []
        keys = logical + [p["rspan_key"] for p in plans if p["rspan_key"] not in logical]
        needs = []
        if keys:
            reserved = frozenset(str(v) for v in vlan_pool.parse_reserved(self._reserved_vlans))
            pool = [str(v) for v in vlan_pool.parse_pool(self._vlan_pool) if str(v) not in reserved]
            needs.append(AllocationNeed("physical_vlans", "vlan", self._domain("vlans"), pool, [str(k) for k in keys],
                                        excluded=reserved))
        if self._uplink_network and (edge := infra.pick_edge(vm_defs)) is not None:
            pool = uplink_pool.parse_ip_pool(self._uplink_pool)
            needs.append(AllocationNeed("uplink_ip", "uplink_ip", self._domain("uplink"), pool, ["edge"], single=True))
            edge["nics"].insert(0, {"uplink": True})  # as _plan_uplink: the WAN NIC is NIC 0
        if holders := macs.holders(vm_defs):  # one explicit MAC per NIC (vsphere_macs.py)
            needs.append(AllocationNeed("nic_macs", "mac", self._domain("macs"), macs.parse_pool(self._mac_pool),
                                        holders))
        return needs

    def live_usage(self, needs: list[AllocationNeed]) -> list[LiveUsage]:
        """The pool VLANs every port group on the range switch carries now (trunks
        expanded, uplink port groups skipped): read live before the VLANs are reserved
        (range certification S1, vsphere_vlans.py)."""
        vlan_needs = [n for n in needs if n.kind == "vlan"]
        if not vlan_needs:
            return []
        if SmartConnect is None:
            raise RuntimeError("pyvmomi is required to read the range switch (pip install pyvmomi)")
        read_at = datetime.now(UTC)  # before the read: what it saw is at least this fresh
        with self._vim() as si:
            uses, _ = self._switch_usage(si)
        out = []
        for need in vlan_needs:
            foreign, managed = live_vlans.by_value(uses, need.pool)
            out.append(LiveUsage(need.domain, need.kind, self._vlan_source(), foreign, managed, read_at=read_at))
        return out

    def _vlan_source(self) -> str:
        """Which switch a live VLAN read is of: ``<vCenter host>/<vDS or vSwitch>``."""
        switch = self._vswitch if self._switch_mode == "vss" else self._dvs_name
        return f"{urlparse(self._base_url).hostname or self._base_url}/{switch}"

    def _switch_usage(self, si, hosts=None) -> tuple[list, str]:
        """(every port group on the range switch, the switch's name), read live. ``hosts``
        (vss): the host objects to read; default every eligible host of the cluster."""
        if self._switch_mode == "vss":
            if hosts is None:
                _, cluster = self._datacenter_and_cluster(si)
                hosts = [h.ref for h in self._eligible_hosts(cluster)]
            return live_vlans.vss_usage(hosts), self._vswitch
        dvs = self._find(si, vim.DistributedVirtualSwitch, self._dvs_name)
        if dvs is None:
            raise RuntimeError(f"Distributed switch not found: {self._dvs_name!r}")
        return live_vlans.dvs_usage(si, dvs), dvs.name

    def planned_output(self, range_id: str, allocations: dict) -> dict:
        """The port groups and uplink address the build will use, recorded before it starts."""
        out: dict = {"provider": "vsphere_api", "range_id": range_id}
        if vlans := allocations.get("physical_vlans"):
            out["networks"] = [
                {"vlan_id": int(k), "physical_vlan": int(v), "portgroup": infra.portgroup_name(range_id, int(v)),
                 "switch_mode": self._switch_mode}
                for k, v in sorted(vlans.items(), key=lambda kv: int(kv[0]))
            ]
        if ip := allocations.get("uplink_ip"):
            out["uplink"] = {"network": self._uplink_network, "ip": ip}
        return out

    def _plan_uplink(self, vm_defs: list[dict], allocations: dict) -> dict | None:
        """Put the edge firewall's WAN NIC (NIC 0) on the uplink network, with its address."""
        if not self._uplink_network:
            return None
        if self._uplink_network in self._mgmt_networks:
            raise RuntimeError(
                f"refusing to attach a range VM to management network {self._uplink_network!r} "
                "(VSPHERE_RANGE_UPLINK_NETWORK)"
            )
        edge = infra.pick_edge(vm_defs)
        if edge is None:
            return None
        ip = allocations.get("uplink_ip")
        if not ip:
            raise RuntimeError("the edge firewall's uplink address has no reservation (worker: allocation_needs)")
        prefix = self._uplink_prefix
        netmask = str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)
        edge["nics"].insert(0, {
            "uplink": True, "vlan": None, "network": self._uplink_network, "ip": ip, "prefix": prefix,
            "netmask": netmask, "gateway": self._uplink_gateway,
        })
        return {"network": self._uplink_network, "ip": ip, "prefix": prefix, "gateway": self._uplink_gateway,
                "vm": edge["name"], "node_id": edge.get("node_id", "")}

    def _plan_appliances(self, vm_defs: list[dict], template: dict, networks: list[dict],
                         warnings: list[str]) -> None:
        """Render each router appliance's per-range config, after the uplink NIC is planned:
        a config.xml for pfSense and OPNsense (``_pfsense``, pfsense_config.py), configuration
        commands for VyOS (``_vyos``, vyos_config.py). The template's
        ``network.firewall_rules`` apply when it has any; rules that cannot be translated
        are skipped with a warning."""
        net = template.get("network") if isinstance(template.get("network"), dict) else {}
        rules = [r for r in (net.get("firewall_rules") or []) if isinstance(r, dict)]
        depot_host = (urlparse(self._depot_url).hostname or "") if self._depot_url else ""
        for vm_def in vm_defs:
            if infra.os_family(vm_def) != "appliance":
                continue
            kw = {"networks": networks, "rules": rules, "depot_host": depot_host, "depot_ports": self._depot_ports,
                  "hostname": infra.hostname(vm_def)}
            if product := pfsense_config.product_of(vm_def):  # pfSense or OPNsense: a config.xml
                cfg = vm_def["_pfsense"] = pfsense_config.build_config(vm_def, product=product, **kw)
            elif vyos_config.is_vyos(vm_def):  # VyOS: configuration commands through cloud-init
                cfg = vm_def["_vyos"] = vyos_config.build_config(vm_def, **kw)
            else:
                continue
            warnings += [f"VM {vm_def['name']}: {n}" for n in cfg.notes]
            try:  # its own admin password, as a hash (issue #248); the template's otherwise
                vm_def["_appliance_admin"] = appliance_admin.plan(vm_def)
            except appliance_admin.ApplianceAdminError as exc:
                warnings.append(f"VM {vm_def['name']}: {exc}")

    def _plan_mirrors(self, template: dict, networks: list[dict], warnings: list[str]) -> list[dict]:
        """The template's mirror rules, grouped by destination zone (vsphere_infra.plan_mirrors).

        Port mirroring is a vDS feature: on standard switches the rules are skipped with
        a warning (the monitoring port groups are still promiscuous, which shows a sensor
        the traffic of its own host's VMs on its own VLAN, nothing more)."""
        rules = infra.mirror_rules(template)
        if not rules:
            return []
        if self._switch_mode == "vss":
            warnings.append("mirror rules skipped: port mirroring needs VSPHERE_RANGE_SWITCH_MODE=vds")
            return []
        if self._mirror_direction not in ("received", "transmitted", "both"):
            warnings.append(f"VSPHERE_MIRROR_DIRECTION={self._mirror_direction!r} is not received, transmitted "
                            "or both; using received")
            self._mirror_direction = "received"
        plans, notes = infra.plan_mirrors(rules, networks)
        warnings += notes
        return plans

    def _mirror_sync(self, si, site: dict, range_id: str, plans: list[dict], vm_defs: list[dict],
                     vms_out: list[dict], errors: list[str], warnings: list[str]) -> list[dict]:
        """Create the port-mirroring sessions for the built VMs; returns what was made.

        Sources and destinations are the dvPorts the built VMs' NICs hold on the zones'
        port groups. A zone with no sensor NIC, or nothing to mirror, is a warning; a
        session vCenter refuses is an error (the range is then ``partial``)."""
        dvs = site.get("dvs")
        if dvs is None:
            return []
        by_name = {v["name"]: v for v in vm_defs}
        ports: dict[int, list[str]] = {}  # logical VLAN -> dvPort keys of the range's NICs on it
        sensors: dict[int, list[str]] = {}  # the same, less routers/firewalls: who receives copies
        for out in vms_out:
            vm_def = by_name[out["name"]]
            pg_vlan = {ref["key"]: nic["vlan"] for nic, ref in zip(vm_def["nics"], vm_def["_refs"], strict=True)
                       if ref and ref.get("kind") == "dvs" and nic.get("vlan") is not None}
            try:
                keys = infra.nic_port_keys(self._vm(si, out["vm_id"]).config.hardware.device)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"mirror: could not read the NICs of VM {out['name']}: {self._redact(exc)}")
                continue
            for pg_key, port_key in keys:
                if pg_key in pg_vlan:
                    if port_key:
                        ports.setdefault(pg_vlan[pg_key], []).append(str(port_key))
                        # The zone's gateway (soc-training's pfSense has a leg on the
                        # monitoring zone) must not be handed every other zone's frames.
                        if infra.os_family(vm_def) != "appliance":
                            sensors.setdefault(pg_vlan[pg_key], []).append(str(port_key))
                    else:
                        warnings.append(f"mirror: VM {out['name']} has no dvPort on its NIC on port group "
                                        f"{pg_key} (not mirrored)")
        uplinks = list(getattr(getattr(dvs.config, "uplinkPortPolicy", None), "uplinkPortName", None) or [])
        uplink = self._mirror_uplink or (uplinks[0] if uplinks else None)
        if uplink is None:
            warnings.append(f"mirror: {self._dvs_name} has no uplinks; sensors see only VMs on their own host")
        made: list[dict] = []
        for plan in plans:
            dst_ports = sorted(set(sensors.get(plan["dst_vlan"], [])))
            src_ports = sorted({p for _, vlan in plan["sources"] for p in ports.get(vlan, [])})
            name = infra.mirror_session_name(range_id, plan["dst"])
            if not dst_ports:
                warnings.append(f"mirror {name}: no VM has a NIC on {plan['dst']!r} to receive the copy (skipped)")
                continue
            if not src_ports:
                warnings.append(f"mirror {name}: no VM NIC on {', '.join(z for z, _ in plan['sources'])} "
                                "to mirror (skipped)")
                continue
            sessions = infra.vspan_sessions(range_id, plan, src_ports, dst_ports, uplink, plan["rspan_vlan"],
                                            self._mirror_direction)
            try:
                infra.ensure_vspan_sessions(dvs, sessions, lambda t: self._wait(t, si))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"mirror {name}: {self._redact(exc)}")
                continue
            made += [{
                "name": s.name, "type": s.sessionType, "switch": self._dvs_name,
                "source_zones": [z for z, _ in plan["sources"]] if s.sessionType == infra.MIRROR_LOCAL_TYPE else [],
                "destination_zone": plan["dst"], "rules": plan["rules"],
                "source_ports": src_ports if s.sessionType == infra.MIRROR_LOCAL_TYPE else [],
                "destination_ports": dst_ports,
                "uplink": uplink if s.sessionType == infra.MIRROR_LOCAL_TYPE else None,
                "rspan_vlan": plan["rspan_vlan"] if uplink else None,
                "direction": self._mirror_direction if s.sessionType == infra.MIRROR_LOCAL_TYPE else None,
            } for s in sessions]
        return made

    def _plan_software(self, vm_defs: list[dict], uplink: dict | None, warnings: list[str]) -> None:
        """Resolve each VM's ``services`` against the catalogue; give VMs that will install
        something guest credentials (``_guest``) and their install specs (``_software``).

        Without an uplink or a depot, nothing installs: a warning, not a failure."""
        wanted = [v for v in vm_defs if v.get("services") and infra.os_family(v) in ("windows", "linux")
                  and not seconion.is_securityonion(v)]  # its services come with so-setup
        if not wanted:
            return
        try:
            catalogue = software_catalogue.load()
        except software_catalogue.CatalogueError as exc:
            warnings.append(f"software installs skipped: {exc}")
            return
        planned = []
        for vm_def in wanted:
            family = infra.os_family(vm_def)
            specs, notes = software_catalogue.resolve(vm_def["services"], family, str(vm_def.get("os") or ""),
                                                      catalogue)
            warnings += [f"VM {vm_def['name']}: {n}" for n in notes]
            if specs:
                planned.append((vm_def, family, specs))
        if not planned:
            return
        why = ("VSPHERE_RANGE_UPLINK_NETWORK is not set, so the range has no path to the depot"
               if not self._uplink_network else
               "TN_DEPOT_URL is not set" if not self._depot_url else
               "the range has no firewall/router to carry the depot uplink" if uplink is None else "")
        if why:
            names = sorted({s.name for _, _, specs in planned for s in specs})
            warnings.append(f"software installs skipped ({why}): {', '.join(names)}")
            for vm_def, _, specs in planned:
                vm_def["_software_skipped"] = [s.name for s in specs]
            return
        for vm_def, family, specs in planned:
            vm_def["_software"] = specs
            vm_def["_guest"] = (guest.windows_credentials(guest.admin_password(vm_def)) if family == "windows"
                                else guest.linux_credentials())

    def _install_sync(self, si, vm_def: dict, vm_id: str, deadline: float) -> dict:
        creds: guest.GuestCredentials = vm_def["_guest"]
        specs = vm_def["_software"]
        warn_not_offline(str(vm_def.get("name") or vm_id), specs)
        if creds.family == "windows":
            commands, cleanup = guest.windows_commands(specs, self._choco_feed), None
        else:
            commands, cleanup = guest.linux_commands(specs, self._apt_proxy), guest.linux_cleanup_command()
        if vm_def.get("_scrub"):
            # Range accounts (ADR 0012): the install user stays for _verify_accounts, which
            # checks them, removes it and rewrites the guestinfo.
            cleanup = None
        vm = self._vm(si, vm_id)
        session = guest.GuestSession(si.content, vm, creds, poll=self._guest_poll)
        result = guest.install(session, commands, deadline, cleanup)
        if creds.family == "linux" and not vm_def.get("_scrub"):
            # The install user is gone (or expiring); take it out of the guestinfo too.
            try:
                macs = [card.macAddress for card in infra.nic_cards(vm.config.hardware.device)]
                spec = vim.vm.ConfigSpec(extraConfig=infra.linux_guestinfo(vm_def, macs))
                self._wait(vm.ReconfigVM_Task(spec=spec), si)
            except Exception as exc:  # noqa: BLE001
                result.setdefault("error", f"could not scrub the install user from guestinfo: "
                                           f"{guest.redact(str(exc), creds)}")
        return result

    async def _install_software(self, si, vm_defs: list[dict], vms_out: list[dict], start: float,
                                errors: list[str], warnings: list[str],
                                gate: asyncio.Semaphore | None = None) -> None:
        """Install each built VM's software inside the guest, VSPHERE_CONCURRENCY VMs at a time.

        Every VM gets TN_SOFTWARE_INSTALL_TIMEOUT from when its install starts, but nothing
        runs past VSPHERE_PROVISION_BUDGET from the start of the provision. A failed or
        timed-out package is an error (the range is ``partial``), never a failed build.

        ``gate`` is the provision's shared concurrency semaphore; product VMs install their
        software through the same one, so VSPHERE_CONCURRENCY bounds every guest install.
        """
        by_name = {v["name"]: v for v in vm_defs}
        for out in vms_out:
            skipped = by_name[out["name"]].get("_software_skipped")
            if skipped:
                out["software"] = {"status": "skipped", "packages": [
                    {"name": n, "status": "skipped", "exit_code": None} for n in skipped]}
        todo = [(by_name[o["name"]], o) for o in vms_out if by_name[o["name"]].get("_software")]
        if not todo:
            return
        hard_stop = self._budget_end
        gate = gate or asyncio.Semaphore(self._concurrency)

        async def one(vm_def: dict, out: dict) -> None:
            async with gate:
                now = time.monotonic()
                deadline = min(now + self._install_timeout, hard_stop)
                if deadline - now < 60:
                    out["software"] = {"status": "skipped", "packages": [
                        {"name": s.name, "status": "skipped", "exit_code": None} for s in vm_def["_software"]]}
                    warnings.append(f"VM {out['name']}: software installs skipped: the provision time budget "
                                    f"(VSPHERE_PROVISION_BUDGET={self._budget}s) is spent")
                    return
                try:
                    res = await asyncio.to_thread(self._install_sync, si, vm_def, out["vm_id"], deadline)
                except Exception as exc:  # noqa: BLE001
                    res = {"status": "failed", "packages": [], "error": self._redact(exc, vm_def)}
                out["software"] = res
                for pkg in res["packages"]:
                    if pkg["status"] != "ok":
                        code = f", exit {pkg['exit_code']}" if pkg["exit_code"] is not None else ""
                        errors.append(f"VM {out['name']}: software {pkg['name']} {pkg['status']}{code}")
                if res.get("error"):
                    errors.append(f"VM {out['name']}: software install: {res['error']}")

        await asyncio.gather(*(one(v, o) for v, o in todo))

    @staticmethod
    def _plan_roles(vm_defs: list[dict]) -> None:
        """A Windows VM with feature roles to install, or a product image to set up
        (vsphere_products.py), needs a known Administrator password: the one software
        installs use, or its own (``_guest``, set by Sysprep)."""
        for vm_def in vm_defs:
            if ((vsphere_roles.needs_guest(vm_def) or vsphere_products.needs_setup(vm_def))
                    and infra.os_family(vm_def) == "windows"):
                vm_def.setdefault("_guest", guest.windows_credentials(guest.admin_password(vm_def)))

    @staticmethod
    def _plan_accounts(vm_defs: list[dict], warnings: list[str]) -> None:
        """What this build sets of each VM's declared accounts (ADR 0012).

        Windows: only the built-in Administrator (Sysprep); other accounts are scenario
        content. Linux: every name useradd accepts (cloud-init). pfSense, OPNsense, VyOS: their
        admin user only (``appliance_admin``, issue #248). ``_will_set`` names what will be
        set (the build reports it as ``accounts_applied`` once verified); the rest is a warning. A Linux VM whose
        cloud-config will carry account hashes is marked ``_scrub`` and given the ephemeral
        install login, used after first boot to see cloud-init finish, check each account
        exists and take the hashes out of its guestinfo (``_verify_accounts``)."""
        for vm_def in vm_defs:
            if seconion.is_securityonion(vm_def):
                # Its setup sets (and verifies) its admin and SOC web login; no cloud-init.
                vm_def["_will_set"] = []
                continue
            accounts = vm_def.get("_accounts") or []
            names = [a.username for a in accounts]
            family = infra.os_family(vm_def)
            if family == "windows":
                will = [n for n in names if n.casefold() == guest.WINDOWS_ADMIN]
            elif family == "linux":
                skipped = guest.account_users(accounts)[1]
                will = [n for n in names if n not in skipped]
                if will:
                    vm_def["_scrub"] = True
                    vm_def.setdefault("_guest", guest.linux_credentials())
            else:
                admin = appliance_admin.ADMIN_USERS.get(appliance_admin.product_of(vm_def) or "")
                will = [n for n in names if n.casefold() == admin]
            vm_def["_will_set"] = will
            if left := [n for n in names if n not in will]:
                warnings.append(f"VM {vm_def['name']}: declared account(s) not set at deploy on this {family} "
                                f"machine: {', '.join(left)}")

    def _scrub_sync(self, si, vm_def: dict, vm_id: str, deadline: float) -> tuple[list[str], str]:
        """As the install user: wait for cloud-init, check each planned account exists
        (``getent passwd``), remove the install user. Then rewrite the VM's guestinfo without
        account hashes or install user, whether or not any of that worked.

        Returns (the accounts found, why the install user may remain or "")."""
        vm = self._vm(si, vm_id)
        planned = list(vm_def.get("_will_set") or [])
        session = guest.GuestSession(si.content, vm, vm_def["_guest"], poll=self._guest_poll)
        result = guest.install(session, guest.linux_commands([], self._apt_proxy) + guest.account_checks(planned),
                               deadline, guest.linux_cleanup_command())
        found = guest.accounts_found(result)
        remains = ""
        if any(p["status"] == "not_run" for p in result["packages"]) or (planned and not result["packages"]):
            remains = "the install login failed, so the ephemeral tn-install user may remain (it expires in two days)"
        elif "could not remove the install user" in str(result.get("error", "")):
            remains = "the ephemeral tn-install user could not be removed (it expires in two days)"
        macs = [card.macAddress for card in infra.nic_cards(vm.config.hardware.device)]
        self._wait(vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(extraConfig=infra.linux_guestinfo(vm_def, macs))), si)
        return found, remains

    def _windows_admin_sync(self, si, vm_def: dict, vm_id: str, deadline: float) -> bool:
        """Whether the declared Administrator password logs in (guest operations)."""
        password = next(a.password.reveal() for a in vm_def.get("_accounts") or []
                        if a.username.casefold() == guest.WINDOWS_ADMIN)
        creds = guest.GuestCredentials("windows", "Administrator", guest.Secret(password))
        try:
            guest.GuestSession(si.content, self._vm(si, vm_id), creds, poll=self._guest_poll).wait_ready(deadline)
        except Exception:  # noqa: BLE001 — not verified: recorded as not set
            return False
        return True

    async def _verify_accounts(self, si, vm_defs: list[dict], vms_out: list[dict], start: float,
                               warnings: list[str]) -> None:
        """Report on every built VM the accounts that were verified to exist
        (``accounts_applied``, usernames only), VSPHERE_CONCURRENCY at a time:

        * Linux (``_scrub``): ``_scrub_sync``, at most VSPHERE_ACCOUNT_SCRUB_TIMEOUT; the
          guestinfo is rewritten without the hashes in every case;
        * Windows with a declared Administrator: a guest-operations login with its password
          (Sysprep may still be running: up to TN_SOFTWARE_INSTALL_TIMEOUT).

        Anything unverified is not reported (Students never see it) and gets a warning."""
        by_name = {v["name"]: v for v in vm_defs}
        gate = asyncio.Semaphore(self._concurrency)
        hard_stop = self._budget_end

        def deadline(timeout: float) -> float:
            now = time.monotonic()
            return max(min(now + timeout, hard_stop), now + 60)

        async def one(vm_def: dict, out: dict) -> None:
            planned = list(vm_def.get("_will_set") or [])
            verified: list[str] = []
            if vm_def.get("_scrub"):
                async with gate:
                    try:
                        verified, remains = await asyncio.to_thread(
                            self._scrub_sync, si, vm_def, out["vm_id"], deadline(self._scrub_timeout))
                        if remains:
                            warnings.append(f"VM {out['name']}: {remains}")
                    except Exception as exc:  # noqa: BLE001
                        warnings.append(f"VM {out['name']}: could not remove the range account hashes from its "
                                        f"guestinfo: {self._redact(exc, vm_def)}")
            elif planned and infra.os_family(vm_def) == "windows":
                async with gate:
                    if await asyncio.to_thread(self._windows_admin_sync, si, vm_def, out["vm_id"],
                                               deadline(self._install_timeout)):
                        verified = planned
            if missing := [n for n in planned if n not in verified]:
                warnings.append(f"VM {out['name']}: account(s) not verified, recorded as not set: "
                                f"{', '.join(missing)}")
            out["accounts_applied"] = [n for n in planned if n in verified]
            # A guest-operations login worked in this VM: the template-login check can trust a refusal.
            vm_def["_proven"] = bool(verified)

        # Security Onion VMs report their own (``_setup_securityonion``), appliances theirs
        # with their template-login check (``_appliance_check_sync``: no guest operations).
        await asyncio.gather(*(one(by_name[o["name"]], o) for o in vms_out if o["name"] in by_name
                               and not seconion.is_securityonion(by_name[o["name"]])
                               and infra.os_family(by_name[o["name"]]) != "appliance"))

    def _appliance_check_sync(self, si, vm_def: dict, out: dict) -> dict:
        """An appliance's template-login check (appliance_admin.check): SSH at its uplink
        address when the worker can reach it, else its boot hook's acknowledgement in
        guestinfo, read until RANGE_APPLIANCE_ACK_TIMEOUT (never past the build budget; not
        at all from a template whose finish marker does not offer it). A proven change marks
        its admin account set (``accounts_applied``). The hash is taken out of the VM's
        configuration after the check, whatever its outcome."""
        admin = vm_def.get("_appliance_admin")
        vm = self._vm(si, out["vm_id"])
        nics = vm_def.get("nics") or []
        mgmt_ip = str(nics[0].get("ip") or "") if nics and nics[0].get("uplink") else ""
        key = appliance_admin.ack_key(admin.product) if admin is not None else ""

        def extra(name: str) -> str:
            return next((str(o.value or "") for o in (getattr(vm.config, "extraConfig", None) or [])
                         if o.key == name), "")

        now = time.monotonic()
        deadline = min(now + self._appliance_ack_timeout, self._budget_end or float("inf"))
        res = appliance_admin.check(admin, mgmt_ip=mgmt_ip, read_ack=lambda: extra(key), deadline=deadline,
                                    read_marker=lambda: extra(appliance_admin.FINISH_KEY),
                                    poll=3 * self._guest_poll)
        proven = admin is not None and res["status"] in appliance_admin.PROVEN
        out["accounts_applied"] = [admin.account] if proven else []
        if admin is not None:
            cleared = False
            nic_macs = [str(card.macAddress) for card in infra.nic_cards(vm.config.hardware.device)]
            values = appliance_admin.scrub_values(vm_def, nic_macs)
            try:
                self._wait(vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(extraConfig=[
                    vim.option.OptionValue(key=k, value=v) for k, v in values.items()])), si)
                cleared = True
            except Exception as exc:  # noqa: BLE001 — a hash, not a password: say so, keep the result
                logger.warning("VM %s: could not take the admin hash out of its guestinfo (%s)",
                               out.get("name"), type(exc).__name__)
            out["admin_login"] = {**admin.summary(), "hash_cleared": cleared}
        return res

    def _template_check_sync(self, si, vm_def: dict, vm_id: str, proven: bool) -> dict:
        vm = self._vm(si, vm_id)
        return credcheck.check(lambda creds: guest.GuestSession(si.content, vm, creds, poll=self._guest_poll),
                               infra.os_family(vm_def), proven=proven,
                               securityonion=seconion.is_securityonion(vm_def))

    async def _check_template_logins(self, si, vm_defs: list[dict], vms_out: list[dict], errors: list[str],
                                     warnings: list[str], not_ready: list[str]) -> None:
        """The template-login check (vsphere_credcheck, ADR 0013 §3) of every built VM, once,
        after its accounts were verified: ``template_login`` on the VM's output. A VM whose
        template password still logs in is not ready under RANGE_TEMPLATE_LOGIN_CHECK=enforce.
        A Security Onion node is checked once its setup changed its login (here when it
        finished in the build, else by the reconcile task); so is a Windows VM whose Sysprep the
        build's budget left running (#255); an appliance over SSH or by its boot hook's
        acknowledgement (``_appliance_check_sync``, issue #248)."""
        by_name = {v["name"]: v for v in vm_defs}
        gate = asyncio.Semaphore(self._concurrency)

        async def one(vm_def: dict, out: dict) -> None:
            if infra.os_family(vm_def) == "appliance":
                try:  # no gate: mostly waiting for the appliance's acknowledgement
                    res = await asyncio.to_thread(self._appliance_check_sync, si, vm_def, out)
                except Exception as exc:  # noqa: BLE001 — never a pass
                    res = {"status": credcheck.INCONCLUSIVE, "method": appliance_admin.NONE,
                           "detail": f"not checked ({type(exc).__name__})"}
                out["template_login"] = res
                if res["status"] == appliance_admin.UNSUPPORTED:
                    warnings.append(f"VM {out['name']}: template-password check not possible: {res['detail']}")
                if reason := credcheck.report(out["name"], res, warnings):
                    errors.append(f"VM {out['name']}: {reason}")
                    not_ready.append(out["name"])
                return
            proven = bool(vm_def.get("_proven"))
            if (out.get("customization") or {}).get("status") == customization.RUNNING:
                # Sysprep has not replaced the template password yet: a check now proves nothing (#255).
                out["template_login"] = {"status": credcheck.INCONCLUSIVE, "detail": "checked when its guest "
                                         "customization (Sysprep) has finished (reconcile)"}
                return
            if seconion.is_securityonion(vm_def):
                if (out.get("securityonion") or {}).get("status") != "ok":
                    out["template_login"] = {"status": credcheck.INCONCLUSIVE, "detail": "checked when its Security "
                                             "Onion setup has changed the login (reconcile)"}
                    return
                proven = bool(out.get("accounts_applied"))
            async with gate:
                try:
                    res = await asyncio.to_thread(self._template_check_sync, si, vm_def, out["vm_id"], proven)
                except Exception as exc:  # noqa: BLE001 — never a pass
                    res = {"status": credcheck.INCONCLUSIVE, "detail": f"not checked ({type(exc).__name__})"}
            out["template_login"] = res
            if reason := credcheck.report(out["name"], res, warnings):
                errors.append(f"VM {out['name']}: {reason}")
                not_ready.append(out["name"])

        await asyncio.gather(*(one(by_name[o["name"]], o) for o in vms_out if o["name"] in by_name))

    supports_credential_rotation = True

    async def rotate_guest_passwords(self, range_id: str, vms: list[dict], timeout: float) -> dict[str, dict]:
        """Set new passwords in running VMs of the range (worker/credential_rotation.py), each
        ``{vm_id, name, family, logins: [GuestLogin, ...], changes: [GuestLogin, ...]}``: log in
        with the first of ``logins`` the guest accepts (one attempt each), then
        ``vsphere_credcheck.rotate``. Returns name -> ``{applied: [usernames verified], error}``.
        Only VMs that are ``range_id``'s on this vCenter (``_screen``, as ``run_in_guest``)."""
        if SmartConnect is None:
            raise RuntimeError("pyvmomi is required for guest operations (pip install pyvmomi)")
        async with self._soap_scope(always=True):
            _, notes, missing = await self._screen(range_id, [_screened(v) for v in vms])
        if notes:
            raise RuntimeError(notes[0])
        out = {v["name"]: {"applied": [], "error": "not on this vCenter"} for v in vms if v["vm_id"] in missing}
        todo = [v for v in vms if v["vm_id"] not in missing]
        out.update(await asyncio.to_thread(self._rotate_sync, todo, time.monotonic() + timeout))
        return out

    def _rotate_sync(self, vms: list[dict], run_end: float) -> dict[str, dict]:
        out: dict[str, dict] = {}
        host = self._transfer_host()
        with self._vim() as si:
            for v in vms:
                vm = self._vm(si, v["vm_id"])

                def session_for(creds, _vm=vm):
                    return guest.GuestSession(si.content, _vm, creds, poll=self._guest_poll, transfer_host=host,
                                              put=self._guest_put)

                family = v["family"]
                changes = [guest.RangeAccount(c.username, guest.Secret(c.password)) for c in v["changes"]]
                logins = [guest.GuestCredentials(family, c.username, guest.Secret(c.password)) for c in v["logins"]]
                deadline = min(time.monotonic() + self._install_timeout, run_end)
                try:
                    session_for(logins[0] if logins else guest.linux_credentials()).wait_tools(deadline)
                except Exception as exc:  # noqa: BLE001
                    out[v["name"]] = {"applied": [], "error": f"guest operations not ready ({type(exc).__name__})"}
                    continue
                login = next((c for c in logins if session_for(c).try_login() is True), None)
                if login is None:
                    out[v["name"]] = {"applied": [], "error": "none of the machine's admin logins was accepted"}
                    continue
                applied, error = credcheck.rotate(session_for(login), family, changes, deadline, session_for)
                out[v["name"]] = {"applied": applied, "error": error}
        return out

    def _roles_sync(self, si, vm_def: dict, vm_id: str, deadline: float) -> dict[str, dict]:
        session = guest.GuestSession(si.content, self._vm(si, vm_id), vm_def["_guest"], poll=self._guest_poll)
        return vsphere_roles.install(session, vm_def, deadline)

    async def _install_roles(self, si, vm_defs: list[dict], vms_out: list[dict], start: float,
                             errors: list[str], warnings: list[str] | None = None) -> None:
        """Windows Server roles of each built VM (vsphere_roles.py), before its software:
        VSPHERE_CONCURRENCY VMs at a time, VSPHERE_ROLE_TIMEOUT each, never past
        VSPHERE_PROVISION_BUDGET. Every role that does not end ``ok`` is an error, except an
        Exchange 2019 / SharePoint (SE, 2019) / MECM role: set up later (``_setup_products``), or, when its
        image was not built and the plain OS was, a warning and ``role_setup`` ``skipped``."""
        by_name = {v["name"]: v for v in vm_defs}
        hard_stop = self._budget_end
        gate = asyncio.Semaphore(self._concurrency)

        async def one(vm_def: dict, out: dict) -> None:
            status = vsphere_roles.initial_status(vm_def)
            if vsphere_roles.needs_guest(vm_def):
                async with gate:
                    now = time.monotonic()
                    deadline = min(now + self._role_timeout, hard_stop)
                    state, detail = "skipped", ""
                    if not vm_def.get("_guest"):
                        detail = "no guest login for this VM"
                    elif deadline - now < 60:
                        detail = f"the provision time budget (VSPHERE_PROVISION_BUDGET={self._budget}s) is spent"
                    else:
                        try:
                            status = await asyncio.to_thread(self._roles_sync, si, vm_def, out["vm_id"], deadline)
                        except Exception as exc:  # noqa: BLE001
                            state, detail = "failed", self._redact(exc, vm_def)
                    if detail:
                        for rid in vsphere_roles.feature_roles(vm_def):
                            status[rid] = {"status": state, "detail": detail}
            # A product image's role is set up later (_setup_products), which reports it.
            product = vsphere_products.setup_role(vm_def)
            if product:
                status[product] = {"status": "pending", "detail": "deploy-time setup after the domain controller"}
            elif fallback := vsphere_products.not_built(vm_def):
                product = fallback[0]
                res = vsphere_products.not_built_outcome(vm_def)
                status[product] = {"status": res["status"], "detail": res["detail"]}
                out["role_setup"] = res
                if warnings is not None:
                    warnings.append(f"VM {out['name']}: {res['detail']}")
            out["roles"] = status
            errors.extend(vsphere_roles.errors(out["name"], {k: v for k, v in status.items() if k != product}))

        await asyncio.gather(*(one(by_name[o["name"]], o) for o in vms_out if by_name[o["name"]].get("roles")))

    def _transfer_host(self) -> str | None:
        """The host a guest file-transfer URL's ``*`` resolves to: the vCenter/ESXi the
        adapter is bound to. The worker must reach it on 443 (mblanke/TrueNorth#194)."""
        return urlparse(self._base_url).hostname if getattr(self, "_base_url", "") else None

    def _product_session(self, si, vm_id: str):
        vm = self._vm(si, vm_id)
        return lambda creds: guest.GuestSession(si.content, vm, creds, poll=self._guest_poll,
                                                transfer_host=self._transfer_host(), put=self._guest_put)

    def _product_prepare_sync(self, si, plan, vm_id: str, deadline: float) -> dict | None:
        return vsphere_products.prepare(self._product_session(si, vm_id), plan, deadline)

    def _product_run_sync(self, si, plan, vm_id: str) -> dict:
        return vsphere_products.run(self._product_session(si, vm_id), plan, self._budget_end, self._budget_end)

    async def _setup_products(self, si, range_name: str, vm_defs: list[dict], todo: list[dict],
                              vms_out: list[dict], start: float, errors: list[str], warnings: list[str],
                              gate: asyncio.Semaphore) -> None:
        """Exchange 2019 / SharePoint (SE, 2019) / MECM role images (vsphere_products.py). Runs after
        every VM's roles, so the forest-root DC of the VM's domain is promoted, or not: then
        the setup is ``skipped``. Per VM, under the shared concurrency ``gate``: staged-image
        check and domain join as the local Administrator, the VM's own software, then the
        product setup started detached as the domain Administrator (guest-ops auth; no secret
        on a command line) and left ``running`` for the reconcile task (worker/setup_reconcile.py).
        Every outcome but ``ok`` is a warning, never a partial range; ``accounts_applied`` is
        unchanged."""
        by_name = {v["name"]: v for v in vm_defs}
        built = {o["name"]: o for o in vms_out}

        async def one(out: dict) -> None:
            vm_def = by_name[out["name"]]
            role = vsphere_products.setup_role(vm_def) or ""
            plan, res = None, None
            try:
                plan = vsphere_products.plan(vm_def, vm_defs, range_name, self._depot_url)
            except vsphere_products.PlanError as exc:
                res = vsphere_products.outcome(None, exc.status, str(exc), vsphere_products.setup_image(vm_def) or "")
            if plan is not None:
                warnings.extend(f"VM {out['name']}: {n}" for n in plan.notes)
                dc_state = ((built.get(plan.dc_name) or {}).get("roles") or {}).get("ad-ds", {}).get("status")
                if dc_state != "ok":
                    res = vsphere_products.outcome(plan, "skipped", f"domain controller {plan.dc_name} is not ready "
                                                                    f"(ad-ds {dc_state or 'not built'})")
            # Join under the shared gate: its per-VM deadline starts after the slot is held.
            if plan is not None and res is None:
                async with gate:
                    if self._budget_end - time.monotonic() < vsphere_products.MIN_START:
                        res = vsphere_products.outcome(plan, "skipped", "the provision time budget "
                                                                        "(VSPHERE_PROVISION_BUDGET) is spent")
                    else:
                        try:
                            res = await asyncio.to_thread(self._product_prepare_sync, si, plan, out["vm_id"],
                                                          self._budget_end)
                        except Exception as exc:  # noqa: BLE001
                            res = vsphere_products.outcome(plan, "failed", self._redact(exc, vm_def))
            # The VM's own software, through the same shared gate (acquired inside the call).
            await self._install_software(si, vm_defs, [out], start, errors, warnings, gate=gate)
            # Start the product setup (brief wait); the reconcile task finishes it.
            if plan is not None and res is None:
                async with gate:
                    try:
                        res = await asyncio.to_thread(self._product_run_sync, si, plan, out["vm_id"])
                    except Exception as exc:  # noqa: BLE001
                        res = vsphere_products.outcome(plan, "failed", self._redact(exc, vm_def))
            res = res or vsphere_products.outcome(plan, "failed", "no outcome")
            out["role_setup"] = res
            out.setdefault("roles", {})[role] = {"status": res["status"], "detail": res["detail"]}
            if res["status"] != "ok":
                warnings.append(vsphere_products.warning(out["name"], role, res))

        await asyncio.gather(*(one(o) for o in todo))

    def _securityonion_sync(self, si, vm_def: dict, vm_id: str, networks: list[dict], dst_vlans: set[int],
                            deadline: float, budget_end: float) -> tuple[dict, list[str]]:
        vm = self._vm(si, vm_id)
        macs = [str(card.macAddress) for card in infra.nic_cards(vm.config.hardware.device)]
        return seconion.setup(lambda creds: guest.GuestSession(si.content, vm, creds, poll=self._guest_poll),
                              vm_def, macs, networks, dst_vlans, deadline, budget_end)

    async def _setup_securityonion(self, si, vm_defs: list[dict], vms_out: list[dict], networks: list[dict],
                                   mirror_plans: list[dict], start: float, warnings: list[str]) -> None:
        """Each built Security Onion VM becomes a standalone node, monitoring the range's
        mirror zone (vsphere_securityonion.py). VSPHERE_SO_SETUP_TIMEOUT each, waiting
        never past VSPHERE_PROVISION_BUDGET; setup carries on in the guest either way.
        Every outcome but ``ok`` is a warning: the range is not partial for it."""
        by_name = {v["name"]: v for v in vm_defs}
        todo = [(by_name[o["name"]], o) for o in vms_out if seconion.is_securityonion(by_name[o["name"]])]
        if not todo:
            return
        dst_vlans = {int(p["dst_vlan"]) for p in mirror_plans}
        budget_end = self._budget_end

        async def one(vm_def: dict, out: dict) -> None:
            deadline = time.monotonic() + seconion.VSPHERE_SO_SETUP_TIMEOUT
            try:
                res, notes = await asyncio.to_thread(self._securityonion_sync, si, vm_def, out["vm_id"], networks,
                                                     dst_vlans, deadline, budget_end)
            except Exception as exc:  # noqa: BLE001
                detail = self._redact(exc, vm_def)
                res, notes = {"status": "failed", "detail": detail}, [f"VM {out['name']}: Security Onion setup "
                                                                       f"failed: {detail}"]
            # Its declared accounts verified in the guest (worker/range_accounts.record_applied).
            out["accounts_applied"] = list(res.pop("accounts_applied", None) or [])
            out["securityonion"] = res
            warnings.extend(notes)

        await asyncio.gather(*(one(v, o) for v, o in todo))

    # ------------------------------------------------------------------ #
    # Commands in a guest after the build (BaseProvisioner.run_in_guest)
    # ------------------------------------------------------------------ #

    supports_guest_commands = True

    def _guest_sync(self, vm_id: str, login, steps: list, deadline: float) -> list[dict]:
        creds = guest.GuestCredentials(login.family, login.username, guest.Secret(login.password))
        out = [{"label": s.label, "status": "not_run", "exit_code": None} for s in steps]
        with self._vim() as si:
            session = guest.GuestSession(si.content, self._vm(si, vm_id), creds, poll=self._guest_poll)
            try:
                session.wait_ready(deadline)
            except Exception as exc:  # noqa: BLE001 — reported with the password scrubbed
                raise RuntimeError(guest.redact(getattr(exc, "msg", None) or str(exc), creds)) from None
            for i, step in enumerate(steps):
                cmd = guest.GuestCommand(step.label, step.program, step.arguments, frozenset(step.ok_codes))
                try:
                    status, code = session.run(cmd, deadline)
                except Exception as exc:  # noqa: BLE001
                    status, code = "failed", None
                    out[i]["error"] = guest.redact(getattr(exc, "msg", None) or str(exc), creds)[:300]
                out[i].update(status=status, exit_code=code)
                if status != "ok":
                    break
        return out

    async def run_in_guest(self, vm_id: str, login, steps: list, timeout: float, *, range_id: str | None = None,
                           vm_name: str = "", identity: dict | None = None) -> list[dict]:
        """Guest operations as ``login`` (VMware Tools must run in the VM): wait until the
        login works, then each step in turn, all within ``timeout`` seconds.

        Only in a VM that is ``range_id``'s on this vCenter (``_screen``): a bare ``vm-NN``
        on the wrong vCenter would run the steps, as root, in someone else's VM. ``identity``:
        the VM's recorded identity (S5), so its instance UUID decides, as for every operation."""
        if SmartConnect is None:
            raise RuntimeError("pyvmomi is required for guest operations (pip install pyvmomi)")
        if not range_id:
            raise RuntimeError("guest commands need the range id: they run only in that range's own VM")
        async with self._soap_scope(always=True):
            _, notes, missing = await self._screen(range_id, [_screened({"vm_id": vm_id, "name": vm_name,
                                                                         "identity": identity})])
        if notes:
            raise RuntimeError(notes[0])
        if vm_id in missing:
            raise RuntimeError(f"VM {vm_name or vm_id}: {vm_id} is not on {self._base_url}")
        deadline = time.monotonic() + timeout
        return await asyncio.to_thread(self._guest_sync, vm_id, login, steps, deadline)

    supports_setup_reconcile = True

    def _reconcile_sync(self, range_id: str, products: list[dict], sos: list[dict], domain_passwords: dict,
                        so_logins: dict, missing: set[str], run_end: float,
                        per_vm: float) -> tuple[dict, list[str]]:
        changes: dict[str, dict] = {}
        warnings: list[str] = []
        host = self._transfer_host()

        def vm_deadline() -> float | None:  # None: this run's guest-wait budget is spent
            now = time.monotonic()
            return None if now >= run_end else min(now + per_vm, run_end)

        with self._vim() as si:
            self._soap_si = si
            try:
                for v in products:
                    info = (v.get("role_setup") or {}).get("reconcile") or {}
                    secret = domain_passwords.get(info.get("dc_node"))
                    if (expired := vsphere_products.timed_out(info)) is not None:
                        # Terminal whether or not the VM answers; then, if it does, delete any
                        # secret file the setup left (bounded, best effort).
                        changes[v["name"]] = {"role_setup": expired, "role": info.get("role", "")}
                        if secret and v["vm_id"] not in missing and (deadline := vm_deadline()) is not None:
                            creds = guest.GuestCredentials("windows", f"{info['netbios']}\\Administrator",
                                                           guest.Secret(secret))
                            vsphere_products.cleanup(guest.GuestSession(
                                si.content, self._vm(si, v["vm_id"]), creds, poll=self._guest_poll),
                                min(deadline, time.monotonic() + 30))
                        continue
                    if v["vm_id"] in missing or not secret or (deadline := vm_deadline()) is None:
                        if not secret:
                            warnings.append(f"VM {v['name']}: cannot reconcile {info.get('role')}: no Administrator "
                                            f"password for domain controller {info.get('dc_node')}")
                        continue
                    creds = guest.GuestCredentials("windows", f"{info['netbios']}\\Administrator",
                                                   guest.Secret(secret))
                    session = guest.GuestSession(si.content, self._vm(si, v["vm_id"]), creds, poll=self._guest_poll,
                                                 transfer_host=host, put=self._guest_put)
                    res = vsphere_products.reconcile(session, info, deadline=deadline)
                    if res is not None:
                        changes[v["name"]] = {"role_setup": res, "role": info.get("role", "")}
                for v in sos:
                    info = (v.get("securityonion") or {}).get("reconcile") or {}
                    login = so_logins.get(info.get("node"))
                    if (expired := seconion.timed_out(info)) is not None:
                        changes[v["name"]] = {"securityonion": expired, "accounts_applied": [],
                                              "node": info.get("node")}
                        if login and v["vm_id"] not in missing and (deadline := vm_deadline()) is not None:
                            install = guest.GuestCredentials("linux", login[0], guest.Secret(login[1]))
                            seconion.cleanup(guest.GuestSession(si.content, self._vm(si, v["vm_id"]), install,
                                                                poll=self._guest_poll),
                                             install, min(deadline, time.monotonic() + 30))
                        continue
                    if login is None or v["vm_id"] in missing or (deadline := vm_deadline()) is None:
                        continue
                    vm_ref = self._vm(si, v["vm_id"])

                    def session_for(creds, _vm=vm_ref):
                        return guest.GuestSession(si.content, _vm, creds, poll=self._guest_poll)

                    login_pw = login[3] if len(login) > 3 else ""
                    out, verified = seconion.reconcile(
                        session_for, guest.GuestCredentials("linux", login[0], guest.Secret(login[1])), info,
                        admin_password=guest.Secret(login[2]) if login[2] else None, deadline=deadline,
                        login_password=guest.Secret(login_pw) if login_pw else None)
                    if out is not None:
                        changes[v["name"]] = {"securityonion": out, "accounts_applied": sorted(verified),
                                              "node": info.get("node")}
                        if out.get("status") == "ok":  # its login changed: the template password must be gone
                            changes[v["name"]]["template_login"] = credcheck.check(
                                session_for, "linux", proven=bool(verified), securityonion=True)
            finally:
                self._soap_si = None
        return changes, warnings

    async def reconcile_setups(self, range_id: str, output: dict, *, domain_passwords: dict,
                               so_logins: dict, run_seconds: float = 240.0,
                               per_vm_seconds: float = 90.0) -> tuple[dict, list[str]]:
        """Read every ``running`` product / Security Onion setup's status in its guest and
        return the terminal outcomes to store (worker/setup_reconcile.py). ``domain_passwords``
        maps a DC node id to its Administrator password; ``so_logins`` a Security Onion node id
        to (install user, install password, declared admin password or ""). Only the range's own
        VMs are touched (``_screen``). A setup past its timeout ends without a guest operation;
        each VM gets at most ``per_vm_seconds`` and the run ``run_seconds`` of guest waiting."""
        if SmartConnect is None:
            raise RuntimeError("pyvmomi is required for guest operations (pip install pyvmomi)")
        vms = output.get("vms") or []
        products = [v for v in vms if (v.get("role_setup") or {}).get("status") == "running"
                    and (v["role_setup"].get("reconcile")) and v.get("vm_id")]
        sos = [v for v in vms if (v.get("securityonion") or {}).get("status") == "running"
               and (v["securityonion"].get("reconcile")) and v.get("vm_id")]
        if not products and not sos:
            return {}, []
        async with self._soap_scope(always=True):
            _, notes, missing = await self._screen(range_id, [_screened(v) for v in products + sos])
        if notes:
            return {}, [notes[0]]
        run_end = time.monotonic() + run_seconds
        return await asyncio.to_thread(self._reconcile_sync, range_id, products, sos, domain_passwords,
                                       so_logins, missing, run_end, per_vm_seconds)

    async def reconcile_guest_ips(self, range_id: str, output: dict, *, now: float | None = None,
                                  accounts: dict | None = None) -> dict[str, dict]:
        """Settle the readiness checks a build left ``running`` (its budget ended the wait;
        vsphere_readiness.assess): one VMware Tools read per pending VM, no waiting, so a
        reconcile run stays bounded. Returns {vm name: fields to store} for the VMs that
        are settled (verified, or past their window: ``not_ready``). Only the range's own
        VMs are read (``_screen``); a VM vCenter no longer has reports no address.

        A Windows VM's guest customization check left ``running`` (#255,
        vsphere_customization) is settled here too, from one read of its status (a success
        that ended before its power-on is not one; Sysprep's message scrubbed of the VM's
        ``accounts``: node id -> [RangeAccount], range_accounts.load). Once it succeeded, the
        template-login check the build left for it is made (vsphere_credcheck): proven by a
        login as the VM's own Administrator, when the range declares it."""
        pending = [v for v in output.get("vms") or []
                   if vsphere_readiness.pending(v) or customization.pending(v)]
        if not pending:
            return {}
        now = time.time() if now is None else now
        await self._get_session()
        changes: dict[str, dict] = {}
        async with self._soap_scope(always=True):
            ours, _, missing = await self._screen(range_id, [_screened(v) for v in pending])
            mine = {v["vm_id"] for v in ours}  # not ours under that id: left alone (and logged)
            async with self._client() as client:
                for v in (v for v in pending if v["vm_id"] in mine):
                    verdict: dict = {}
                    if vsphere_readiness.pending(v):
                        seen = [] if v["vm_id"] in missing else await self._guest_ipv4s(client, v["vm_id"])
                        verdict.update(vsphere_readiness.settle(v, seen, now) or {})
                    if customization.pending(v):
                        node = str(v.get("node_id") or str(v["name"]).removeprefix(f"{range_id[:8]}-"))
                        accts = list((accounts or {}).get(node) or [])
                        status = ({"status": "", "error": "", "source": "vm not found"} if v["vm_id"] in missing
                                  else await asyncio.to_thread(self._customization_sync, v["vm_id"],
                                                               customization.since(v),
                                                               [a.password.reveal() for a in accts]))
                        if (sysprep := customization.settle(v, status, now)) is not None:
                            if sysprep["customization"]["status"] == customization.OK:
                                admin = next((a for a in accts if a.username.casefold() == guest.WINDOWS_ADMIN), None)
                                sysprep["template_login"] = await asyncio.to_thread(
                                    self._windows_template_check_sync, v["vm_id"], admin)
                            reasons = (verdict.pop("not_ready", ""), sysprep.pop("not_ready", ""))
                            verdict.update(sysprep, **({"not_ready": "; ".join(r for r in reasons if r)}
                                                       if any(reasons) else {}))
                    if verdict:
                        changes[v["name"]] = verdict
        return changes

    def _customization_sync(self, vm_id: str, since: float | None, secrets_: list[str]) -> dict:
        si = self._soap()
        return customization.read(si.content, self._vm(si, vm_id), since=since, secrets=secrets_)

    def _windows_template_check_sync(self, vm_id: str, admin: guest.RangeAccount | None) -> dict:
        """The template-login check of a Windows VM whose Sysprep finished after the build
        (#255). ``proven`` by a login as its own Administrator (``admin``), else a refusal is
        inconclusive. Never raises."""
        try:
            si = self._soap()
            vm = self._vm(si, vm_id)

            def session_for(creds):
                return guest.GuestSession(si.content, vm, creds, poll=self._guest_poll)

            proven = admin is not None and session_for(
                guest.GuestCredentials("windows", admin.username, admin.password)).try_login() is True
            return credcheck.check(session_for, "windows", proven=proven)
        except Exception as exc:  # noqa: BLE001 — never a pass
            return {"status": credcheck.INCONCLUSIVE, "detail": f"not checked ({type(exc).__name__})"}

    async def _rollback(self, si, range_id: str, networks: list[dict], errors: list[str]) -> None:
        """Best effort: remove what a failed build created (its VMs already removed themselves)."""
        try:
            await asyncio.to_thread(self._teardown_sync, si, range_id, [n for n in networks if n.get("portgroup")])
        except Exception as exc:
            errors.append(f"cleanup after the failed build: {exc}")

    @staticmethod
    def _build_record(vm_def: dict) -> dict:
        """What the VM was built from, for its range record (ADR 0013 §2, read by the S6
        certification record): the promoted build, or that it was not one."""
        build = vm_def["template_build"]
        keys = ("catalogue_id", "build_id", "checksum", "build_date", "iso_checksum") if build.get("promoted") \
            else ("catalogue_id", "allowed_by")
        return {"promoted": bool(build.get("promoted")), **{k: build.get(k, "") for k in keys},
                "source": vm_def["_source"]}

    async def _provision_one_vm(self, client: httpx.AsyncClient, si, site: dict, vm_def: dict) -> dict:
        """Create one VM (OVF item or inventory clone), wire and customize it, power it on."""
        vm_id = None
        async with self._semaphore:
            try:
                # A promoted build: exactly its copy on the placed host, never a library item
                # (ADR 0013 §2). Let through unpromoted: its own host's copy, else the library
                # item, else the plain template.
                item = None if vm_def["template_build"].get("promoted") or vm_def["_source"] != \
                    vm_def["template_name"] else await self._find_library_item(client, vm_def["template_name"])
                if item:
                    vm_def["_source"] = f"library:{vm_def['template_name']}"
                    vm_id = await self._deploy_ovf(client, item, vm_def, site)
                    vm = self._vm(si, vm_id)
                    await asyncio.to_thread(self._reconfigure_sync, si, vm, vm_def)
                else:
                    vm = await asyncio.to_thread(self._clone_sync, si, site, vm_def)
                    vm_id = vm._moId
                await self._tag_vm(vm_def, vm_id)
                await asyncio.to_thread(self._customize_sync, si, vm, vm_def)
                power_asked_at = time.time()  # nothing Sysprep finished before this is this boot's
                await self._power_action(client, vm_id, "start")
            except Exception:
                if vm_id:  # half-built: do not leave it behind unrecorded
                    with contextlib.suppress(Exception):
                        await self._delete_vm(client, vm_id)
                raise
        powered_on, powered_on_at = time.monotonic(), time.time()
        identity = await self._identity(si, vm_def, vm_id)  # as built, before anything else runs on it (S5)
        guest_net = await self._await_guest_network(client, vm_def, vm_id)
        ready = vsphere_readiness.assess(vm_def, guest_net)
        if customization.applies(vm_def):  # #255: Sysprep's outcome, every Windows VM
            sysprep = customization.assess(await self._await_customization(
                si, vm, vm_def, powered_on, powered_on_at, power_asked_at))
            # Sysprep's own error text, already scrubbed by read(): masked again with the build's secrets.
            if detail := sysprep["customization"].get("detail"):
                sysprep["customization"]["detail"] = self._redact(detail, vm_def)
            ready["customization"] = sysprep["customization"]
            for key in ("not_ready", "warning"):
                if sysprep[key]:
                    ready[key] = "; ".join(t for t in (ready[key], self._redact(sysprep[key], vm_def)) if t)
        mgmt_ip = vm_def.get("_mgmt_ip") if any(n.get("noise") for n in vm_def["nics"]) else None
        host, datastore = vm_def["_placement"]
        extra = {key: ready[key] for key in ("not_ready", "warning", "ip_check", "customization") if ready.get(key)}
        if (cfg := vm_def.get("_pfsense") or vm_def.get("_vyos")) is not None:
            extra[getattr(cfg, "product", "vyos")] = cfg.summary()  # pfsense | opnsense | vyos
        if mgmt_ip:
            extra["mgmt_ip"] = mgmt_ip  # the noise controller reaches the agent here
        return {
            **extra,
            "vm_id": vm_id,
            "name": vm_def["name"],
            "node_id": vm_def.get("node_id", ""),
            "status": "running",
            # What VMware Tools reports, never the plan when Tools says otherwise (vsphere_readiness).
            "ip": ready["ip"],
            "planned_ip": ready["planned_ip"],
            "guest_ips": ready["guest_ips"],
            "ip_verified": ready["ip_verified"],
            "nics": [
                {"network": ref["name"], "ip": nic.get("ip", ""), "mac": nic.get("mac", "")}
                for nic, ref in zip(vm_def["nics"], vm_def["_refs"], strict=True)
            ],
            "host": host.name if host else "",
            "datastore": getattr(datastore, "name", ""),
            "template": vm_def["_source"],  # template_copies.names_in_use
            "template_build": self._build_record(vm_def),
            "identity": identity,  # independently identified inventory (range certification S5)
            "tools_ready": guest_net["tools_ready"],
            "family": infra.os_family(vm_def),  # windows | linux | appliance (credential rotation)
        }

    @staticmethod
    def _vm_rows_sync(si) -> list[tuple[str, str, str, bool]]:
        """(vm id, name, annotation, is template) of every VM in the inventory, read in one
        property-collector pass (attribute access would be a round trip per VM per property)."""
        return [(vm_id, str(p.get("name") or ""), str(p.get("config.annotation") or ""), bool(p.get("config.template")))
                for vm_id, p in _inventory_props(si, ("name", "config.annotation", "config.template")).items()]

    async def find_vms(self, name_prefix: str) -> list[dict]:
        """VMs whose name, or whose range-qualified name ``<range id>-<name>``, starts with
        ``name_prefix``, as ``[{"vm_id", "name", "range_id"}]``. ``range_id`` comes from the
        annotation a build sets (RANGE_TAG), '' for a VM TrueNorth did not tag. Templates are
        never returned: reconciliation deletes what this finds."""
        if SmartConnect is None:
            raise RuntimeError("pyvmomi is required to list vSphere VMs (pip install pyvmomi)")

        def listing():
            with self._vim() as si:
                return self._vm_rows_sync(si)

        out = []
        for vm_id, name, annotation, template in await asyncio.to_thread(listing):
            rid = annotated_range(annotation)
            qualified = f"{rid}-{name}" if rid else name
            if not template and (name.startswith(name_prefix) or qualified.startswith(name_prefix)):
                out.append({"vm_id": vm_id, "name": name, "range_id": rid})
        return out

    async def prune_template_copies(self, policy: template_copies.PrunePolicy, *, dry_run: bool = True) -> dict:
        """Delete host-local template copies (``<id>@<host>-<yyyymmdd>``) of images with a
        promoted build that no build record still needs and no range records
        (``template_copies.prunable``, ADR 0013 §2). ``dry_run`` (the default) only reports.
        Only inventory *templates* whose names parse as copies are candidates; plain templates
        and VMs never are. Each is re-checked just before it is destroyed: still that template,
        and not stamped as a protected build.

        Returns ``{"deletable", "kept_in_use", "deleted", "errors", "dry_run"}`` (names)."""
        if SmartConnect is None:
            raise RuntimeError("pyvmomi is required to prune vSphere template copies (pip install pyvmomi)")

        def run() -> dict:
            with self._vim() as si:
                ids: dict[str, list[str]] = {}
                notes: dict[str, str | None] = {}
                for vm_id, name, annotation, template in self._vm_rows_sync(si):
                    if template and template_copies.parse(name):
                        ids.setdefault(name, []).append(vm_id)
                        # Two templates of one name: kept when either carries a protected stamp.
                        if template_copies.stamp_of(notes.get(name)) not in policy.builds:
                            notes[name] = annotation
                deletable, kept = template_copies.prunable(notes, policy)
                deleted: list[str] = []
                errors: list[str] = []
                for name in [] if dry_run else deletable:
                    for vm_id in ids[name]:
                        try:
                            vm = self._vm(si, vm_id)
                            if vm.name != name or not vm.config.template:
                                errors.append(f"{name} ({vm_id}): no longer that template, left alone")
                                continue
                            if template_copies.stamp_of(getattr(vm.config, "annotation", None)) in policy.builds:
                                errors.append(f"{name} ({vm_id}): stamped as a protected build, left alone")
                                continue
                            self._wait(vm.Destroy_Task(), si)
                            deleted.append(name)
                            logger.warning("Deleted stale template copy %r (%s)", name, vm_id)
                        except Exception as exc:  # noqa: BLE001 -- one failure does not stop the rest
                            errors.append(f"{name} ({vm_id}): {exc}")
                return {"deletable": deletable, "kept_in_use": kept, "deleted": deleted, "errors": errors,
                        "dry_run": dry_run}

        return await asyncio.to_thread(run)

    async def _delete_range_tags(self, range_id: str) -> None:
        """The range's tn-range tag, once its VMs are gone. Best effort, like the tagging."""
        if not self._tags_enabled:
            return
        try:
            async with self._client() as client:  # a slow tagging service never holds up a destroy
                gone = await asyncio.wait_for(self._tag_client(client).delete_range_tags(range_id),
                                              4 * self._tag_timeout)
            if gone:
                logger.info("range %s: vCenter tag(s) %s deleted", range_id, ", ".join(gone))
        except Exception as exc:  # noqa: BLE001 -- a stale tag is untidy, not a failed destroy
            logger.warning("range %s: its tn-range vCenter tag was not deleted: %s", range_id,
                           str(exc) or type(exc).__name__)

    async def destroy(
        self,
        range_id: str,
        provision_output: dict,
    ) -> DestroyResult:
        """Delete the VMs, then (only once they are all gone) the port groups and folder.

        Only VMs that are this range's on this vCenter are deleted (``_screen``): by their
        recorded moid *and* instance UUID (range certification S5), so a renamed VM still
        goes and a reused id never does; any other recorded id is left alone and reported
        as an error, so the port groups stay too and the range is not recorded destroyed
        while its real VMs may be elsewhere.

        Then (S5, ``teardown`` in the result): anything else carrying this range's
        annotation, ``tn-range`` tag or port group owner mark is listed as unidentified and
        **not** deleted (an administrator may confirm it by moid: ``delete_confirmed``);
        the range's tag is kept while an unidentified VM carries it. Last, every recorded id
        is looked for again: VMs, their disk files, port groups, the folder."""
        start = time.monotonic()
        errors: list[str] = []
        vms = provision_output.get("vms", [])
        portgroups = [n for n in provision_output.get("networks", []) if n.get("portgroup")]
        mirrors = bool(provision_output.get("mirrors"))

        if not vms and not portgroups and not mirrors:
            return DestroyResult(status="ok", resources_removed=0, duration_seconds=0.0)

        removed = 0
        deleted: list[dict] = []
        unidentified: list[dict] = []
        notes: list[str] = []
        report: dict | None = None
        try:
            has_ids = any(vm.get("vm_id") for vm in vms)
            if has_ids:  # only port groups left: no REST login needed
                await self._get_session()
            # One Web Services session for the whole teardown: the deletes, the scan, the check.
            async with self._soap_scope(always=True):
                try:
                    removed = await self._destroy_in_scope(range_id, provision_output, vms, portgroups, mirrors,
                                                           has_ids, errors, deleted, unidentified, notes)
                finally:
                    report = await self._teardown_report(provision_output, deleted, unidentified, notes)
        except Exception as exc:
            errors.append(str(exc))
            removed = removed or len(deleted)  # what was deleted before it stopped

        return DestroyResult(
            status=outcome(removed, errors),
            resources_removed=removed,
            duration_seconds=time.monotonic() - start,
            errors=errors,
            # Without a session (could not connect) the report says nothing could be checked.
            teardown=report or await self._teardown_report(provision_output, deleted, unidentified, notes),
        )

    async def _destroy_in_scope(self, range_id: str, provision_output: dict, vms: list[dict], portgroups: list[dict],
                                mirrors: bool, has_ids: bool, errors: list[str], deleted: list[dict],
                                unidentified: list[dict], notes: list[str]) -> int:
        """``destroy``'s work inside its session; fills ``errors``, ``deleted``,
        ``unidentified`` and ``notes`` as it goes (the report is written even if it raises)."""
        removed = 0
        async with self._client() as client:
            recorded_ids = [vm["vm_id"] for vm in vms if vm.get("vm_id")]
            vms, skipped, missing = await self._screen(range_id, vms)
            errors += skipped
            if recorded_ids and set(recorded_ids) <= missing and not provision_output.get("connection"):
                # Nothing recorded which vCenter built it, and none of its VMs is here: this
                # is likely not that vCenter (the tenant's preferred connection changed).
                # Counting them as deleted would record the range destroyed and orphan them.
                if self.on_unrecorded_vms_missing is not None:
                    try:
                        await asyncio.to_thread(self.on_unrecorded_vms_missing, range_id)
                    except Exception as exc:  # noqa: BLE001 — the refusal below stands either way
                        logger.warning("range %s: could not mark its VMs missing: %s", range_id, exc)
                # Its ids are not here, so finding none of them proves nothing (S5 report).
                notes.append(f"none of the recorded VMs is on {self._base_url}; this may not be the vCenter that "
                             "built the range, so the absence of its recorded ids verifies nothing")
                raise RuntimeError(
                    f"none of the range's {len(recorded_ids)} recorded VMs exist on {self._base_url}, and the "
                    "range does not record which vCenter built it; refusing to call it destroyed. Point the "
                    "tenant's connection at the vCenter that built it, or delete its VMs there by hand")
            ids = [vm["vm_id"] for vm in vms if vm.get("vm_id") and vm["vm_id"] not in missing]
            names = {vm["vm_id"]: vm.get("name", "") for vm in vms if vm.get("vm_id")}
            results = await asyncio.gather(*(self._delete_vm(client, i) for i in ids), return_exceptions=True)
            for vm_id, res in zip(ids, results, strict=True):
                if isinstance(res, Exception):
                    errors.append(f"VM {vm_id}: {self._redact(res)}")
                else:
                    deleted.append({"kind": "vm", "id": vm_id, "name": names.get(vm_id, "")})
            removed += len(deleted) + sum(1 for vm in vms if vm.get("vm_id") in missing)  # gone counts as deleted
        found, scan_notes = await self._unidentified(range_id, provision_output, tags=has_ids)
        unidentified += found
        notes += scan_notes
        # A port group still has VMs on it until every VM is gone.
        if (portgroups or mirrors) and not errors:
            with self._vim() as si:
                removed += await asyncio.to_thread(self._teardown_sync, si, range_id, portgroups, mirrors, notes)
        # The tag stays while an unidentified VM carries it: it is the evidence of the mark.
        if has_ids and not errors and not any("tag" in u.get("marks", ()) for u in unidentified):
            await self._delete_range_tags(range_id)
        return removed

    # ------------------------------------------------------------------ #
    # Range certification S5: inventory, unidentified items, drift
    # ------------------------------------------------------------------ #

    supports_inventory = True

    async def _identity(self, si, vm_def: dict, vm_id: str) -> dict | None:
        """The VM's independently identified inventory, read once it is built. Best effort:
        a VM whose identity could not be read is still the range's, by the older rule
        (annotation, name), and its teardown report says so (range_inventory.recorded)."""
        try:
            return await asyncio.to_thread(inventory.read_identity, si, vm_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("VM %s (%s): identity not recorded: %s", vm_def.get("name"), vm_id, self._redact(exc))
            return None

    def _range_inventory_sync(self, site: dict, physical: dict, allocations: dict) -> dict:
        """The range-wide part of the inventory: its folder, VLAN and MAC reservations."""
        folder = site.get("folder")
        return {
            "folder": {"moid": getattr(folder, "_moId", None), "name": str(getattr(folder, "name", "") or "")}
            if folder is not None else None,
            "vlans": {str(k): int(v) for k, v in (physical or {}).items()},
            "macs": dict(allocations.get("nic_macs") or {}),
            "recorded_at": datetime.now(UTC).isoformat(),
        }

    async def _tagged_vms(self, range_id: str) -> tuple[set[str], str | None]:
        """Moids the range's tn-range tag is on, and a note when that could not be read."""
        found, notes = await self._tagged_many([range_id])
        return found[range_id], notes.get(range_id)

    async def _tagged_many(self, range_ids: list[str]) -> tuple[dict[str, set[str]], dict[str, str]]:
        """``_tagged_vms`` of many ranges through one tag client (the categories and the
        tn-range tags are listed once, then one attached-objects call per range)."""
        found: dict[str, set[str]] = {rid: set() for rid in range_ids}
        notes: dict[str, str] = {}
        if not self._tags_enabled or not range_ids:
            return found, notes
        try:
            await self._get_session()
            async with self._client() as client:
                tagger = self._tag_client(client)
                for rid in range_ids:
                    try:
                        found[rid] = await asyncio.wait_for(tagger.range_tagged_vms(rid), 4 * self._tag_timeout)
                    except Exception as exc:  # noqa: BLE001 -- the annotation scan still runs; the gap is reported
                        notes[rid] = f"VMs carrying the range's tn-range tag: {str(exc) or type(exc).__name__}"
        except Exception as exc:  # noqa: BLE001 -- no session or client at all
            notes.update(dict.fromkeys(range_ids, f"VMs carrying the range's tn-range tag: {str(exc) or type(exc).__name__}"))
        return found, notes

    def _marks_sync(self, si) -> tuple[dict[str, dict], dict[str, dict]]:
        """Every VM's marks and every range-switch port group's owner mark: two calls, shared
        by every range they are matched against (``inventory.unidentified``)."""
        dvs = self._find(si, vim.DistributedVirtualSwitch, self._dvs_name)
        return inventory.all_vm_marks(si), inventory.portgroup_marks(si, dvs)

    def _unidentified_sync(self, si, range_id: str, inv: dict, tagged: set[str]) -> list[dict]:
        """What carries this range's mark (annotation, tn-range tag, port group owner mark)
        but is not a recorded item: listed, never deleted here."""
        return inventory.unidentified(range_id, inv, *self._marks_sync(si), tagged, annotated_range)

    async def _unidentified(self, range_id: str, output: dict, *, tags: bool = True) -> tuple[list[dict], list[str]]:
        """``_unidentified_sync`` with the tag lookup (``tags``: a range that recorded no VM
        has had no VM tagged by TrueNorth, and needs no REST login for it)."""
        tagged, note = await self._tagged_vms(range_id) if tags else (set(), None)
        notes = [note] if note else []
        try:
            with self._vim() as si:
                found = await asyncio.to_thread(self._unidentified_sync, si, range_id,
                                                range_inventory.recorded(output), tagged)
        except Exception as exc:  # noqa: BLE001 -- reported as not checked, never guessed
            return [], [*notes, f"the scan for unrecorded items carrying the range's mark failed: {self._redact(exc)}"]
        return found, notes

    async def _teardown_report(self, output: dict, deleted: list[dict], unidentified: list[dict],
                               notes: list[str]) -> dict:
        """What the teardown did and what is left, for the record (range_inventory.teardown_status)."""
        inv = range_inventory.recorded(output)
        left: list[dict] = []
        not_checked = list(notes)
        try:
            with self._vim() as si:
                left, more = await asyncio.to_thread(inventory.remaining, si, inv, lambda t: self._wait(t, si))
            not_checked += more
        except Exception as exc:  # noqa: BLE001
            not_checked.append(f"the check for recorded ids that remain failed: {self._redact(exc)}")
        return {"verified_at": datetime.now(UTC).isoformat(), "vcenter": self._base_url, "legacy": bool(inv["gaps"]),
                "gaps": inv["gaps"], "deleted": deleted, "remaining": left, "unidentified": unidentified,
                "not_checked": not_checked}

    async def live_inventory(self, range_id: str, provision_output: dict) -> dict:
        """The drift reading (range_inventory.compare) of one range."""
        return (await self.live_inventories([(range_id, provision_output)]))[range_id]

    def _live_many_sync(self, si, items: list[tuple[str, dict]], tagged: dict[str, set[str]]) -> dict[str, dict]:
        """Every range of ``items`` read in the same handful of calls: all their recorded VMs
        in one property-collector call (devices, snapshot tree, config), their port groups in
        one more, and the VM and port group marks once for all of them (unidentified items)."""
        invs = {rid: range_inventory.recorded(out) for rid, out in items}
        reading = inventory.live(si, [v for inv in invs.values() for v in inv["vms"]],
                                 [p for inv in invs.values() for p in inv["portgroups"]])
        vm_marks, pg_marks = self._marks_sync(si)
        out = {}
        for rid, inv in invs.items():
            out[rid] = {"vms": {v["moid"]: reading["vms"][v["moid"]] for v in inv["vms"]},
                        "portgroups": {p["moid"]: reading["portgroups"][p["moid"]] for p in inv["portgroups"]
                                       if p["moid"] in reading["portgroups"]},
                        "unidentified": inventory.unidentified(rid, inv, vm_marks, pg_marks, tagged.get(rid, set()),
                                                               annotated_range),
                        "errors": list(reading["errors"])}
        return out

    async def live_inventories(self, items: list[tuple[str, dict]]) -> dict[str, dict]:
        """``live_inventory`` of many ranges on this vCenter at once (the drift sweep), in one
        Web Services session (the scheduled run's, inside ``session()``). A range whose tag
        could not be read says so in its ``errors`` (its tagged items are then not seen)."""
        tagged, notes = await self._tagged_many([rid for rid, _ in items])

        def read(si):
            return self._live_many_sync(si, items, tagged)

        if self._scope is not None:  # inside a scheduled run's session(): its one login
            out = await asyncio.to_thread(read, self._scope_si(self._scope))
        else:
            def own():
                with self._vim() as si:
                    return read(si)
            out = await asyncio.to_thread(own)
        for rid, note in notes.items():
            out[rid]["errors"].append(note)
        return out

    def _delete_listed_sync(self, si, range_id: str, item: dict, tagged: set[str]) -> tuple[str, str]:
        """Delete one confirmed unidentified item only if it is still exactly what was listed
        *and* still carries this range's mark, read now: a VM its instance UUID and (its
        annotation names the range, or ``tagged``: the range's tn-range tag is on it now); a
        port group its key and owner mark. Returns ``("deleted" | "gone" | "skipped", why)``."""
        moid = str(item.get("moid") or "")
        if item.get("kind") == "vm":
            props, missing = inventory.read_props(si, vim.VirtualMachine, [moid], inventory.MARK_PATHS)
            if moid in missing:
                return "gone", f"{moid} was no longer in vCenter"
            seen = props[moid]
            if not item.get("instance_uuid") or not range_inventory.vm_owned(
                    {"instance_uuid": item["instance_uuid"]}, _str_or_none(seen.get("config.instanceUuid"))):
                return "skipped", f"{moid} now has instance UUID {seen.get('config.instanceUuid')}, not the listed one"
            if seen.get("config.template"):
                return "skipped", f"{moid} is a template"
            if annotated_range(str(seen.get("config.annotation") or "")) != range_id and moid not in tagged:
                return "skipped", (f"{moid} no longer carries this range's annotation or tag; the confirmation "
                                   "was for an item marked as this range's")
            vm = vim.VirtualMachine(moid, si._stub)
            if str(vm.runtime.powerState) != "poweredOff":
                self._wait(vm.PowerOffVM_Task(), si)
            self._wait(vm.Destroy_Task(), si)
            return "deleted", ""
        if item.get("kind") == "portgroup":
            props, missing = inventory.read_props(si, vim.dvs.DistributedVirtualPortgroup, [moid],
                                                  inventory.PORTGROUP_PATHS)
            if moid in missing:
                return "gone", f"{moid} was no longer in vCenter"
            seen = props[moid]
            if str(seen.get("key")) != str(item.get("key")) or \
                    inventory.description(seen.get("config")) != live_vlans.owner_mark(range_id):
                return "skipped", f"{moid} is no longer the listed port group (key or owner mark changed)"
            self._wait(vim.dvs.DistributedVirtualPortgroup(moid, si._stub).Destroy_Task(), si)
            return "deleted", ""
        return "skipped", f"{moid}: {item.get('kind')!r} items are not deleted by TrueNorth"

    async def delete_confirmed(self, range_id: str, provision_output: dict, items: list[dict]) -> dict:
        """Delete the unidentified items an administrator confirmed by moid, each re-checked
        first against vCenter, the range's mark included (``_delete_listed_sync``); then
        remove the range's recorded VM folder if that left it empty (a folder holding
        anything is never touched), and report the teardown again from scratch."""
        if SmartConnect is None:
            raise RuntimeError("pyvmomi is required to delete vSphere items (pip install pyvmomi)")
        done: list[dict] = []
        skipped: list[dict] = []
        await self._get_session()  # the dialect, and the tag lookups
        async with self._soap_scope(always=True):
            si = self._soap()
            for item in items:
                # The range's tag, read again right before this item: one listed for its tag must
                # still carry it. Unreadable: only the annotation counts (the item is left alone
                # unless annotated, and says why).
                tagged, tag_note = await self._tagged_vms(range_id) if item.get("kind") == "vm" else (set(), None)
                try:
                    outcome_, why = await asyncio.to_thread(self._delete_listed_sync, si, range_id, item, tagged)
                except Exception as exc:  # noqa: BLE001 -- one failure does not stop the others
                    outcome_, why = "skipped", f"delete failed: {self._redact(exc)}"
                if outcome_ == "deleted":
                    done.append({k: item.get(k) for k in ("kind", "moid", "name", "instance_uuid", "key")})
                    logger.warning("range %s: deleted confirmed unidentified %s %s (%s)", range_id, item.get("kind"),
                                   item.get("moid"), item.get("name"))
                else:
                    if tag_note and outcome_ == "skipped" and "tag" in (item.get("marks") or []):
                        why = f"{why}; {tag_note}"
                    skipped.append({"moid": item.get("moid"), "why": why, "outcome": outcome_})
            folder = (provision_output.get("inventory") or {}).get("folder") or {}
            if folder.get("moid"):
                await asyncio.to_thread(self._drop_empty_folder_sync, si, folder["moid"])
            unidentified, notes = await self._unidentified(range_id, provision_output)
            if not any("tag" in u.get("marks", ()) for u in unidentified):
                await self._delete_range_tags(range_id)
            report = await self._teardown_report(provision_output, [], unidentified, notes)
        return {**report, "confirmed_deleted": done, "confirm_skipped": skipped}

    def _drop_empty_folder_sync(self, si, moid: str) -> None:
        """The range's recorded folder, once a confirmed delete left it empty. Anything still
        in it keeps it (and ``remaining`` reports it)."""
        with contextlib.suppress(vmodl.fault.ManagedObjectNotFound):
            ref = vim.Folder(moid, si._stub)
            if not ref.childEntity:
                self._wait(ref.Destroy_Task(), si)

    # The power state each action leaves a VM in.
    _POWER_TARGET = {"stop": "POWERED_OFF", "start": "POWERED_ON"}

    async def _power_all(self, range_id: str, provision_output: dict, action: str) -> tuple[int, list[str]]:
        """Power every VM of a range. A VM already in the wanted state counts as done (a
        retry after a partial attempt, a guest that shut itself down, a VM powered by hand);
        vCenter answers ALREADY_IN_DESIRED_STATE for one that got there between the read
        and the action. A VM with no recorded id is an error, never silently counted; so is
        one that is not this range's on this vCenter (``_screen``), which is left alone."""
        recorded = provision_output.get("vms", [])
        skipped: list[str] = []

        async def one(client, vm: dict) -> None:
            if not vm.get("vm_id"):
                raise RuntimeError("no vm_id recorded")
            if await self._power_state(client, vm["vm_id"]) == self._POWER_TARGET[action]:
                return
            try:
                await self._power_action(client, vm["vm_id"], action)
            except httpx.HTTPStatusError as exc:
                if "ALREADY_IN_DESIRED_STATE" not in exc.response.text:
                    raise

        try:
            has_ids = any(vm.get("vm_id") for vm in recorded)
            if has_ids:  # nothing to power: no login
                await self._get_session()
            async with self._soap_scope(always=has_ids), self._client() as client:
                vms, skipped, _ = await self._screen(range_id, recorded)
                results = await asyncio.gather(*(one(client, vm) for vm in vms), return_exceptions=True)
        except Exception as exc:  # could not log in, connect or check ownership: nothing was powered
            return 0, [self._redact(exc)]
        errors = skipped + [f"VM {vm.get('name')}: {self._redact(res)}"
                            for vm, res in zip(vms, results, strict=True) if isinstance(res, Exception)]
        return len(recorded) - len(errors), errors

    async def stop(
        self,
        range_id: str,
        provision_output: dict,
    ) -> StopResult:
        start = time.monotonic()
        stopped, errors = await self._power_all(range_id, provision_output, "stop")
        return StopResult(
            status=outcome(stopped, errors),
            vms_stopped=stopped,
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )

    async def start(
        self,
        range_id: str,
        provision_output: dict,
    ) -> StartResult:
        start = time.monotonic()
        started, errors = await self._power_all(range_id, provision_output, "start")
        return StartResult(
            status=outcome(started, errors),
            vms_started=started,
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )

    async def snapshot(
        self,
        range_id: str,
        provision_output: dict,
        name: str,
    ) -> SnapshotResult:
        start = time.monotonic()
        taken: dict[str, str] = {}  # VM moid -> the snapshot's moid (range certification S5: drift)
        snapped, errors, _ = await self._vim_op(range_id, provision_output, self._snapshot_sync, name, taken)
        return SnapshotResult(
            status=outcome(snapped, errors),
            snapshot_name=name,
            vms_snapped=snapped,
            duration_seconds=time.monotonic() - start,
            errors=errors,
            snapshot_ids=taken,
        )

    async def restore(
        self,
        range_id: str,
        provision_output: dict,
        name: str,
        power_on: bool,
    ) -> RestoreResult:
        start = time.monotonic()
        restored, errors, reverted = await self._vim_op(range_id, provision_output, self._restore_sync, name, power_on)
        return RestoreResult(
            status=outcome(restored, errors),
            snapshot_name=name,
            vms_restored=restored,
            vms_reverted=len(reverted),
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )

    async def delete_snapshot(
        self,
        range_id: str,
        provision_output: dict,
        name: str,
    ) -> SnapshotDeleteResult:
        start = time.monotonic()
        cleaned, errors, _ = await self._vim_op(range_id, provision_output, self._delete_snapshot_sync, name)
        return SnapshotDeleteResult(
            status=outcome(cleaned, errors),
            snapshot_name=name,
            vms_cleaned=cleaned,
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )

    async def health_check(
        self,
        range_id: str,
        provision_output: dict,
    ) -> HealthResult:
        start = time.monotonic()
        errors: list[str] = []
        vm_statuses: list[dict] = []  # same shape as the other backends: one dict per VM
        vms = provision_output.get("vms", [])
        await self._confirm(range_id, provision_output)  # a range with nothing recorded: record it

        try:
            if any(vm.get("vm_id") for vm in vms):  # nothing to read: no login
                await self._get_session()
            async with self._soap_scope(), self._client() as client:
                for vm in vms:
                    vm_id = vm.get("vm_id")
                    if not vm_id:
                        continue
                    try:
                        state = (await self._power_state(client, vm_id)).lower()
                    except Exception as exc:
                        state = "error"
                        errors.append(f"VM {vm.get('name', vm_id)}: {exc}")
                    vm_statuses.append(
                        {"vm_id": vm_id, "name": vm.get("name", ""), "status": state, "healthy": state == "powered_on"}
                    )
        except Exception as exc:
            errors.append(str(exc))

        healthy = not errors and all(s["healthy"] for s in vm_statuses)
        return HealthResult(
            healthy=healthy,
            status="ok" if healthy else ("degraded" if vm_statuses else "unhealthy"),
            vm_statuses=vm_statuses,
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # Scheduled reads: one login per run, metrics in one call per range
    # ------------------------------------------------------------------ #

    @contextlib.asynccontextmanager
    async def session(self):
        """One vCenter login for a whole scheduled run, logged out at its end.

        Inside it, collect_metrics shares one pyVmomi session (opened on first use), and
        the REST token health_check gets is reused for every range and then deleted. A
        run every 30 to 60 seconds used to leave a REST session behind each time, and
        sessions are capped per vCenter.
        """
        self._scope = {"si": None, "error": None, "closed": False, "lock": threading.Lock()}
        try:
            yield self
        finally:
            scope, self._scope = self._scope, None
            await asyncio.to_thread(self._close_scope, scope)
            if self._session_token:
                await self._logout()

    @staticmethod
    def _close_scope(scope: dict) -> None:
        with scope["lock"]:  # waits for a login still in progress, so it is not leaked
            scope["closed"] = True
            si, scope["si"] = scope["si"], None
        if si is not None:
            with contextlib.suppress(Exception):
                Disconnect(si)

    def _scope_si(self, scope: dict):
        """The run's pyVmomi session, logging in once. A failed login is not retried in the run."""
        with scope["lock"]:
            if scope["closed"]:
                raise RuntimeError("the run's vCenter session has ended")
            if scope["error"]:
                raise RuntimeError(scope["error"])
            if scope["si"] is None:
                try:
                    scope["si"] = self._connect(self._metrics_timeout)
                except Exception as exc:
                    scope["error"] = f"vCenter login failed: {self._redact(exc)}"
                    raise RuntimeError(scope["error"]) from exc
            return scope["si"]

    def _metrics_sync(self, vm_ids: list[str]) -> tuple[dict[str, dict], set[str]]:
        paths = METRIC_PATHS + OWNER_PATHS  # the figures and the ownership check, in one call
        scope = self._scope
        if scope is not None:
            return _read_vm_metrics(self._scope_si(scope), vm_ids, paths)
        si = self._connect(self._metrics_timeout)  # a call outside a run: a session of its own
        try:
            return _read_vm_metrics(si, vm_ids, paths)
        finally:
            with contextlib.suppress(Exception):
                Disconnect(si)

    async def _logout(self) -> None:
        token, self._session_token = self._session_token, None
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, verify=self._verify_ssl, timeout=10.0, transport=self._transport
            ) as client:
                path = "/rest/com/vmware/cis/session" if self._dialect == "rest" else "/api/session"
                await client.delete(path, headers={"vmware-api-session-id": token})
        except Exception as exc:  # noqa: BLE001 — the session expires on its own after 30 idle minutes
            logger.debug("vCenter REST logout failed: %s", exc)

    async def collect_metrics(
        self,
        range_id: str,
        provision_output: dict,
    ) -> MetricsResult:
        """Power state, Tools status and quickStats of every VM of the range, read in one call.

        A VM vCenter no longer has is reported with power_state ``notFound`` and an error
        (the range is then ``partial``); a VM with no recorded id is an error. Nothing is
        estimated: a value vCenter did not send is None.

        Only the range's own VMs are reported, by the rule a delete or power operation
        uses (``_screen``), read in the same call. A recorded id vCenter now gives to
        another range's VM, or to one TrueNorth did not build, is left out (no figures at
        all) with an error, so no other tenant's load is ever shown as this range's.
        """
        start = time.monotonic()
        vms = provision_output.get("vms", [])
        errors = [f"VM {vm.get('name')}: no vm_id recorded" for vm in vms if not vm.get("vm_id")]
        ids = [vm["vm_id"] for vm in vms if vm.get("vm_id")]
        out: list[dict] = []
        if ids:
            try:
                props, missing = await asyncio.to_thread(self._metrics_sync, ids)
            except Exception as exc:
                errors.append(f"could not read VM metrics from vCenter: {self._redact(exc)}")
            else:
                for vm in vms:
                    if not vm.get("vm_id"):
                        continue
                    found = vm["vm_id"] not in missing
                    foreign = found and _not_ours(range_id, vm, props.get(vm["vm_id"], {}), self._base_url)
                    if foreign:
                        logger.warning("range %s: %s; metrics not read", range_id, foreign)
                        errors.append(f"{foreign}; not read")
                        continue
                    out.append(_vm_metrics(vm, props.get(vm["vm_id"], {}) if found else None))
                    if not found:
                        errors.append(f"VM {vm.get('name') or vm['vm_id']}: not found in vCenter")
        read = sum(1 for v in out if v["power_state"] != "notFound")
        return MetricsResult(
            status=outcome(read, errors) if vms else "ok",
            vms=out,
            source="vsphere",
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )
