"""TrueNorth Range - VMware vSphere REST API provisioner.

Provisions VMs by cloning from a vSphere Content Library using the
vSphere Automation REST API (available since vSphere 6.7).  No
third-party SDK required — only httpx.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

from .base import BaseProvisioner
from .results import (
    DestroyResult,
    HealthResult,
    ProvisionResult,
    SnapshotResult,
    StartResult,
    StopResult,
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
        items = await self._api_get(
            client, f"/vcenter/cluster?names={self._cluster}&datacenters={dc_id}"
        )
        if not items:
            raise RuntimeError(f"Cluster not found: {self._cluster!r}")
        return items[0]["cluster"]

    async def _find_datastore(self, client: httpx.AsyncClient, dc_id: str) -> str:
        """Return the datastore MoRef ID for the configured datastore name."""
        items = await self._api_get(
            client, f"/vcenter/datastore?names={self._datastore}&datacenters={dc_id}"
        )
        if not items:
            raise RuntimeError(f"Datastore not found: {self._datastore!r}")
        return items[0]["datastore"]

    async def _find_network(self, client: httpx.AsyncClient, dc_id: str) -> str:
        """Return the network MoRef ID for the configured network name."""
        items = await self._api_get(
            client, f"/vcenter/network?names={self._network}&datacenters={dc_id}"
        )
        if not items:
            raise RuntimeError(f"Network not found: {self._network!r}")
        return items[0]["network"]

    async def _find_library_item(self, client: httpx.AsyncClient, template_name: str) -> str:
        """Return the Content Library item ID for the given OVF template name."""
        # Find the library first
        libs = await self._api_get(
            client, f"/content/library?action=find&spec={{\"name\":\"{self._content_library}\"}}"
        )
        if not libs:
            raise RuntimeError(f"Content Library not found: {self._content_library!r}")
        lib_id = libs[0] if isinstance(libs, list) else libs

        # Find the item within the library
        items = await self._api_post(
            client,
            f"/content/library/item?action=find",
            json={"name": template_name, "library_id": lib_id},
        )
        if not items:
            raise RuntimeError(
                f"Template {template_name!r} not found in library {self._content_library!r}"
            )
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

    async def _create_snapshot(self, client: httpx.AsyncClient, vm_id: str, name: str) -> None:
        """Create a named snapshot of a VM."""
        await self._api_post(
            client,
            f"/vcenter/vm/{vm_id}/snapshot",
            json={"name": name, "memory": False, "quiesce": False},
        )

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
                rp_items = await self._api_get(
                    client, f"/vcenter/resource-pool?clusters={cluster_id}"
                )
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
                    template_name = vm_def.get("template_name", "ubuntu-2204-cloud")
                    vm_name = f"{range_id}-{vm_def['name']}"
                    tasks.append(
                        self._provision_one_vm(
                            client, vm_def, vm_name, template_name,
                            folder_id, rp_id, ds_id,
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
    ) -> dict:
        library_item_id = await self._find_library_item(client, template_name)
        vm_id = await self._deploy_ovf(
            client, library_item_id, vm_name, folder_id, rp_id, ds_id
        )

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
                tasks = [
                    self._power_action(client, vm["vm_id"], "stop")
                    for vm in vms if vm.get("vm_id")
                ]
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
                tasks = [
                    self._power_action(client, vm["vm_id"], "start")
                    for vm in vms if vm.get("vm_id")
                ]
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
        errors: list[str] = []
        vms = provision_output.get("vms", [])

        try:
            session = await self._get_session()
            async with self._client(session) as client:
                tasks = [
                    self._create_snapshot(client, vm["vm_id"], name)
                    for vm in vms if vm.get("vm_id")
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for vm, res in zip(vms, results):
                    if isinstance(res, Exception):
                        errors.append(f"VM {vm.get('name')}: {res}")
        except Exception as exc:
            errors.append(str(exc))

        return SnapshotResult(
            status="ok" if not errors else "partial",
            snapshot_name=name,
            vms_snapped=len(vms) - len(errors),
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
