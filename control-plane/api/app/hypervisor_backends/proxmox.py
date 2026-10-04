"""Proxmox VE connection backend (legacy; vSphere is the range platform)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import HypervisorConnection, HypervisorNode
from ..schemas import HypervisorTestResult
from .base import BaseHypervisorBackend


def proxmox_client(
    host: str, *, user: str, password: str, verify_ssl: bool, port: int | None = None, timeout: int = 15
) -> Any:
    """The one place the API constructs a proxmoxer client. ``routers/proxmox.py`` uses it too."""
    from proxmoxer import ProxmoxAPI

    kwargs: dict[str, Any] = {"user": user, "password": password, "verify_ssl": verify_ssl, "timeout": timeout}
    if port is not None:
        kwargs["port"] = port
    return ProxmoxAPI(host, **kwargs)


class ProxmoxBackend(BaseHypervisorBackend):
    kind = "proxmox"

    def check_connection(self, conn, db):
        return check_connection(conn, db)

    def discover(self, conn_id, conn, db):
        return discover(conn_id, conn, db)


def _client_for(conn: HypervisorConnection, timeout: int) -> Any:
    return proxmox_client(
        conn.host,
        user=conn.username,
        password=conn.password_encrypted or "",
        verify_ssl=conn.verify_ssl,
        port=conn.port,
        timeout=timeout,
    )


def check_connection(conn: HypervisorConnection, db) -> HypervisorTestResult:
    try:
        prox = _client_for(conn, timeout=10)
        version_info = prox.version.get()
        nodes = prox.nodes.get()
        conn.is_active = True
        conn.last_seen_at = datetime.utcnow()
        db.commit()
        return HypervisorTestResult(
            success=True,
            message=f"Connected to Proxmox VE {version_info.get('version', '?')}",
            version=version_info.get("version", "unknown"),
            nodes_found=len(nodes),
        )
    except Exception as exc:
        conn.is_active = False
        db.commit()
        return HypervisorTestResult(
            success=False,
            message=f"Connection failed: {exc}",
            version="n/a",
            nodes_found=0,
        )


def _resolve_node_ip(prox, node_name: str, fallback: str) -> str:
    """Try to resolve the real management IP for a Proxmox node.

    Queries the node's network interfaces for the one carrying the
    management address (usually vmbr0 or the interface with a gateway).
    Falls back to the connection host if unavailable.
    """
    try:
        ifaces = prox.nodes(node_name).network.get()
        for iface in ifaces:
            # Prefer the interface with a gateway (management bridge)
            if iface.get("gateway") and iface.get("address"):
                return iface["address"]
        # No gateway found — try any bridge with an address
        for iface in ifaces:
            if iface.get("address") and iface.get("type") == "bridge":
                return iface["address"]
    except Exception:
        pass
    return fallback


def discover(conn_id, conn: HypervisorConnection, db) -> dict:
    try:
        prox = _client_for(conn, timeout=15)
        api_nodes = prox.nodes.get()
        discovered = 0
        for n in api_nodes:
            node_name = n.get("node", "unknown")

            # ── CROSS-CONNECTION DEDUP ──
            # Check if this node already exists under ANY connection
            # (both nodes in a cluster are visible from either connection)
            existing = db.query(HypervisorNode).filter_by(node_name=node_name).first()

            maxcpu = n.get("maxcpu", 0)
            maxmem_gb = round(n.get("maxmem", 0) / (1024**3), 1)
            mem_used_gb = round(n.get("mem", 0) / (1024**3), 1)
            cpu_pct = round(n.get("cpu", 0) * 100, 1)
            status = "online" if n.get("status") == "online" else "offline"

            # Resolve real management IP from the node's own network config
            ip_address = _resolve_node_ip(prox, node_name, conn.host)

            # Count VMs on this node
            try:
                vms = prox.nodes(node_name).qemu.get()
                vm_count = len(vms)
            except Exception:
                vm_count = 0
            # Count storage
            try:
                storages = prox.nodes(node_name).storage.get()
                total_gb = sum(s.get("total", 0) / (1024**3) for s in storages)
                used_gb = sum(s.get("used", 0) / (1024**3) for s in storages)
            except Exception:
                total_gb = 0
                used_gb = 0
            if existing:
                # Update existing row (may belong to a different connection)
                existing.status = status
                existing.cpu_total = maxcpu
                existing.cpu_used = cpu_pct
                existing.memory_total_gb = maxmem_gb
                existing.memory_used_gb = mem_used_gb
                existing.storage_total_gb = round(total_gb, 1)
                existing.storage_used_gb = round(used_gb, 1)
                existing.vm_count = vm_count
                existing.ip_address = ip_address
            else:
                node_obj = HypervisorNode(
                    connection_id=str(conn_id),
                    node_name=node_name,
                    ip_address=ip_address,
                    status=status,
                    cpu_total=maxcpu,
                    cpu_used=cpu_pct,
                    memory_total_gb=maxmem_gb,
                    memory_used_gb=mem_used_gb,
                    storage_total_gb=round(total_gb, 1),
                    storage_used_gb=round(used_gb, 1),
                    vm_count=vm_count,
                )
                db.add(node_obj)
                discovered += 1
        conn.is_active = True
        conn.last_seen_at = datetime.utcnow()
        db.commit()
        return {
            "message": f"Discovered {discovered} new nodes ({len(api_nodes)} total)",
            "nodes_discovered": discovered,
        }
    except Exception as exc:
        return {"message": f"Discovery failed: {exc}", "nodes_discovered": 0}
