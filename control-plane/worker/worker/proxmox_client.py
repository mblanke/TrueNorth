"""TrueNorth Range - Async Proxmox API client for VM management at scale.

Features:
  - API token authentication (not password)
  - Connection pooling via httpx
  - Task polling with configurable timeout
  - Bulk operations with semaphore-controlled concurrency
  - Retry with exponential backoff on 5xx errors
  - Structured logging
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("truenorth.proxmox")

_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.5


class ProxmoxError(Exception):
    """Raised when a Proxmox API call fails."""

    def __init__(self, status: int, message: str, endpoint: str = "") -> None:
        self.status = status
        self.endpoint = endpoint
        super().__init__(f"[{status}] {endpoint}: {message}")


class ProxmoxClient:
    """Async Proxmox API client for VM management at scale."""

    def __init__(
        self,
        url: str,
        user: str,
        token_name: str,
        token_value: str,
        *,
        verify_ssl: bool = False,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base = url.rstrip("/")
        self._auth_header = f"PVEAPIToken={user}!{token_name}={token_value}"
        self._client = httpx.AsyncClient(
            base_url=f"{self._base}/api2/json",
            headers={
                "Authorization": self._auth_header,
                "Accept": "application/json",
            },
            verify=verify_ssl,
            timeout=httpx.Timeout(timeout, connect=10.0),
        )
        logger.info("ProxmoxClient initialised for %s (user=%s)", self._base, user)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> "ProxmoxClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Issue an HTTP request with retry on 5xx."""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.request(
                    method, path, data=data, params=params,
                )
                if resp.status_code >= 500:
                    raise ProxmoxError(resp.status_code, resp.text, path)
                if resp.status_code >= 400:
                    raise ProxmoxError(resp.status_code, resp.text, path)
                body = resp.json()
                return body.get("data", body)
            except (httpx.TransportError, ProxmoxError) as exc:
                last_exc = exc
                if isinstance(exc, ProxmoxError) and exc.status < 500:
                    raise
                wait = _BACKOFF_BASE ** attempt
                logger.warning(
                    "Proxmox %s %s attempt %d failed: %s — retrying in %.1fs",
                    method, path, attempt + 1, exc, wait,
                )
                await asyncio.sleep(wait)
        raise last_exc  # type: ignore[misc]

    async def _get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params=params or None)

    async def _post(self, path: str, **data: Any) -> Any:
        return await self._request("POST", path, data=data or None)

    async def _put(self, path: str, **data: Any) -> Any:
        return await self._request("PUT", path, data=data or None)

    async def _delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self) -> None:
        """Verify the API token is valid by fetching cluster version."""
        info = await self._get("/version")
        logger.info("Authenticated — Proxmox VE %s", info.get("version", "unknown"))

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    async def list_nodes(self) -> list[dict[str, Any]]:
        """Return all cluster nodes."""
        return await self._get("/nodes")

    async def get_node_status(self, node: str) -> dict[str, Any]:
        """Return status for a specific node."""
        return await self._get(f"/nodes/{node}/status")

    # ------------------------------------------------------------------
    # VMs
    # ------------------------------------------------------------------

    async def clone_vm(
        self,
        node: str,
        template_vmid: int,
        new_vmid: int,
        name: str,
        **kwargs: Any,
    ) -> str:
        """Clone a template VM. Returns the Proxmox task ID (UPID)."""
        payload = {"newid": new_vmid, "name": name, "full": 1, **kwargs}
        return await self._post(f"/nodes/{node}/qemu/{template_vmid}/clone", **payload)

    async def start_vm(self, node: str, vmid: int) -> str:
        """Start a VM. Returns task ID."""
        return await self._post(f"/nodes/{node}/qemu/{vmid}/status/start")

    async def stop_vm(self, node: str, vmid: int) -> str:
        """Stop a VM. Returns task ID."""
        return await self._post(f"/nodes/{node}/qemu/{vmid}/status/stop")

    async def delete_vm(self, node: str, vmid: int) -> str:
        """Delete a VM. Returns task ID."""
        return await self._delete(f"/nodes/{node}/qemu/{vmid}")

    async def get_vm_status(self, node: str, vmid: int) -> dict[str, Any]:
        """Get current status of a VM."""
        return await self._get(f"/nodes/{node}/qemu/{vmid}/status/current")

    async def get_vm_config(self, node: str, vmid: int) -> dict[str, Any]:
        """Get full VM configuration."""
        return await self._get(f"/nodes/{node}/qemu/{vmid}/config")

    async def update_vm_config(self, node: str, vmid: int, **config: Any) -> str:
        """Update VM configuration parameters."""
        return await self._put(f"/nodes/{node}/qemu/{vmid}/config", **config)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    async def create_snapshot(self, node: str, vmid: int, name: str) -> str:
        """Create a VM snapshot. Returns task ID."""
        return await self._post(f"/nodes/{node}/qemu/{vmid}/snapshot", snapname=name)

    async def rollback_snapshot(self, node: str, vmid: int, name: str) -> str:
        """Rollback a VM to a named snapshot. Returns task ID."""
        return await self._post(f"/nodes/{node}/qemu/{vmid}/snapshot/{name}/rollback")

    async def list_snapshots(self, node: str, vmid: int) -> list[dict[str, Any]]:
        """List all snapshots for a VM."""
        return await self._get(f"/nodes/{node}/qemu/{vmid}/snapshot")

    async def delete_snapshot(self, node: str, vmid: int, name: str) -> str:
        """Delete a VM snapshot. Returns task ID."""
        return await self._delete(f"/nodes/{node}/qemu/{vmid}/snapshot/{name}")

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    async def wait_for_task(
        self, node: str, task_id: str, timeout: int = 300, poll_interval: float = 2.0,
    ) -> dict[str, Any]:
        """Poll a Proxmox task until completion or timeout.

        Returns the final task status dict.
        Raises ``TimeoutError`` if the task does not finish in time.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = await self._get(f"/nodes/{node}/tasks/{task_id}/status")
            if status.get("status") == "stopped":
                exit_status = status.get("exitstatus", "")
                if exit_status != "OK":
                    raise ProxmoxError(500, f"Task {task_id} failed: {exit_status}", "task")
                logger.info("Task %s completed OK", task_id)
                return status
            await asyncio.sleep(poll_interval)
        raise TimeoutError(f"Task {task_id} did not complete within {timeout}s")

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    async def list_networks(self, node: str) -> list[dict[str, Any]]:
        """List all network interfaces / bridges on a node."""
        return await self._get(f"/nodes/{node}/network")

    async def create_bridge(
        self, node: str, name: str, vlan_aware: bool = True,
    ) -> None:
        """Create a Linux bridge on a node."""
        await self._post(
            f"/nodes/{node}/network",
            iface=name,
            type="bridge",
            autostart=1,
            vlan_aware=int(vlan_aware),
        )
        logger.info("Created bridge %s on node %s", name, node)

    # ------------------------------------------------------------------
    # Firewall
    # ------------------------------------------------------------------

    async def set_firewall_rule(
        self, node: str, vmid: int, rule: dict[str, Any],
    ) -> None:
        """Add a firewall rule to a VM."""
        await self._post(f"/nodes/{node}/qemu/{vmid}/firewall/rules", **rule)

    # ------------------------------------------------------------------
    # Bulk operations (scale-optimised)
    # ------------------------------------------------------------------

    async def bulk_clone(
        self,
        node: str,
        template_vmid: int,
        vms: list[dict[str, Any]],
        concurrency: int = 10,
    ) -> list[dict[str, Any]]:
        """Clone multiple VMs from a template with bounded concurrency.

        Each entry in *vms* must have ``vmid`` and ``name`` keys.
        Returns a list of ``{vmid, name, task_id, status}`` dicts.
        """
        sem = asyncio.Semaphore(concurrency)
        results: list[dict[str, Any]] = []

        async def _clone_one(vm: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                try:
                    tid = await self.clone_vm(
                        node, template_vmid, vm["vmid"], vm["name"],
                        **{k: v for k, v in vm.items() if k not in ("vmid", "name")},
                    )
                    return {"vmid": vm["vmid"], "name": vm["name"], "task_id": tid, "status": "cloning"}
                except Exception as exc:
                    logger.error("Clone failed for VMID %s: %s", vm["vmid"], exc)
                    return {"vmid": vm["vmid"], "name": vm["name"], "task_id": None, "status": f"error: {exc}"}

        tasks = [_clone_one(vm) for vm in vms]
        results = await asyncio.gather(*tasks)
        logger.info("Bulk clone: %d/%d initiated for node %s", sum(1 for r in results if r["task_id"]), len(vms), node)
        return results

    async def _bulk_action(
        self,
        node: str,
        vmids: list[int],
        action_fn: Any,
        concurrency: int,
        action_name: str,
    ) -> list[dict[str, Any]]:
        sem = asyncio.Semaphore(concurrency)
        results: list[dict[str, Any]] = []

        async def _do(vmid: int) -> dict[str, Any]:
            async with sem:
                try:
                    tid = await action_fn(node, vmid)
                    return {"vmid": vmid, "task_id": tid, "status": action_name}
                except Exception as exc:
                    logger.error("%s failed for VMID %d: %s", action_name, vmid, exc)
                    return {"vmid": vmid, "task_id": None, "status": f"error: {exc}"}

        tasks = [_do(vmid) for vmid in vmids]
        results = await asyncio.gather(*tasks)
        ok = sum(1 for r in results if r["task_id"])
        logger.info("Bulk %s: %d/%d on node %s", action_name, ok, len(vmids), node)
        return results

    async def bulk_start(
        self, node: str, vmids: list[int], concurrency: int = 20,
    ) -> list[dict[str, Any]]:
        """Start multiple VMs with bounded concurrency."""
        return await self._bulk_action(node, vmids, self.start_vm, concurrency, "start")

    async def bulk_stop(
        self, node: str, vmids: list[int], concurrency: int = 20,
    ) -> list[dict[str, Any]]:
        """Stop multiple VMs with bounded concurrency."""
        return await self._bulk_action(node, vmids, self.stop_vm, concurrency, "stop")

    async def bulk_delete(
        self, node: str, vmids: list[int], concurrency: int = 10,
    ) -> list[dict[str, Any]]:
        """Delete multiple VMs with bounded concurrency."""
        return await self._bulk_action(node, vmids, self.delete_vm, concurrency, "delete")