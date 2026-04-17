"""TrueNorth Range - Proxmox VE API provisioner.

Provisions VMs directly via the Proxmox REST API using httpx,
with concurrent clone operations gated by a semaphore.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid

try:
    import httpx
except ImportError:  # allow import even when httpx is not installed
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
PROXMOX_URL: str = os.environ.get("PROXMOX_URL", "https://proxmox.local:8006")
PROXMOX_TOKEN_ID: str = os.environ.get("PROXMOX_TOKEN_ID", "")
PROXMOX_TOKEN_SECRET: str = os.environ.get("PROXMOX_TOKEN_SECRET", "")
PROXMOX_NODE: str = os.environ.get("PROXMOX_NODE", "pve")
PROXMOX_VERIFY_SSL: bool = os.environ.get("PROXMOX_VERIFY_SSL", "false").lower() == "true"
PROXMOX_CONCURRENCY: int = int(os.environ.get("PROXMOX_CONCURRENCY", "4"))
PROXMOX_AGENT_TIMEOUT: int = int(os.environ.get("PROXMOX_AGENT_TIMEOUT", "120"))


class ProxmoxAPIProvisioner(BaseProvisioner):
    """Proxmox VE REST API provisioner with concurrent VM operations."""

    def __init__(self) -> None:
        self._base_url = PROXMOX_URL.rstrip("/")
        self._node = PROXMOX_NODE
        self._semaphore = asyncio.Semaphore(PROXMOX_CONCURRENCY)
        self._agent_timeout = PROXMOX_AGENT_TIMEOUT
        self._headers = {
            "Authorization": f"PVEAPIToken={PROXMOX_TOKEN_ID}={PROXMOX_TOKEN_SECRET}",
        }
        self._verify_ssl = PROXMOX_VERIFY_SSL

    # ------------------------------------------------------------------ #
    # HTTP helpers
    # ------------------------------------------------------------------ #

    def _client(self) -> httpx.AsyncClient:
        if httpx is None:
            raise RuntimeError("httpx is required for ProxmoxAPIProvisioner")
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            verify=self._verify_ssl,
            timeout=60.0,
        )

    async def _api_get(self, client: httpx.AsyncClient, path: str) -> dict:
        resp = await client.get(f"/api2/json{path}")
        resp.raise_for_status()
        return resp.json().get("data", {})

    async def _api_post(self, client: httpx.AsyncClient, path: str, **kwargs) -> dict:
        resp = await client.post(f"/api2/json{path}", **kwargs)
        resp.raise_for_status()
        return resp.json().get("data", {})

    # ------------------------------------------------------------------ #
    # VM operations
    # ------------------------------------------------------------------ #

    async def _clone_vm(
        self,
        client: httpx.AsyncClient,
        template_vmid: int,
        new_vmid: int,
        name: str,
        cloud_init: dict | None = None,
    ) -> dict:
        """Clone a template VM and optionally apply cloud-init config."""
        async with self._semaphore:
            logger.info("Cloning VM %d -> %d (%s)", template_vmid, new_vmid, name)
            data = await self._api_post(
                client,
                f"/nodes/{self._node}/qemu/{template_vmid}/clone",
                data={"newid": new_vmid, "name": name, "full": 1},
            )
            upid = data if isinstance(data, str) else data.get("data", "")
            # Wait for clone task
            await self._wait_task(client, upid)

            # Apply cloud-init if provided
            if cloud_init:
                ci_params = {}
                if "ip" in cloud_init:
                    ci_params["ipconfig0"] = f"ip={cloud_init['ip']}/24,gw={cloud_init.get('gateway', '10.0.1.1')}"
                if "nameserver" in cloud_init:
                    ci_params["nameserver"] = cloud_init["nameserver"]
                if "sshkeys" in cloud_init:
                    ci_params["sshkeys"] = cloud_init["sshkeys"]
                if ci_params:
                    await self._api_post(
                        client,
                        f"/nodes/{self._node}/qemu/{new_vmid}/config",
                        data=ci_params,
                    )

            return {"vmid": new_vmid, "name": name, "status": "stopped"}

    async def _start_vm(self, client: httpx.AsyncClient, vmid: int) -> None:
        async with self._semaphore:
            await self._api_post(client, f"/nodes/{self._node}/qemu/{vmid}/status/start")

    async def _stop_vm(self, client: httpx.AsyncClient, vmid: int) -> None:
        async with self._semaphore:
            await self._api_post(client, f"/nodes/{self._node}/qemu/{vmid}/status/stop")

    async def _destroy_vm(self, client: httpx.AsyncClient, vmid: int) -> None:
        async with self._semaphore:
            await self._api_post(client, f"/nodes/{self._node}/qemu/{vmid}/status/stop")
            await asyncio.sleep(2)
            resp = await client.delete(f"/api2/json/nodes/{self._node}/qemu/{vmid}")
            resp.raise_for_status()

    async def _wait_task(self, client: httpx.AsyncClient, upid: str, timeout: int = 120) -> None:
        """Poll a Proxmox task until completion."""
        if not upid:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = await self._api_get(client, f"/nodes/{self._node}/tasks/{upid}/status")
            if data.get("status") == "stopped":
                return
            await asyncio.sleep(2)
        logger.warning("Task %s did not complete within %ds", upid, timeout)

    async def _wait_qemu_agent(self, client: httpx.AsyncClient, vmid: int) -> bool:
        """Wait until QEMU guest agent responds."""
        deadline = time.monotonic() + self._agent_timeout
        while time.monotonic() < deadline:
            try:
                await self._api_get(
                    client,
                    f"/nodes/{self._node}/qemu/{vmid}/agent/ping",
                )
                return True
            except Exception:
                await asyncio.sleep(3)
        logger.warning("QEMU agent not ready for VM %d after %ds", vmid, self._agent_timeout)
        return False

    def _next_vmid(self, base: int, idx: int) -> int:
        return base + idx

    # ------------------------------------------------------------------ #
    # provision
    # ------------------------------------------------------------------ #
    async def provision(
        self,
        range_id: str,
        template: dict,
        allocations: dict,
    ) -> ProvisionResult:
        start = time.monotonic()
        errors: list[str] = []
        vms: list[dict] = []
        networks: list[dict] = []
        base_vmid = allocations.get("base_vmid", 9000)

        async with self._client() as client:
            # Clone VMs concurrently
            clone_tasks = []
            vm_defs = template.get("vms", [])
            for idx, vm_def in enumerate(vm_defs):
                new_vmid = self._next_vmid(base_vmid, idx)
                clone_tasks.append(
                    self._clone_vm(
                        client,
                        template_vmid=vm_def.get("template_vmid", 100),
                        new_vmid=new_vmid,
                        name=vm_def.get("name", f"vm-{idx}"),
                        cloud_init=vm_def.get("cloud_init"),
                    )
                )

            results = await asyncio.gather(*clone_tasks, return_exceptions=True)

            for idx, result in enumerate(results):
                if isinstance(result, Exception):
                    errors.append(f"Clone failed for VM index {idx}: {result}")
                else:
                    vms.append(result)

            # Start all cloned VMs concurrently
            start_tasks = []
            for vm in vms:
                start_tasks.append(self._start_vm(client, vm["vmid"]))
            await asyncio.gather(*start_tasks, return_exceptions=True)

            # Wait for QEMU agent on each VM
            agent_tasks = []
            for vm in vms:
                agent_tasks.append(self._wait_qemu_agent(client, vm["vmid"]))
            agent_results = await asyncio.gather(*agent_tasks, return_exceptions=True)

            for vm, agent_ok in zip(vms, agent_results, strict=False):
                if agent_ok is True:
                    vm["status"] = "running"
                    # Retrieve IP from QEMU agent
                    try:
                        net_info = await self._api_get(
                            client,
                            f"/nodes/{self._node}/qemu/{vm['vmid']}/agent/network-get-interfaces",
                        )
                        for iface in net_info.get("result", []):
                            for addr in iface.get("ip-addresses", []):
                                if addr.get("ip-address-type") == "ipv4" and not addr["ip-address"].startswith("127."):
                                    vm["ip"] = addr["ip-address"]
                                    break
                    except Exception:
                        pass
                else:
                    vm["status"] = "agent_timeout"

            # Collect network info from allocations
            for net in template.get("networks", []):
                networks.append(
                    {
                        "network_id": str(uuid.uuid4()),
                        "name": net.get("name", "vmbr0"),
                        "cidr": net.get("cidr", "10.0.1.0/24"),
                        "vlan_id": net.get("vlan_id"),
                    }
                )

        status = "ok" if not errors else ("partial" if vms else "failed")
        return ProvisionResult(
            status=status,
            vms=vms,
            networks=networks,
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # destroy
    # ------------------------------------------------------------------ #
    async def destroy(
        self,
        range_id: str,
        provision_output: dict,
    ) -> DestroyResult:
        start = time.monotonic()
        errors: list[str] = []
        removed = 0

        vms = provision_output.get("vms", [])
        async with self._client() as client:
            tasks = []
            for vm in vms:
                vmid = vm.get("vmid")
                if vmid:
                    tasks.append(self._destroy_vm(client, vmid))
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for _idx, r in enumerate(results):
                if isinstance(r, Exception):
                    errors.append(f"Destroy VM failed: {r}")
                else:
                    removed += 1

        status = "ok" if not errors else ("partial" if removed else "failed")
        return DestroyResult(
            status=status,
            resources_removed=removed,
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # stop
    # ------------------------------------------------------------------ #
    async def stop(
        self,
        range_id: str,
        provision_output: dict,
    ) -> StopResult:
        start = time.monotonic()
        errors: list[str] = []
        stopped = 0

        vms = provision_output.get("vms", [])
        async with self._client() as client:
            tasks = [self._stop_vm(client, vm["vmid"]) for vm in vms if "vmid" in vm]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    errors.append(str(r))
                else:
                    stopped += 1

        return StopResult(
            status="ok" if not errors else "partial",
            vms_stopped=stopped,
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # start
    # ------------------------------------------------------------------ #
    async def start(
        self,
        range_id: str,
        provision_output: dict,
    ) -> StartResult:
        start = time.monotonic()
        errors: list[str] = []
        started = 0

        vms = provision_output.get("vms", [])
        async with self._client() as client:
            tasks = [self._start_vm(client, vm["vmid"]) for vm in vms if "vmid" in vm]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    errors.append(str(r))
                else:
                    started += 1

        return StartResult(
            status="ok" if not errors else "partial",
            vms_started=started,
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # snapshot
    # ------------------------------------------------------------------ #
    async def snapshot(
        self,
        range_id: str,
        provision_output: dict,
        name: str,
    ) -> SnapshotResult:
        start = time.monotonic()
        errors: list[str] = []
        snapped = 0

        vms = provision_output.get("vms", [])
        async with self._client() as client:
            for vm in vms:
                vmid = vm.get("vmid")
                if not vmid:
                    continue
                try:
                    async with self._semaphore:
                        await self._api_post(
                            client,
                            f"/nodes/{self._node}/qemu/{vmid}/snapshot",
                            data={"snapname": name, "vmstate": 1},
                        )
                    snapped += 1
                except Exception as exc:
                    errors.append(f"Snapshot VM {vmid} failed: {exc}")

        return SnapshotResult(
            status="ok" if not errors else "partial",
            snapshot_name=name,
            vms_snapped=snapped,
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    # health_check
    # ------------------------------------------------------------------ #
    async def health_check(
        self,
        range_id: str,
        provision_output: dict,
    ) -> HealthResult:
        start = time.monotonic()
        errors: list[str] = []
        vm_statuses: list[dict] = []
        all_healthy = True

        vms = provision_output.get("vms", [])
        async with self._client() as client:
            for vm in vms:
                vmid = vm.get("vmid")
                if not vmid:
                    continue
                try:
                    data = await self._api_get(
                        client,
                        f"/nodes/{self._node}/qemu/{vmid}/status/current",
                    )
                    status = data.get("status", "unknown")
                    healthy = status == "running"
                    if not healthy:
                        all_healthy = False
                    vm_statuses.append(
                        {
                            "vmid": vmid,
                            "name": vm.get("name", ""),
                            "status": status,
                            "healthy": healthy,
                        }
                    )
                except Exception as exc:
                    all_healthy = False
                    errors.append(f"Health check VM {vmid} failed: {exc}")
                    vm_statuses.append(
                        {
                            "vmid": vmid,
                            "name": vm.get("name", ""),
                            "status": "error",
                            "healthy": False,
                        }
                    )

        overall = "ok" if all_healthy else ("degraded" if vm_statuses else "unhealthy")
        return HealthResult(
            healthy=all_healthy,
            status=overall,
            vm_statuses=vm_statuses,
            duration_seconds=time.monotonic() - start,
            errors=errors,
        )
