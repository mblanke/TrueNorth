"""TrueNorth Range - VMware vSphere REST API provisioner.

Provisions VMs by cloning from a vSphere Content Library using the
vSphere Automation REST API (available since vSphere 6.7), over httpx.

Snapshots are the exception. The Automation REST API has no VM snapshot
operations, so snapshot, restore and delete go through the vSphere Web
Services API with pyVmomi, VMware's own SDK.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
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
    from pyVim.task import WaitForTask
    from pyVmomi import vim
except ImportError:  # only the snapshot operations need it
    Disconnect = SmartConnect = WaitForTask = vim = None  # type: ignore[assignment]

from .. import windows_roles
from .base import BaseProvisioner
from .results import (
    DestroyResult,
    HealthResult,
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
VSPHERE_NETWORK: str = os.environ.get("VSPHERE_NETWORK", "VM Network")
VSPHERE_CONTENT_LIBRARY: str = os.environ.get("VSPHERE_CONTENT_LIBRARY", "TrueNorth-Templates")
VSPHERE_VERIFY_SSL: bool = os.environ.get("VSPHERE_VERIFY_SSL", "false").lower() == "true"
VSPHERE_CONCURRENCY: int = int(os.environ.get("VSPHERE_CONCURRENCY", "4"))
VSPHERE_TOOLS_TIMEOUT: int = int(os.environ.get("VSPHERE_TOOLS_TIMEOUT", "120"))
# Per snapshot task. Keep it under Celery's visibility timeout (celery_app.py, 3600s).
VSPHERE_SNAPSHOT_TIMEOUT: int = int(os.environ.get("VSPHERE_SNAPSHOT_TIMEOUT", "1800"))
# Local administrator baked into the Windows golden images (Packer Autounattend). Used only
# for VMware Tools guest operations that install Windows Server roles; never logged.
VSPHERE_GUEST_WIN_USER: str = os.environ.get("VSPHERE_GUEST_WIN_USER", "Administrator")
VSPHERE_GUEST_WIN_PASSWORD: str = os.environ.get("VSPHERE_GUEST_WIN_PASSWORD", "")
# Per guest step (feature install, forest promotion, reboot). Under Celery's 3600s timeout.
VSPHERE_ROLE_TIMEOUT: int = int(os.environ.get("VSPHERE_ROLE_TIMEOUT", "1800"))

_POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
_EXIT_REBOOT = 3010  # the Windows "success, restart required" code

# Grows C: into any space a larger virtual disk added, then installs the features.
_FEATURE_SCRIPT = """$ErrorActionPreference = 'Stop'
$max = (Get-PartitionSupportedSize -DriveLetter C).SizeMax
if ((Get-Partition -DriveLetter C).Size -lt $max) {{ Resize-Partition -DriveLetter C -Size $max }}
$r = Install-WindowsFeature -Name {features} -IncludeManagementTools
if (-not $r.Success) {{ exit 1 }}
if ($r.RestartNeeded -eq 'Yes') {{ exit 3010 }}
exit 0
"""

_FOREST_SCRIPT = """$ErrorActionPreference = 'Stop'
Import-Module ADDSDeployment
$dsrm = ConvertTo-SecureString '{dsrm}' -AsPlainText -Force
Install-ADDSForest -DomainName '{domain}' -DomainNetbiosName '{netbios}' -SafeModeAdministratorPassword $dsrm `
  -InstallDns -Force -NoRebootOnCompletion | Out-Null
exit 3010
"""


def _encoded(script: str) -> str:
    """PowerShell -EncodedCommand form: no quoting surprises, no length games."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _ps_quote(value: str) -> str:
    return value.replace("'", "''")


def _netbios(domain: str) -> str:
    label = "".join(c for c in domain.split(".")[0].upper() if c.isalnum())
    return (label or "RANGE")[:15]


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


class VsphereAPIProvisioner(BaseProvisioner):
    """VMware vSphere REST API provisioner.

    Uses the vCenter Automation API (``/api/vcenter/``) to clone VMs from a
    Content Library, configure them via guestinfo metadata, and manage their
    lifecycle.  Authentication is performed once per provisioner instance via
    the ``/api/session`` endpoint; the session token is reused for all calls.
    """

    def __init__(self) -> None:
        self._base_url = VSPHERE_URL.rstrip("/")
        self._username = VSPHERE_USERNAME
        self._password = VSPHERE_PASSWORD
        self._datacenter = VSPHERE_DATACENTER
        self._cluster = VSPHERE_CLUSTER
        self._datastore = VSPHERE_DATASTORE
        self._network = VSPHERE_NETWORK
        self._content_library = VSPHERE_CONTENT_LIBRARY
        self._verify_ssl = VSPHERE_VERIFY_SSL
        self._semaphore = asyncio.Semaphore(VSPHERE_CONCURRENCY)
        self._tools_timeout = VSPHERE_TOOLS_TIMEOUT
        self._session_token: str | None = None
        self._concurrency = max(1, VSPHERE_CONCURRENCY)
        self._snapshot_timeout = VSPHERE_SNAPSHOT_TIMEOUT
        self._guest_user = VSPHERE_GUEST_WIN_USER
        self._guest_password = VSPHERE_GUEST_WIN_PASSWORD
        self._role_timeout = VSPHERE_ROLE_TIMEOUT

    # ------------------------------------------------------------------ #
    # HTTP helpers
    # ------------------------------------------------------------------ #

    def _client(self, session_token: str) -> httpx.AsyncClient:
        if httpx is None:
            raise RuntimeError("httpx is required for VsphereAPIProvisioner")
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={"vmware-api-session-id": session_token},
            verify=self._verify_ssl,
            timeout=60.0,
        )

    async def _authenticate(self) -> str:
        """Create a vCenter session and return the session token."""
        if httpx is None:
            raise RuntimeError("httpx is required for VsphereAPIProvisioner")
        async with httpx.AsyncClient(
            base_url=self._base_url,
            verify=self._verify_ssl,
            timeout=30.0,
        ) as client:
            resp = await client.post(
                "/api/session",
                auth=(self._username, self._password),
            )
            resp.raise_for_status()
            # vCenter returns the token as a JSON string
            return resp.json()

    async def _get_session(self) -> str:
        """Return a valid session token, creating one if needed."""
        if not self._session_token:
            self._session_token = await self._authenticate()
        return self._session_token

    async def _api_get(self, client: httpx.AsyncClient, path: str) -> dict | list:
        resp = await client.get(f"/api{path}")
        resp.raise_for_status()
        return resp.json()

    async def _api_post(self, client: httpx.AsyncClient, path: str, **kwargs) -> dict | str:
        resp = await client.post(f"/api{path}", **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def _api_delete(self, client: httpx.AsyncClient, path: str) -> None:
        resp = await client.delete(f"/api{path}")
        resp.raise_for_status()

    async def _api_patch(self, client: httpx.AsyncClient, path: str, **kwargs) -> None:
        resp = await client.patch(f"/api{path}", **kwargs)
        resp.raise_for_status()

    # ------------------------------------------------------------------ #
    # Inventory helpers
    # ------------------------------------------------------------------ #

    async def _find_datacenter(self, client: httpx.AsyncClient) -> str:
        """Return the datacenter MoRef ID for the configured datacenter name."""
        items = await self._api_get(client, f"/vcenter/datacenter?names={self._datacenter}")
        if not items:
            raise RuntimeError(f"Datacenter not found: {self._datacenter!r}")
        return items[0]["datacenter"]

    async def _find_cluster(self, client: httpx.AsyncClient, dc_id: str) -> str:
        """Return the cluster ID for the configured cluster name."""
        items = await self._api_get(client, f"/vcenter/cluster?names={self._cluster}&datacenters={dc_id}")
        if not items:
            raise RuntimeError(f"Cluster not found: {self._cluster!r}")
        return items[0]["cluster"]

    async def _find_datastore(self, client: httpx.AsyncClient, dc_id: str) -> str:
        """Return the datastore MoRef ID for the configured datastore name."""
        items = await self._api_get(client, f"/vcenter/datastore?names={self._datastore}&datacenters={dc_id}")
        if not items:
            raise RuntimeError(f"Datastore not found: {self._datastore!r}")
        return items[0]["datastore"]

    async def _find_network(self, client: httpx.AsyncClient, dc_id: str) -> str:
        """Return the network MoRef ID for the configured network name."""
        items = await self._api_get(client, f"/vcenter/network?names={self._network}&datacenters={dc_id}")
        if not items:
            raise RuntimeError(f"Network not found: {self._network!r}")
        return items[0]["network"]

    async def _find_library_item(self, client: httpx.AsyncClient, template_name: str) -> str:
        """Return the Content Library item ID for the given OVF template name."""
        # Find the library first
        libs = await self._api_get(client, f'/content/library?action=find&spec={{"name":"{self._content_library}"}}')
        if not libs:
            raise RuntimeError(f"Content Library not found: {self._content_library!r}")
        lib_id = libs[0] if isinstance(libs, list) else libs

        # Find the item within the library
        items = await self._api_post(
            client,
            "/content/library/item?action=find",
            json={"name": template_name, "library_id": lib_id},
        )
        if not items:
            raise RuntimeError(f"Template {template_name!r} not found in library {self._content_library!r}")
        return items[0] if isinstance(items, list) else items

    # ------------------------------------------------------------------ #
    # VM operations
    # ------------------------------------------------------------------ #

    async def _deploy_ovf(
        self,
        client: httpx.AsyncClient,
        library_item_id: str,
        name: str,
        folder_id: str,
        resource_pool_id: str,
        datastore_id: str,
        network_mappings: list[dict] | None = None,
    ) -> str:
        """Deploy a VM from a Content Library OVF item.  Returns the new VM ID."""
        async with self._semaphore:
            logger.info("Deploying VM %r from library item %s", name, library_item_id)
            spec: dict = {
                "name": name,
                "accept_all_eula": True,
                "default_datastore_id": datastore_id,
                "deployment_spec": {
                    "name": name,
                    "default_datastore_id": datastore_id,
                    "accept_all_EULA": True,
                    "storage_provisioning": "thin",
                },
                "target": {
                    "folder_id": folder_id,
                    "resource_pool_id": resource_pool_id,
                },
            }
            if network_mappings:
                spec["deployment_spec"]["network_mappings"] = network_mappings

            result = await self._api_post(
                client,
                f"/vcenter/ovf/library-item/{library_item_id}?action=deploy",
                json=spec,
            )
            if not result.get("succeeded"):
                error = result.get("error", {})
                raise RuntimeError(f"OVF deploy failed for {name!r}: {error}")
            return result["resource_id"]["id"]

    async def _power_action(self, client: httpx.AsyncClient, vm_id: str, action: str) -> None:
        """Perform a power action (start/stop/reset/suspend) on a VM."""
        await self._api_post(client, f"/vcenter/vm/{vm_id}/power?action={action}")

    async def _delete_vm(self, client: httpx.AsyncClient, vm_id: str) -> None:
        """Power off (if running) then delete a VM."""
        try:
            power = await self._api_get(client, f"/vcenter/vm/{vm_id}/power")
            if power.get("state") == "POWERED_ON":
                await self._power_action(client, vm_id, "stop")
                await asyncio.sleep(5)
        except Exception:
            pass
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
    # Disk size and Windows Server roles
    # ------------------------------------------------------------------ #

    def _grow_disk_sync(self, vm_id: str, disk_gb: int) -> bool:
        """Grow the VM's first virtual disk to ``disk_gb`` (never shrink). True if grown.

        The Automation REST API cannot change a disk's capacity (Disk.UpdateSpec has
        only ``backing``), so this goes through pyVmomi like the snapshots do.
        """
        want_kb = int(disk_gb) * 1024 * 1024
        with self._vim() as si:
            vm = vim.VirtualMachine(vm_id, si._stub)
            disk = next((d for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)), None)
            if disk is None:
                raise LookupError("VM has no virtual disk")
            if disk.capacityInKB >= want_kb:
                return False
            disk.capacityInKB = want_kb
            change = vim.vm.device.VirtualDeviceSpec(
                operation=vim.vm.device.VirtualDeviceSpec.Operation.edit, device=disk
            )
            WaitForTask(vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(deviceChange=[change])),
                        si=si, maxWaitTime=self._role_timeout)
            return True

    def _guest_credentials(self) -> dict:
        return {"interactive_session": False, "type": "USERNAME_PASSWORD",
                "user_name": self._guest_user, "password": self._guest_password}

    async def _run_guest_ps(self, client: httpx.AsyncClient, vm_id: str, script: str) -> int:
        """Run a PowerShell script in the guest through VMware Tools; return its exit code.

        Guest operations go through vCenter, so they work although the range VLAN is
        isolated from the worker.
        """
        creds = self._guest_credentials()
        pid = await self._api_post(
            client,
            f"/vcenter/vm/{vm_id}/guest/processes?action=create",
            json={"credentials": creds,
                  "spec": {"path": _POWERSHELL,
                           "arguments": f"-NoProfile -NonInteractive -ExecutionPolicy Bypass "
                                        f"-EncodedCommand {_encoded(script)}"}},
        )
        deadline = time.monotonic() + self._role_timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(10)
            info = await self._api_post(
                client, f"/vcenter/vm/{vm_id}/guest/processes/{pid}?action=get", json={"credentials": creds}
            )
            if isinstance(info, dict) and info.get("finished"):
                return int(info.get("exit_code", -1))
        raise TimeoutError(f"guest script still running after {self._role_timeout}s")

    async def _guest_reboot(self, client: httpx.AsyncClient, vm_id: str) -> None:
        await self._api_post(client, f"/vcenter/vm/{vm_id}/guest/power?action=reboot")
        await asyncio.sleep(60)  # let Tools drop before waiting for it to come back
        deadline = time.monotonic() + self._role_timeout
        while time.monotonic() < deadline:
            if await self._wait_tools(client, vm_id):
                return
        raise TimeoutError("VMware Tools did not come back after the reboot")

    async def _install_roles(self, client: httpx.AsyncClient, vm_id: str, vm_def: dict) -> dict[str, dict]:
        """Install the VM's Windows feature roles and, on a forest-root DC, promote it.

        Returns ``{role: {"status": ok|failed|skipped, "detail": ...}}`` for every role
        on the VM. Image roles were built into the template the VM was cloned from.
        """
        roles = list(vm_def.get("roles") or [])
        features = list(vm_def.get("role_features") or [])
        status: dict[str, dict] = {r: {"status": "ok", "detail": "in role image"} for r in roles}
        feature_roles = [r for r in roles if windows_roles.BY_ID[r]["install"]["method"] == "feature"]
        if not feature_roles:
            return status

        def mark(state: str, detail: str, only: list[str] | None = None) -> dict[str, dict]:
            for r in only or feature_roles:
                status[r] = {"status": state, "detail": detail}
            return status

        if not self._guest_password:
            return mark("skipped", "VSPHERE_GUEST_WIN_PASSWORD is not set")
        step = feature_roles  # the roles a failure right now belongs to
        try:
            code = await self._run_guest_ps(
                client, vm_id, _FEATURE_SCRIPT.format(features=",".join(features))
            )
            if code not in (0, _EXIT_REBOOT):
                return mark("failed", f"Install-WindowsFeature exited {code}")
            if code == _EXIT_REBOOT:
                await self._guest_reboot(client, vm_id)
            mark("ok", "installed")
            if "ad-ds" in roles:
                if not vm_def.get("ad_forest_root"):
                    mark("skipped", f"AD DS installed; promotion as an extra DC for "
                                    f"{vm_def.get('ad_domain')} is not automated", ["ad-ds"])
                else:
                    step = ["ad-ds"]
                    domain = str(vm_def.get("ad_domain") or "range.local")
                    code = await self._run_guest_ps(client, vm_id, _FOREST_SCRIPT.format(
                        dsrm=_ps_quote(secrets.token_urlsafe(18) + "aA1!"),
                        domain=_ps_quote(domain), netbios=_ps_quote(_netbios(domain)),
                    ))
                    if code != _EXIT_REBOOT:
                        return mark("failed", f"Install-ADDSForest exited {code}", ["ad-ds"])
                    await self._guest_reboot(client, vm_id)
                    mark("ok", f"forest {domain} created", ["ad-ds"])
        except Exception as exc:  # noqa: BLE001 - reported per role, never raised past the VM
            return mark("failed", str(exc)[:300], step)
        return status

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

    @contextmanager
    def _vim(self):
        if SmartConnect is None:
            raise RuntimeError("pyvmomi is required for vSphere snapshots (pip install pyvmomi)")
        url = urlparse(self._base_url)
        si = SmartConnect(
            host=url.hostname,
            port=url.port or 443,
            user=self._username,
            pwd=self._password,
            disableSslCertValidation=not self._verify_ssl,
        )
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
                    WaitForTask(task, si=si, maxWaitTime=self._snapshot_timeout)
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
    # BaseProvisioner implementation
    # ------------------------------------------------------------------ #

    async def provision(
        self,
        range_id: str,
        template: dict,
        allocations: dict,
    ) -> ProvisionResult:
        start = time.monotonic()
        errors: list[str] = []
        vms_out: list[dict] = []

        try:
            session = await self._get_session()
            async with self._client(session) as client:
                dc_id = await self._find_datacenter(client)
                cluster_id = await self._find_cluster(client, dc_id)
                ds_id = await self._find_datastore(client, dc_id)

                # vSphere resource pool is derived from cluster
                rp_items = await self._api_get(client, f"/vcenter/resource-pool?clusters={cluster_id}")
                rp_id = rp_items[0]["resource_pool"] if rp_items else None

                # VM folder (datacenter root)
                folder_items = await self._api_get(
                    client,
                    f"/vcenter/folder?datacenters={dc_id}&type=VIRTUAL_MACHINE",
                )
                folder_id = folder_items[0]["folder"] if folder_items else None

                vm_defs = template.get("vms", [])
                tasks = []
                for vm_def in vm_defs:
                    template_name = vm_def.get("template_name", "ubuntu-2404-cloud")
                    vm_name = f"{range_id}-{vm_def['name']}"
                    tasks.append(
                        self._provision_one_vm(
                            client,
                            vm_def,
                            vm_name,
                            template_name,
                            folder_id,
                            rp_id,
                            ds_id,
                        )
                    )

                results = await asyncio.gather(*tasks, return_exceptions=True)
                for vm_def, res in zip(vm_defs, results):
                    if isinstance(res, Exception):
                        errors.append(f"VM {vm_def['name']}: {res}")
                    else:
                        vms_out.append(res)
                        errors.extend(f"VM {vm_def['name']}: {e}" for e in res.get("errors", []))

        except Exception as exc:
            errors.append(str(exc))
            return ProvisionResult(
                status="failed",
                errors=errors,
                duration_seconds=time.monotonic() - start,
            )

        status = "ok" if not errors else ("partial" if vms_out else "failed")
        return ProvisionResult(
            status=status,
            vms=vms_out,
            networks=template.get("networks", []),
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )

    async def _provision_one_vm(
        self,
        client: httpx.AsyncClient,
        vm_def: dict,
        vm_name: str,
        template_name: str,
        folder_id: str,
        rp_id: str,
        ds_id: str,
    ) -> dict:
        library_item_id = await self._find_library_item(client, template_name)
        vm_id = await self._deploy_ovf(client, library_item_id, vm_name, folder_id, rp_id, ds_id)

        # Resize hardware to match vm_def
        hw_update: dict = {}
        if "cores" in vm_def:
            hw_update["cpu"] = {"count": vm_def["cores"], "hot_add_enabled": True}
        if "memory" in vm_def:
            hw_update["memory"] = {"size_MiB": vm_def["memory"], "hot_add_enabled": True}
        if hw_update:
            await self._api_patch(client, f"/vcenter/vm/{vm_id}/hardware", json=hw_update)
        errors: list[str] = []
        if vm_def.get("disk_gb"):
            try:
                await asyncio.to_thread(self._grow_disk_sync, vm_id, int(vm_def["disk_gb"]))
            except Exception as exc:  # noqa: BLE001 - the VM still works on its template disk
                errors.append(f"disk not resized to {vm_def['disk_gb']} GB: {getattr(exc, 'msg', None) or exc}")

        await self._power_action(client, vm_id, "start")
        tools_ready = await self._wait_tools(client, vm_id)
        ip = await self._get_vm_ip(client, vm_id) if tools_ready else None

        out = {
            "vm_id": vm_id,
            "name": vm_def["name"],
            "status": "running",
            "ip": ip or vm_def.get("ip", ""),
            "tools_ready": tools_ready,
        }
        if vm_def.get("roles"):
            if tools_ready:
                roles = await self._install_roles(client, vm_id, vm_def)
            else:
                roles = {r: {"status": "skipped", "detail": "VMware Tools not running"} for r in vm_def["roles"]}
            out["roles"] = roles
            errors += [f"role {r}: {v['status']} ({v['detail']})"
                       for r, v in roles.items() if v["status"] != "ok"]
        if errors:
            out["errors"] = errors
        return out

    async def destroy(
        self,
        range_id: str,
        provision_output: dict,
    ) -> DestroyResult:
        start = time.monotonic()
        errors: list[str] = []
        vms = provision_output.get("vms", [])

        if not vms:
            return DestroyResult(status="ok", resources_removed=0, duration_seconds=0.0)

        try:
            session = await self._get_session()
            async with self._client(session) as client:
                tasks = [self._delete_vm(client, vm["vm_id"]) for vm in vms if vm.get("vm_id")]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for vm, res in zip(vms, results):
                    if isinstance(res, Exception):
                        errors.append(f"VM {vm.get('name')}: {res}")
        except Exception as exc:
            errors.append(str(exc))

        status = "ok" if not errors else ("partial" if len(errors) < len(vms) else "failed")
        return DestroyResult(
            status=status,
            resources_removed=len(vms) - len(errors),
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )

    async def stop(
        self,
        range_id: str,
        provision_output: dict,
    ) -> StopResult:
        start = time.monotonic()
        errors: list[str] = []
        vms = provision_output.get("vms", [])

        try:
            session = await self._get_session()
            async with self._client(session) as client:
                tasks = [self._power_action(client, vm["vm_id"], "stop") for vm in vms if vm.get("vm_id")]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for vm, res in zip(vms, results):
                    if isinstance(res, Exception):
                        errors.append(f"VM {vm.get('name')}: {res}")
        except Exception as exc:
            errors.append(str(exc))

        return StopResult(
            status="ok" if not errors else "partial",
            vms_stopped=len(vms) - len(errors),
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )

    async def start(
        self,
        range_id: str,
        provision_output: dict,
    ) -> StartResult:
        start = time.monotonic()
        errors: list[str] = []
        vms = provision_output.get("vms", [])

        try:
            session = await self._get_session()
            async with self._client(session) as client:
                tasks = [self._power_action(client, vm["vm_id"], "start") for vm in vms if vm.get("vm_id")]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for vm, res in zip(vms, results):
                    if isinstance(res, Exception):
                        errors.append(f"VM {vm.get('name')}: {res}")
        except Exception as exc:
            errors.append(str(exc))

        return StartResult(
            status="ok" if not errors else "partial",
            vms_started=len(vms) - len(errors),
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
        vm_statuses: dict[str, str] = {}
        vms = provision_output.get("vms", [])

        try:
            session = await self._get_session()
            async with self._client(session) as client:
                for vm in vms:
                    vm_id = vm.get("vm_id")
                    vm_name = vm.get("name", vm_id)
                    if not vm_id:
                        continue
                    try:
                        power = await self._api_get(client, f"/vcenter/vm/{vm_id}/power")
                        vm_statuses[vm_name] = power.get("state", "UNKNOWN").lower()
                    except Exception as exc:
                        vm_statuses[vm_name] = "error"
                        errors.append(f"VM {vm_name}: {exc}")
        except Exception as exc:
            errors.append(str(exc))

        healthy = not errors and all(s == "powered_on" for s in vm_statuses.values())
        return HealthResult(
            healthy=healthy,
            status="ok" if healthy else "degraded",
            vm_statuses=vm_statuses,
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )
