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
guest operations (vsphere_guest.py), from the depot behind the uplink.

Each pfSense VM gets a per-range config.xml (worker/pfsense_config.py: zone gateways,
WAN, NAT, the template's firewall rules, depot-only egress) in its guestinfo before its
first power-on; the template's boot script applies it.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
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

from .. import pfsense_config, software_catalogue, uplink_pool, vlan_pool
from . import vsphere_guest as guest
from . import vsphere_infra as infra
from .base import BaseProvisioner
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
# Networks no range VM may ever attach to (comma-separated names).
VSPHERE_MGMT_NETWORK: str = os.environ.get("VSPHERE_MGMT_NETWORK", "dPG-TN-MGMT")
# Empty, or no item named like the template: clone the inventory VM template instead.
VSPHERE_CONTENT_LIBRARY: str = os.environ.get("VSPHERE_CONTENT_LIBRARY", "")
VSPHERE_VERIFY_SSL: bool = os.environ.get("VSPHERE_VERIFY_SSL", "false").lower() == "true"
VSPHERE_CONCURRENCY: int = int(os.environ.get("VSPHERE_CONCURRENCY", "4"))
VSPHERE_TOOLS_TIMEOUT: int = int(os.environ.get("VSPHERE_TOOLS_TIMEOUT", "120"))
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
# "spread" (each VM on the host with most vCPU headroom), "per-range-host" (whole range
# on one host) or "cluster" (cluster resource pool + VSPHERE_DATASTORE, DRS places).
VSPHERE_PLACEMENT: str = os.environ.get("VSPHERE_PLACEMENT", "spread").lower()
VSPHERE_RANGE_FOLDER: str = os.environ.get("VSPHERE_RANGE_FOLDER", "truenorth/ranges")
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
# No new work starts once a provision has run this long: Celery redelivers a task that is
# still unacknowledged after its visibility timeout (celery_app.py, 3600s), and a second
# copy of a range build is the one thing worse than a slow one.
VSPHERE_PROVISION_BUDGET: int = int(os.environ.get("VSPHERE_PROVISION_BUDGET", "3300"))
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


def _names(csv: str) -> set[str]:
    return {n.strip() for n in (csv or "").split(",") if n.strip()}


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


def _read_vm_metrics(si, vm_ids: list[str]) -> tuple[dict[str, dict], set[str]]:
    """METRIC_PATHS of every VM in ``vm_ids``, in one RetrievePropertiesEx call.

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
            propSet=[pcq.PropertySpec(type=vim.VirtualMachine, all=False, pathSet=list(METRIC_PATHS))],
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


def _vm_metrics(vm: dict, props: dict | None) -> dict:
    """One MetricsResult VM entry from the properties read (None: vCenter has no such VM)."""
    def val(path):
        v = (props or {}).get(path)
        return str(v) if isinstance(v, str) else v  # pyVmomi enums are str subclasses

    return {
        "vm_id": vm.get("vm_id", ""),
        "name": vm.get("name", ""),
        "power_state": val("runtime.powerState") if props is not None else "notFound",
        "tools_status": val("guest.toolsRunningStatus"),
        "cpu_usage_mhz": val("summary.quickStats.overallCpuUsage"),
        "cpu_capacity_mhz": val("summary.runtime.maxCpuUsage"),
        "memory_active_mb": val("summary.quickStats.guestMemoryUsage"),
        "memory_configured_mb": val("summary.config.memorySizeMB"),
        "uptime_seconds": val("summary.quickStats.uptimeSeconds"),
    }


class VsphereAPIProvisioner(BaseProvisioner):
    """VMware vSphere provisioner (Automation REST API + pyVmomi).

    The REST session token is created on first use and renewed once on a 401 (vCenter
    sessions expire after 30 idle minutes, well inside a long build). Credentials come
    from the environment, or from the primary vSphere HypervisorConnection row when
    VSPHERE_URL is unset (worker.tasks passes them in through ``use_credentials``).
    """

    def __init__(self, credentials: dict | None = None) -> None:
        self._base_url = VSPHERE_URL.rstrip("/")
        self._username = VSPHERE_USERNAME
        self._password = VSPHERE_PASSWORD
        self._datacenter = VSPHERE_DATACENTER
        self._cluster = VSPHERE_CLUSTER
        self._datastore = VSPHERE_DATASTORE
        self._network = VSPHERE_NETWORK
        self._mgmt_networks = _names(VSPHERE_MGMT_NETWORK)
        self._content_library = VSPHERE_CONTENT_LIBRARY
        self._verify_ssl = VSPHERE_VERIFY_SSL
        self._semaphore = asyncio.Semaphore(VSPHERE_CONCURRENCY)
        self._tools_timeout = VSPHERE_TOOLS_TIMEOUT
        self._session_token: str | None = None
        self._login_loop = None
        self._login_gate: asyncio.Lock | None = None
        self._concurrency = max(1, VSPHERE_CONCURRENCY)
        self._snapshot_timeout = VSPHERE_SNAPSHOT_TIMEOUT
        self._switch_mode = VSPHERE_RANGE_SWITCH_MODE
        self._dvs_name = VSPHERE_RANGE_DVS
        self._vswitch = VSPHERE_RANGE_VSWITCH
        self._vlan_pool = VSPHERE_VLAN_POOL
        self._placement = VSPHERE_PLACEMENT
        self._range_folder = VSPHERE_RANGE_FOLDER.strip("/")
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
        self._budget = VSPHERE_PROVISION_BUDGET
        self._guest_poll = 5.0
        self._library_id: str | None = None
        self._library_looked_up = False
        self._transport = None  # an httpx transport for tests; None = the network
        self._metrics_timeout = VSPHERE_METRICS_TIMEOUT
        self._mirror_uplink = VSPHERE_MIRROR_UPLINK.strip()
        self._mirror_direction = VSPHERE_MIRROR_DIRECTION
        self._scope: dict | None = None  # set inside session(): the run's shared pyVmomi login
        if credentials:
            self.use_credentials(credentials)

    def use_credentials(self, creds: dict) -> None:
        """Take endpoint and login from a HypervisorConnection row (worker.tasks._hypervisor_creds)."""
        host = str(creds.get("host") or "").strip()
        if host:
            self._base_url = (host if "://" in host else f"https://{host}").rstrip("/")
        self._username = creds.get("username") or self._username
        self._password = creds.get("password") or self._password
        self._datacenter = creds.get("datacenter") or self._datacenter
        if "verify_ssl" in creds:
            self._verify_ssl = bool(creds["verify_ssl"])
        self._session_token = None

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
        """Create a vCenter session and return the session token."""
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
            resp.raise_for_status()
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

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, **kwargs):
        """One REST call; on a 401 (session expired) log in again and retry once."""
        for attempt in (1, 2):
            token = await self._get_session()
            resp = await client.request(method, f"/api{path}", headers={"vmware-api-session-id": token}, **kwargs)
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

    # ------------------------------------------------------------------ #
    # Content Library
    # ------------------------------------------------------------------ #

    async def _find_library_item(self, client: httpx.AsyncClient, template_name: str) -> str | None:
        """The library item named ``template_name``, or None (clone the inventory template)."""
        if not self._library_looked_up:
            self._library_looked_up = True
            if self._content_library:
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

    async def _power_action(self, client: httpx.AsyncClient, vm_id: str, action: str) -> None:
        """Start or stop a VM; a VM already in that state is left alone (so retries are safe)."""
        power = await self._api_get(client, f"/vcenter/vm/{vm_id}/power")
        if power.get("state") == ("POWERED_ON" if action == "start" else "POWERED_OFF"):
            return
        await self._api_post(client, f"/vcenter/vm/{vm_id}/power?action={action}")

    async def _delete_vm(self, client: httpx.AsyncClient, vm_id: str) -> None:
        """Power off (if running) then delete a VM. A VM that is already gone counts as deleted."""
        try:
            await self._power_action(client, vm_id, "stop")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return
            raise
        await self._api_delete(client, f"/vcenter/vm/{vm_id}")

    async def _wait_tools(self, client: httpx.AsyncClient, vm_id: str) -> bool:
        """Wait for VMware Tools to report running inside the VM."""
        deadline = time.monotonic() + self._tools_timeout
        while time.monotonic() < deadline:
            try:
                tools = await self._api_get(client, f"/vcenter/vm/{vm_id}/tools")
                if tools.get("run_state") == "RUNNING":
                    return True
            except Exception:
                pass
            await asyncio.sleep(5)
        return False

    async def _get_vm_ip(self, client: httpx.AsyncClient, vm_id: str) -> str | None:
        """Return the primary IPv4 address reported by VMware Tools."""
        try:
            guest = await self._api_get(client, f"/vcenter/vm/{vm_id}/guest/networking/interfaces")
            for iface in guest:
                for addr in iface.get("ip", {}).get("ip_addresses", []):
                    if addr.get("state") == "PREFERRED" and ":" not in addr.get("ip_address", ""):
                        return addr["ip_address"]
        except Exception:
            pass
        return None

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
        si = self._connect()
        try:
            yield si
        finally:
            Disconnect(si)

    def _fan_out(self, si, vm_ids: list[str], submit: Callable) -> tuple[dict[str, str], set[str]]:
        """Start ``submit(vm)``'s tasks, a batch of VSPHERE_CONCURRENCY VMs at a time.

        vCenter runs a batch side by side, so a 20-VM range does not take twenty
        snapshots' worth of wall-clock, without handing vCenter all twenty at once.
        Returns the VMs that failed with the reason, and the VMs whose tasks started.
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
        return failed, submitted

    def _snapshot_sync(self, vm_ids: list[str], name: str) -> tuple[dict[str, str], set[str]]:
        with self._vim() as si:
            return self._fan_out(
                si,
                vm_ids,
                lambda vm: [
                    vm.CreateSnapshot_Task(
                        name=name, description="TrueNorth range snapshot", memory=False, quiesce=False
                    )
                ],
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

    async def _vim_op(self, provision_output: dict, fn: Callable, *args) -> tuple[int, list[str], set[str]]:
        """Run a blocking pyVmomi operation over the range's VMs.

        Returns how many VMs it succeeded on, the errors, and the VMs it started a
        task on (for restore: the ones that may have changed).
        """
        vms = provision_output.get("vms", [])
        vm_ids = [vm["vm_id"] for vm in vms if vm.get("vm_id")]
        # A VM with no recorded id cannot be addressed, so the operation cannot cover
        # the whole range. That is a failure, not something to skip over quietly.
        errors = [f"VM {vm.get('name')}: no vm_id recorded" for vm in vms if not vm.get("vm_id")]
        touched: set[str] = set()
        if vm_ids:
            try:
                failed, touched = await asyncio.to_thread(fn, vm_ids, *args)
            except Exception as exc:  # could not connect or log in: no VM was touched
                failed = dict.fromkeys(vm_ids, str(exc))
            errors += [f"VM {vm_id}: {msg}" for vm_id, msg in failed.items()]
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

    def _eligible_hosts(self, cluster) -> list[infra.HostCapacity]:
        found = (infra.host_capacity(h) for h in cluster.host if h.name not in self._exclude_hosts)
        return [h for h in found if h is not None]

    def _guard(self, ref: dict) -> dict:
        if ref["name"] in self._mgmt_networks:
            raise RuntimeError(f"refusing to attach a range VM to management network {ref['name']!r}")
        return ref

    def _prepare_sync(self, si, range_id: str, vm_defs: list[dict], networks: list[dict], physical: dict) -> dict:
        """Placement, the range's VM folder and port groups, and the inventory templates.

        Sets ``_placement`` ((HostCapacity | None, datastore)) and ``_refs`` (one network
        ref per NIC) on every vm_def; returns the shared ``site``.
        """
        dc, cluster = self._datacenter_and_cluster(si)
        hosts = self._eligible_hosts(cluster)
        if self._placement == "cluster":
            datastore = self._find(si, vim.Datastore, self._datastore)
            if datastore is None:
                raise RuntimeError(f"Datastore not found: {self._datastore!r}")
            placements = [(None, datastore)] * len(vm_defs)
        else:
            placements = infra.place(hosts, vm_defs, self._placement, self._vcpu_ratio)
        for vm_def, placed in zip(vm_defs, placements, strict=True):
            vm_def["_placement"] = placed

        refs: dict = {}
        names = {n.get("vlan_id"): n.get("name", "") for n in networks}
        if self._switch_mode == "vss":
            # Every host a VM may run on: the placed ones, or all of them when DRS places.
            pg_hosts = list({id(h): h.ref for h, _ in placements if h}.values()) or [h.ref for h in hosts]
        else:
            dvs = self._find(si, vim.DistributedVirtualSwitch, self._dvs_name)
            if dvs is None:
                raise RuntimeError(f"Distributed switch not found: {self._dvs_name!r}")
        for logical, vlan in sorted(physical.items()):
            pg = infra.portgroup_name(range_id, vlan)
            promiscuous = infra.wants_promiscuous(names.get(logical, ""))
            if self._switch_mode == "vss":
                for host in pg_hosts:
                    infra.ensure_vss_portgroup(host, pg, vlan, self._vswitch, promiscuous)
                net = next((n for n in pg_hosts[0].network if n.name == pg), None)
                if net is None:
                    raise RuntimeError(f"port group {pg!r} not visible on {pg_hosts[0].name} after creating it")
            else:
                net = infra.ensure_dvs_portgroup(dvs, pg, vlan, promiscuous, lambda t: self._wait(t, si))
            refs[logical] = self._guard(infra.network_ref(net))

        uplink = None
        if any(nic.get("uplink") for v in vm_defs for nic in v["nics"]):
            net = self._find(si, vim.Network, self._uplink_network)
            if net is None:
                raise RuntimeError(f"uplink network VSPHERE_RANGE_UPLINK_NETWORK={self._uplink_network!r} was not found")
            uplink = self._guard(infra.network_ref(net))
        fallback = None
        if any(nic.get("vlan") is None and not nic.get("uplink") for v in vm_defs for nic in v["nics"]):
            net = self._find(si, vim.Network, self._network) if self._network else None
            if net is None:
                raise RuntimeError(f"a VM has no VLAN and the fallback network VSPHERE_NETWORK={self._network!r} "
                                   "was not found")
            fallback = self._guard(infra.network_ref(net))
        for vm_def in vm_defs:
            vm_def["_refs"] = [
                uplink if n.get("uplink") else refs[n["vlan"]] if n.get("vlan") is not None else fallback
                for n in vm_def["nics"]
            ]

        wanted = {v["template_name"] for v in vm_defs}
        view = si.content.viewManager.CreateContainerView(dc.vmFolder, [vim.VirtualMachine], True)
        try:
            templates = {vm.name: vm for vm in view.view if vm.name in wanted and vm.config.template}
        finally:
            view.Destroy()
        folder = infra.ensure_folder(dc, f"{self._range_folder}/{range_id[:8]}")
        return {"dc": dc, "cluster": cluster, "pool": cluster.resourcePool, "folder": folder, "templates": templates,
                "dvs": dvs if self._switch_mode != "vss" else None}

    def _clone_sync(self, si, site: dict, vm_def: dict):
        """Clone the inventory VM template onto the placed host and datastore, NICs wired, powered off."""
        template = site["templates"].get(vm_def["template_name"])
        if template is None:
            raise RuntimeError(
                f"template {vm_def['template_name']!r} is neither a Content Library item nor an inventory VM template"
            )
        host, datastore = vm_def["_placement"]
        config = infra.hardware_spec(vm_def, template.config.hardware.device, vm_def["_refs"])
        self._drop_ovf_env(config, vm_def, template)
        spec = vim.vm.CloneSpec(
            location=vim.vm.RelocateSpec(pool=site["pool"], datastore=datastore, host=host.ref if host else None),
            powerOn=False,
            template=False,
            config=config,
        )
        logger.info("Cloning %r from template %r", vm_def["name"], vm_def["template_name"])
        task = template.CloneVM_Task(folder=site["folder"], name=vm_def["name"], spec=spec)
        self._wait(task, si)
        return task.info.result

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
        self._drop_ovf_env(spec, vm_def, vm)
        self._wait(vm.ReconfigVM_Task(spec=spec), si)

    def _customize_sync(self, si, vm, vm_def: dict) -> None:
        """Hostname and static IPs, applied while the VM is still powered off.

        Linux: cloud-init guestinfo metadata, NICs matched by MAC. Windows: Sysprep
        guest customization. pfSense: the per-range config.xml (``_pfsense``, planned in
        ``_plan_appliances``) in ``guestinfo.tn.pfsense.*``, with the NICs' MACs, for the
        template's boot script to apply. Other routers (VyOS, OPNsense): none yet; their
        NIC order is WAN first (the uplink, when there is one), then the zones.

        A VM with software to install carries ``_guest`` credentials: the Windows
        Administrator password Sysprep sets, or the Linux install user cloud-init creates.
        """
        family = infra.os_family(vm_def)
        creds: guest.GuestCredentials | None = vm_def.get("_guest")
        if family == "linux":
            macs = [card.macAddress for card in infra.nic_cards(vm.config.hardware.device)]
            user = guest.cloud_init_user(creds) if creds else None
            spec = vim.vm.ConfigSpec(extraConfig=infra.linux_guestinfo(vm_def, macs, user))
            self._wait(vm.ReconfigVM_Task(spec=spec), si)
        elif family == "windows":
            password = creds.password.reveal() if creds else None
            self._wait(vm.CustomizeVM_Task(spec=infra.windows_customization(vm_def, password)), si)
        elif vm_def.get("_pfsense") is not None:
            macs = [card.macAddress for card in infra.nic_cards(vm.config.hardware.device)]
            values = pfsense_config.guestinfo(vm_def["_pfsense"], macs)
            spec = vim.vm.ConfigSpec(extraConfig=[vim.option.OptionValue(key=k, value=v) for k, v in values.items()])
            self._wait(vm.ReconfigVM_Task(spec=spec), si)
        # TODO(appliance): VyOS / OPNsense configs. Until then those boot with their
        # template config (the runbook, §4.1a, says what that config must hold).

    def _teardown_sync(self, si, range_id: str, networks: list[dict], mirrors: bool = False) -> int:
        """Remove the range's mirror sessions, port groups and (empty) VM folder. Missing
        ones are fine. Sessions go by name prefix (``tn-<range8>-``), so ones a build made
        but never recorded go too; they go first, while their port groups still exist."""
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
                removed += any([infra.remove_vss_portgroup(h, name) for h in cluster.host])
                continue
            dvs = dvs or self._find(si, vim.DistributedVirtualSwitch, self._dvs_name)
            if dvs is None:
                raise RuntimeError(f"Distributed switch not found: {self._dvs_name!r}")
            removed += infra.remove_dvs_portgroup(dvs, name, lambda t: self._wait(t, si))
        folder = infra.find_folder(dc, f"{self._range_folder}/{range_id[:8]}")
        if folder is not None and not folder.childEntity:
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
        plan["nics"] = [dict(n) for n in plan.get("nics") or []]  # the uplink is added to this copy
        if not plan.get("nics"):
            plan["nics"] = [{
                "vlan": vm_def.get("vlan_id"), "ip": vm_def.get("ip", ""), "prefix": vm_def.get("prefix", 24),
                "netmask": vm_def.get("netmask", "255.255.255.0"), "gateway": vm_def.get("gateway", ""),
            }]
        return plan

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
        VLAN to the physical VLAN worker.tasks reserved for it; without it, VLANs are
        allocated here from the pool, avoiding ``allocations["used_vlans"]``.

        With VSPHERE_RANGE_UPLINK_NETWORK set, the edge firewall gets a WAN NIC first, at
        ``allocations["uplink_ip"]`` (reserved by worker.tasks) or the lowest pool address
        not in ``allocations["used_uplink_ips"]``. Software in the nodes' ``services`` is
        installed after power-on (``_install_software``).

        Nothing is left behind on failure: when no VM could be built, the VMs and port
        groups this call made are removed again, so a retry starts clean.
        """
        start = time.monotonic()
        errors: list[str] = []
        warnings: list[str] = []
        vms_out: list[dict] = []
        vm_defs = [self._vm_plan(range_id, v) for v in template.get("vms", [])]
        networks = [dict(n) for n in template.get("networks", [])]
        physical: dict[int, int] = {}
        uplink: dict | None = None
        mirrors: list[dict] = []

        try:
            if SmartConnect is None:
                raise RuntimeError("pyvmomi is required to build vSphere ranges (pip install pyvmomi)")
            uplink = self._plan_uplink(vm_defs, allocations)
            self._plan_software(vm_defs, uplink, warnings)
            self._plan_appliances(vm_defs, template, networks, warnings)
            mirror_plans = self._plan_mirrors(template, networks, warnings)
            logical = sorted({int(n["vlan"]) for v in vm_defs for n in v["nics"] if n.get("vlan") is not None})
            rspan_keys = [p["rspan_key"] for p in mirror_plans]
            physical = {int(k): int(v) for k, v in (allocations.get("physical_vlans") or {}).items()}
            if missing := [v for v in logical + rspan_keys if v not in physical]:
                used = {int(v) for v in allocations.get("used_vlans") or ()} | set(physical.values())
                physical.update(vlan_pool.allocate(missing, used, vlan_pool.parse_pool(self._vlan_pool)))
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

            with self._vim() as si:
                try:
                    site = await asyncio.to_thread(self._prepare_sync, si, range_id, vm_defs, networks, physical)
                    async with self._client() as client:
                        results = await asyncio.gather(
                            *(self._provision_one_vm(client, si, site, v) for v in vm_defs), return_exceptions=True
                        )
                    for vm_def, res in zip(vm_defs, results, strict=True):
                        if isinstance(res, Exception):
                            errors.append(f"VM {vm_def['name']}: {self._redact(res, vm_def)}")
                        else:
                            vms_out.append(res)
                    if mirror_plans and vms_out:
                        mirrors = await asyncio.to_thread(self._mirror_sync, si, site, range_id, mirror_plans,
                                                          vm_defs, vms_out, errors, warnings)
                    await self._install_software(si, vm_defs, vms_out, start, errors, warnings)
                except Exception as exc:
                    errors.append(self._redact(exc, *vm_defs))
                if not vms_out:
                    await self._rollback(si, range_id, networks, errors)
        except Exception as exc:
            errors.append(self._redact(exc, *vm_defs))
        finally:
            for vm_def in vm_defs:  # the guest passwords end with this call
                vm_def.pop("_guest", None)

        built = {v["name"] for v in vms_out}
        if uplink and uplink["vm"] not in built:
            uplink = None  # the edge firewall was not built; its address goes back to the pool
        return ProvisionResult(
            status=outcome(len(vms_out), errors) if vm_defs else ("failed" if errors else "ok"),
            vms=vms_out,
            networks=networks,
            duration_seconds=time.monotonic() - start,
            errors=errors,
            warnings=warnings,
            uplink=uplink,
            mirrors=mirrors,
        )

    # ------------------------------------------------------------------ #
    # WAN uplink and deploy-time software
    # ------------------------------------------------------------------ #

    @staticmethod
    def _redact(exc, *vm_defs: dict) -> str:
        # A vSphere fault often has an empty str(); its msg or its type still says what failed.
        text = str(exc) or getattr(exc, "msg", None) or type(exc).__name__
        return guest.redact(text, *(v.get("_guest") for v in vm_defs))

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
            pool = uplink_pool.parse_ip_pool(self._uplink_pool)
            ip = uplink_pool.allocate_ip({str(a) for a in allocations.get("used_uplink_ips") or ()}, pool)
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
        """Render each pfSense VM's per-range config.xml (``_pfsense``), after the uplink NIC
        is planned. The template's ``network.firewall_rules`` apply when it has any; rules
        that cannot become pfSense rules are skipped with a warning."""
        net = template.get("network") if isinstance(template.get("network"), dict) else {}
        rules = [r for r in (net.get("firewall_rules") or []) if isinstance(r, dict)]
        depot_host = (urlparse(self._depot_url).hostname or "") if self._depot_url else ""
        for vm_def in vm_defs:
            if infra.os_family(vm_def) != "appliance" or not pfsense_config.is_pfsense(vm_def):
                continue
            cfg = pfsense_config.build_config(
                vm_def, networks=networks, rules=rules, depot_host=depot_host, depot_ports=self._depot_ports,
                hostname=infra.hostname(vm_def),
            )
            vm_def["_pfsense"] = cfg
            warnings += [f"VM {vm_def['name']}: {n}" for n in cfg.notes]

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
        wanted = [v for v in vm_defs if v.get("services") and infra.os_family(v) in ("windows", "linux")]
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
            vm_def["_guest"] = guest.windows_credentials() if family == "windows" else guest.linux_credentials()

    def _install_sync(self, si, vm_def: dict, vm_id: str, deadline: float) -> dict:
        creds: guest.GuestCredentials = vm_def["_guest"]
        specs = vm_def["_software"]
        if creds.family == "windows":
            commands, cleanup = guest.windows_commands(specs, self._choco_feed), None
        else:
            commands, cleanup = guest.linux_commands(specs, self._apt_proxy), guest.linux_cleanup_command()
        vm = self._vm(si, vm_id)
        session = guest.GuestSession(si.content, vm, creds, poll=self._guest_poll)
        result = guest.install(session, commands, deadline, cleanup)
        if creds.family == "linux":
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
                                errors: list[str], warnings: list[str]) -> None:
        """Install each built VM's software inside the guest, VSPHERE_CONCURRENCY VMs at a time.

        Every VM gets TN_SOFTWARE_INSTALL_TIMEOUT from when its install starts, but nothing
        runs past VSPHERE_PROVISION_BUDGET from the start of the provision. A failed or
        timed-out package is an error (the range is ``partial``), never a failed build.
        """
        by_name = {v["name"]: v for v in vm_defs}
        for out in vms_out:
            skipped = by_name[out["name"]].get("_software_skipped")
            if skipped:
                out["software"] = {"status": "skipped", "packages": [
                    {"name": n, "status": "skipped", "exit_code": None} for n in skipped]}
        todo = [(by_name[o["name"]], o) for o in vms_out if by_name[o["name"]].get("_guest")]
        if not todo:
            return
        hard_stop = start + self._budget
        gate = asyncio.Semaphore(self._concurrency)

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

    async def _rollback(self, si, range_id: str, networks: list[dict], errors: list[str]) -> None:
        """Best effort: remove what a failed build created (its VMs already removed themselves)."""
        try:
            await asyncio.to_thread(self._teardown_sync, si, range_id, [n for n in networks if n.get("portgroup")])
        except Exception as exc:
            errors.append(f"cleanup after the failed build: {exc}")

    async def _provision_one_vm(self, client: httpx.AsyncClient, si, site: dict, vm_def: dict) -> dict:
        """Create one VM (OVF item or inventory clone), wire and customize it, power it on."""
        vm_id = None
        async with self._semaphore:
            try:
                item = await self._find_library_item(client, vm_def["template_name"])
                if item:
                    vm_id = await self._deploy_ovf(client, item, vm_def, site)
                    vm = self._vm(si, vm_id)
                    await asyncio.to_thread(self._reconfigure_sync, si, vm, vm_def)
                else:
                    vm = await asyncio.to_thread(self._clone_sync, si, site, vm_def)
                    vm_id = vm._moId
                await asyncio.to_thread(self._customize_sync, si, vm, vm_def)
                await self._power_action(client, vm_id, "start")
            except Exception:
                if vm_id:  # half-built: do not leave it behind unrecorded
                    with contextlib.suppress(Exception):
                        await self._delete_vm(client, vm_id)
                raise
        tools_ready = await self._wait_tools(client, vm_id)
        ip = await self._get_vm_ip(client, vm_id) if tools_ready else None
        host, datastore = vm_def["_placement"]
        primary = next((n for n in vm_def["nics"] if not n.get("uplink")), vm_def["nics"][0])
        extra = {"pfsense": vm_def["_pfsense"].summary()} if vm_def.get("_pfsense") is not None else {}
        return {
            **extra,
            "vm_id": vm_id,
            "name": vm_def["name"],
            "node_id": vm_def.get("node_id", ""),
            "status": "running",
            "ip": ip or primary.get("ip", ""),
            "nics": [
                {"network": ref["name"], "ip": nic.get("ip", "")}
                for nic, ref in zip(vm_def["nics"], vm_def["_refs"], strict=True)
            ],
            "host": host.name if host else "",
            "datastore": getattr(datastore, "name", ""),
            "tools_ready": tools_ready,
        }

    async def destroy(
        self,
        range_id: str,
        provision_output: dict,
    ) -> DestroyResult:
        """Delete the VMs, then (only once they are all gone) the port groups and folder."""
        start = time.monotonic()
        errors: list[str] = []
        vms = provision_output.get("vms", [])
        portgroups = [n for n in provision_output.get("networks", []) if n.get("portgroup")]
        mirrors = bool(provision_output.get("mirrors"))

        if not vms and not portgroups and not mirrors:
            return DestroyResult(status="ok", resources_removed=0, duration_seconds=0.0)

        removed = 0
        try:
            async with self._client() as client:
                ids = [vm["vm_id"] for vm in vms if vm.get("vm_id")]
                results = await asyncio.gather(*(self._delete_vm(client, i) for i in ids), return_exceptions=True)
                for vm_id, res in zip(ids, results, strict=True):
                    if isinstance(res, Exception):
                        errors.append(f"VM {vm_id}: {res}")
                    else:
                        removed += 1
            # A port group still has VMs on it until every VM is gone.
            if (portgroups or mirrors) and not errors:
                if SmartConnect is None:
                    raise RuntimeError("pyvmomi is required to remove range port groups (pip install pyvmomi)")
                with self._vim() as si:
                    removed += await asyncio.to_thread(self._teardown_sync, si, range_id, portgroups, mirrors)
        except Exception as exc:
            errors.append(str(exc))

        return DestroyResult(
            status=outcome(removed, errors),
            resources_removed=removed,
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )

    async def _power_all(self, provision_output: dict, action: str) -> tuple[int, list[str]]:
        vms = provision_output.get("vms", [])
        errors = [f"VM {vm.get('name')}: no vm_id recorded" for vm in vms if not vm.get("vm_id")]
        ids = [vm["vm_id"] for vm in vms if vm.get("vm_id")]
        try:
            async with self._client() as client:
                results = await asyncio.gather(
                    *(self._power_action(client, i, action) for i in ids), return_exceptions=True
                )
            errors += [f"VM {i}: {res}" for i, res in zip(ids, results, strict=True) if isinstance(res, Exception)]
        except Exception as exc:
            errors.append(str(exc))
        return max(0, len(vms) - len(errors)), errors

    async def stop(
        self,
        range_id: str,
        provision_output: dict,
    ) -> StopResult:
        start = time.monotonic()
        stopped, errors = await self._power_all(provision_output, "stop")
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
        started, errors = await self._power_all(provision_output, "start")
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
        snapped, errors, _ = await self._vim_op(provision_output, self._snapshot_sync, name)
        return SnapshotResult(
            status=outcome(snapped, errors),
            snapshot_name=name,
            vms_snapped=snapped,
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )

    async def restore(
        self,
        range_id: str,
        provision_output: dict,
        name: str,
        power_on: bool,
    ) -> RestoreResult:
        start = time.monotonic()
        restored, errors, reverted = await self._vim_op(provision_output, self._restore_sync, name, power_on)
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
        cleaned, errors, _ = await self._vim_op(provision_output, self._delete_snapshot_sync, name)
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

        try:
            async with self._client() as client:
                for vm in vms:
                    vm_id = vm.get("vm_id")
                    if not vm_id:
                        continue
                    try:
                        power = await self._api_get(client, f"/vcenter/vm/{vm_id}/power")
                        state = power.get("state", "UNKNOWN").lower()
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
        scope = self._scope
        if scope is not None:
            return _read_vm_metrics(self._scope_si(scope), vm_ids)
        si = self._connect(self._metrics_timeout)  # a call outside a run: a session of its own
        try:
            return _read_vm_metrics(si, vm_ids)
        finally:
            with contextlib.suppress(Exception):
                Disconnect(si)

    async def _logout(self) -> None:
        token, self._session_token = self._session_token, None
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, verify=self._verify_ssl, timeout=10.0, transport=self._transport
            ) as client:
                await client.delete("/api/session", headers={"vmware-api-session-id": token})
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
