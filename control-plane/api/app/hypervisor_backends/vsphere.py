"""vSphere connection backend — vCenter Automation REST API (the range platform)."""

from __future__ import annotations

import ipaddress
from datetime import datetime

import httpx

from ..models import HypervisorConnection, HypervisorNode
from ..schemas import HypervisorTestResult
from ..secretbox import unseal
from .base import BaseHypervisorBackend


class VSphereBackend(BaseHypervisorBackend):
    kind = "vsphere"

    def check_connection(self, conn, db):
        return check_connection(conn, db)

    def discover(self, conn_id, conn, db):
        return discover(conn_id, conn, db)


def check_connection(conn: HypervisorConnection, db) -> HypervisorTestResult:
    """Test a vSphere connection via the vCenter Automation REST API."""
    scheme = "https" if conn.verify_ssl else "https"
    base = f"{scheme}://{conn.host}"
    # Outside the try: a missing or wrong TN_SECRETS_KEY is our fault, not the vCenter's,
    # and must not mark the connection inactive (it surfaces as a 503 naming the key).
    password = unseal(conn.password_encrypted) or ""
    try:
        with httpx.Client(verify=conn.verify_ssl, timeout=10) as client:
            # Create a session
            r = client.post(f"{base}/api/session", auth=(conn.username, password))
            r.raise_for_status()
            token = r.json()

            # Retrieve version info from /api/vcenter/system/version
            rv = client.get(
                f"{base}/api/vcenter/system/version",
                headers={"vmware-api-session-id": token},
            )
            rv.raise_for_status()
            ver_info = rv.json()

            # List hosts
            rh = client.get(
                f"{base}/api/vcenter/host",
                headers={"vmware-api-session-id": token},
            )
            rh.raise_for_status()
            hosts = rh.json()

            # Terminate the session
            client.delete(f"{base}/api/session", headers={"vmware-api-session-id": token})

        version = ver_info.get("version", "unknown")
        conn.is_active = True
        conn.last_seen_at = datetime.utcnow()
        db.commit()
        return HypervisorTestResult(
            success=True,
            message=f"Connected to vCenter {version}",
            version=version,
            nodes_found=len(hosts) if isinstance(hosts, list) else 0,
        )
    except Exception as exc:
        conn.is_active = False
        db.commit()
        return HypervisorTestResult(success=False, message=f"Connection failed: {exc}", version="n/a", nodes_found=0)


def _host_ip(name: str) -> str | None:
    """An ESXi host's inventory name is its IP when it was added by address, else an FQDN."""
    try:
        return str(ipaddress.ip_address(name))
    except ValueError:
        return None


def discover(conn_id, conn: HypervisorConnection, db) -> dict:
    """Discover ESXi hosts via the vCenter Automation REST API.

    That API reports each host's name and connection state, and lists VMs per host, but
    has no host CPU or memory figures. Those are left empty (None, "not reported"), never
    0, which would read as an idle host. Host capacity needs pyVmomi or the VI/JSON API.
    """
    base = f"https://{conn.host}"
    password = unseal(conn.password_encrypted) or ""  # outside the try, as in check_connection
    try:
        with httpx.Client(verify=conn.verify_ssl, timeout=15) as client:
            r = client.post(f"{base}/api/session", auth=(conn.username, password))
            r.raise_for_status()
            token = r.json()
            headers = {"vmware-api-session-id": token}

            hosts_resp = client.get(f"{base}/api/vcenter/host", headers=headers)
            hosts_resp.raise_for_status()
            hosts = hosts_resp.json()

            now = datetime.utcnow()
            discovered = 0
            for h in hosts:
                node_name = h.get("name", h.get("host", "unknown"))
                # Scoped to this vCenter: two vCenters can each have a host called esxi01.
                existing = (
                    db.query(HypervisorNode)
                    .filter(HypervisorNode.connection_id == str(conn_id), HypervisorNode.node_name == node_name)
                    .first()
                )
                status = "online" if h.get("connection_state") == "CONNECTED" else "offline"

                vm_count = existing.vm_count if existing else 0
                if h.get("host"):
                    try:
                        vms_resp = client.get(f"{base}/api/vcenter/vm", params={"hosts": h["host"]}, headers=headers)
                        vms_resp.raise_for_status()
                        vm_count = len(vms_resp.json())
                    except httpx.HTTPError:
                        pass  # keep the last known count rather than report an empty host

                if existing:
                    existing.status = status
                    existing.vm_count = vm_count
                    existing.ip_address = _host_ip(node_name)
                    existing.last_seen_at = now
                else:
                    db.add(
                        HypervisorNode(
                            connection_id=str(conn_id),
                            node_name=node_name,
                            ip_address=_host_ip(node_name),
                            status=status,
                            vm_count=vm_count,
                            last_seen_at=now,
                        )
                    )
                    discovered += 1

            client.delete(f"{base}/api/session", headers=headers)

        conn.is_active = True
        conn.last_seen_at = datetime.utcnow()
        db.commit()
        return {
            "message": f"Discovered {discovered} new hosts ({len(hosts)} total)",
            "nodes_discovered": discovered,
        }
    except Exception as exc:
        return {"message": f"vSphere discovery failed: {exc}", "nodes_discovered": 0}
