"""TrueNorth Range - VMware vSphere REST API provisioner.

Provisions VMs by cloning from a vSphere Content Library using the
vSphere Automation REST API (available since vSphere 6.7), over httpx.

Snapshots are the exception. The Automation REST API has no VM snapshot
operations, so snapshot, restore and delete go through the vSphere Web
Services API with pyVmomi, VMware's own SDK.
"""

from __future__ import annotations

import asyncio
import logging
import os
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

    async def _find_network(self, client: httpx.AsyncClient, dc_id: str, name: str | None = None) -> str:
        """Return the network MoRef ID for ``name`` (default: the configured network)."""
        name = name or self._network
        items = await self._api_get(client, f"/vcenter/network?names={name}&datacenters={dc_id}")
        if not items:
            raise RuntimeError(f"Network not found: {name!r}")
        return items[0]["network"]

    async def _ovf_networks(self, client: httpx.AsyncClient, library_item_id: str, rp_id: str) -> list[str]:
        """The network names an OVF template declares (its NICs' sections)."""
        result = await self._api_post(
            client,
            f"/vcenter/ovf/library-item/{library_item_id}?action=filter",
            json={"target": {"resource_pool_id": rp_id}},
        )
        return list(result.get("networks") or []) if isinstance(result, dict) else []

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
        network_mappings: dict[str, str] | None = None,
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
        try:
            await self._api_delete(client, f"/vcenter/vm/{vm_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:  # already gone counts as deleted: a retry is safe
                raise

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
                            dc_id,
                        )
                    )

                results = await asyncio.gather(*tasks, return_exceptions=True)
                for vm_def, res in zip(vm_defs, results):
                    if isinstance(res, Exception):
                        errors.append(f"VM {vm_def['name']}: {res}")
                    else:
                        vms_out.append(res)

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
        dc_id: str = "",
    ) -> dict:
        library_item_id = await self._find_library_item(client, template_name)
        # A VM on a leased, isolated port group (a lab session) has every OVF network
        # mapped onto it; otherwise the OVF's own default network applies.
        mappings = None
        if vm_def.get("port_group"):
            # Only the configured lab pool, whatever a template says: a range must never be
            # able to name an arbitrary datacenter network (management, another tenant's).
            allowed = {p.strip() for p in os.environ.get("LAB_PORT_GROUPS", "").split(",") if p.strip()}
            if vm_def["port_group"] not in allowed:
                raise RuntimeError(f"port group {vm_def['port_group']!r} is not a lab network (LAB_PORT_GROUPS)")
            network_id = await self._find_network(client, dc_id, vm_def["port_group"])
            ovf_networks = await self._ovf_networks(client, library_item_id, rp_id)
            mappings = {net: network_id for net in ovf_networks}
            if not mappings:
                raise RuntimeError(
                    f"template {template_name!r} declares no network to map onto {vm_def['port_group']!r}"
                )
        vm_id = await self._deploy_ovf(client, library_item_id, vm_name, folder_id, rp_id, ds_id, mappings)

        # Resize hardware to match vm_def
        hw_update: dict = {}
        if "cores" in vm_def:
            hw_update["cpu"] = {"count": vm_def["cores"], "hot_add_enabled": True}
        if "memory" in vm_def:
            hw_update["memory"] = {"size_MiB": vm_def["memory"], "hot_add_enabled": True}
        if hw_update:
            await self._api_patch(client, f"/vcenter/vm/{vm_id}/hardware", json=hw_update)

        await self._power_action(client, vm_id, "start")
        tools_ready = await self._wait_tools(client, vm_id)
        ip = await self._get_vm_ip(client, vm_id) if tools_ready else None

        return {
            "vm_id": vm_id,
            "name": vm_def["name"],
            "status": "running",
            "ip": ip or vm_def.get("ip", ""),
            "tools_ready": tools_ready,
        }

    async def find_vms(self, name_prefix: str) -> list[dict]:
        session = await self._get_session()
        async with self._client(session) as client:
            vms = await self._api_get(client, "/vcenter/vm")
        return [{"vm_id": v["vm"], "name": v["name"]} for v in vms if str(v.get("name", "")).startswith(name_prefix)]

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

    # The power state each action leaves a VM in (GET /vcenter/vm/{id}/power).
    _POWER_TARGET = {"stop": "POWERED_OFF", "start": "POWERED_ON"}

    async def _power_all(self, vms: list[dict], action: str) -> tuple[int, list[str]]:
        """Power every VM of a range. A VM already in the wanted state counts as done (a
        retry after a partial attempt, a guest that shut itself down, a VM powered by hand);
        vCenter answers ALREADY_IN_DESIRED_STATE for those, which used to fail the range.
        A VM with no recorded id is an error, never silently counted."""

        async def one(client: httpx.AsyncClient, vm: dict) -> None:
            if not vm.get("vm_id"):
                raise RuntimeError("no vm_id recorded")
            power = await self._api_get(client, f"/vcenter/vm/{vm['vm_id']}/power")
            if isinstance(power, dict) and power.get("state") == self._POWER_TARGET[action]:
                return
            try:
                await self._power_action(client, vm["vm_id"], action)
            except httpx.HTTPStatusError as exc:  # it got there between the read and the action
                if "ALREADY_IN_DESIRED_STATE" not in exc.response.text:
                    raise

        session = await self._get_session()
        async with self._client(session) as client:
            results = await asyncio.gather(*(one(client, vm) for vm in vms), return_exceptions=True)
        failed = [(vm, res) for vm, res in zip(vms, results, strict=True) if isinstance(res, Exception)]
        errors = [f"VM {vm.get('name')}: {res}" for vm, res in failed]
        return len(vms) - len(errors), errors

    async def stop(
        self,
        range_id: str,
        provision_output: dict,
    ) -> StopResult:
        start = time.monotonic()
        try:
            done, errors = await self._power_all(provision_output.get("vms", []), "stop")
        except Exception as exc:
            done, errors = 0, [str(exc)]
        return StopResult(
            status="ok" if not errors else "partial",
            vms_stopped=done,
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )

    async def start(
        self,
        range_id: str,
        provision_output: dict,
    ) -> StartResult:
        start = time.monotonic()
        try:
            done, errors = await self._power_all(provision_output.get("vms", []), "start")
        except Exception as exc:
            done, errors = 0, [str(exc)]
        return StartResult(
            status="ok" if not errors else "partial",
            vms_started=done,
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
