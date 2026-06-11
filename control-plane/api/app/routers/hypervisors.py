"""TrueNorth Range -- Hypervisor Configuration Router.

DB-backed CRUD for hypervisor connections (Proxmox, vSphere, Hyper-V),
node discovery, pool management, and connection testing.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    HypervisorConnection,
    HypervisorNode,
    HypervisorPool,
)
from ..schemas import (
    HypervisorConnectionIn,
    HypervisorConnectionOut,
    HypervisorConnectionUpdate,
    HypervisorNodeOut,
    HypervisorPoolOut,
    HypervisorSummaryOut,
    HypervisorTestResult,
)

router = APIRouter(prefix="/hypervisors", tags=["Infrastructure"])


# -- Connections CRUD ----------------------------------------------------
@router.get("/connections", response_model=list[HypervisorConnectionOut])
def list_connections(db: Session = Depends(get_db)):
    return db.query(HypervisorConnection).order_by(HypervisorConnection.created_at.desc()).all()


@router.post("/connections", response_model=HypervisorConnectionOut, status_code=201)
def create_connection(payload: HypervisorConnectionIn, db: Session = Depends(get_db)):
    conn = HypervisorConnection(
        name=payload.name,
        hypervisor_type=payload.hypervisor_type,
        host=payload.host,
        port=payload.port,
        username=payload.username,
        password_encrypted=payload.password,
        api_token=payload.api_token,
        verify_ssl=payload.verify_ssl,
        is_primary=payload.is_primary,
        datacenter=payload.datacenter,
        notes=payload.notes,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


@router.get("/connections/{conn_id}", response_model=HypervisorConnectionOut)
def get_connection(conn_id: uuid.UUID, db: Session = Depends(get_db)):
    conn = db.get(HypervisorConnection, str(conn_id))
    if not conn:
        raise HTTPException(404, "Connection not found")
    return conn


@router.patch("/connections/{conn_id}", response_model=HypervisorConnectionOut)
def update_connection(conn_id: uuid.UUID, payload: HypervisorConnectionUpdate, db: Session = Depends(get_db)):
    conn = db.get(HypervisorConnection, str(conn_id))
    if not conn:
        raise HTTPException(404, "Connection not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field == "password":
            conn.password_encrypted = value
        else:
            setattr(conn, field, value)
    db.commit()
    db.refresh(conn)
    return conn


@router.delete("/connections/{conn_id}", status_code=204, response_class=Response)
def delete_connection(conn_id: uuid.UUID, db: Session = Depends(get_db)):
    conn = db.get(HypervisorConnection, str(conn_id))
    if not conn:
        raise HTTPException(404, "Connection not found")
    db.delete(conn)
    db.commit()


# -- Connection Testing --------------------------------------------------
@router.post("/connections/{conn_id}/test", response_model=HypervisorTestResult)
def test_connection(conn_id: uuid.UUID, db: Session = Depends(get_db)):
    conn = db.get(HypervisorConnection, str(conn_id))
    if not conn:
        raise HTTPException(404, "Connection not found")
    if conn.hypervisor_type == "proxmox":
        return _test_proxmox(conn, db)
    if conn.hypervisor_type == "vsphere":
        return _test_vsphere(conn, db)
    if conn.hypervisor_type == "hyperv":
        return _test_hyperv(conn, db)
    return HypervisorTestResult(
        success=False,
        message=f"Unsupported hypervisor type: {conn.hypervisor_type}",
        version="n/a",
        nodes_found=0,
    )


def _test_proxmox(conn: HypervisorConnection, db) -> HypervisorTestResult:
    try:
        from proxmoxer import ProxmoxAPI

        prox = ProxmoxAPI(
            conn.host,
            user=conn.username,
            password=conn.password_encrypted or "",
            verify_ssl=conn.verify_ssl,
            port=conn.port,
            timeout=10,
        )
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


def _test_vsphere(conn: HypervisorConnection, db) -> HypervisorTestResult:
    """Test a vSphere connection via the vCenter Automation REST API."""
    import httpx

    scheme = "https" if conn.verify_ssl else "https"
    base = f"{scheme}://{conn.host}"
    try:
        with httpx.Client(verify=conn.verify_ssl, timeout=10) as client:
            # Create a session
            r = client.post(
                f"{base}/api/session",
                auth=(conn.username, conn.password_encrypted or ""),
            )
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


def _test_hyperv(conn: HypervisorConnection, db) -> HypervisorTestResult:
    """Test a Hyper-V connection via WinRM."""
    try:
        import winrm  # type: ignore[import]

        scheme = "https" if conn.verify_ssl else "http"
        port = conn.port or 5985
        session = winrm.Session(
            f"{scheme}://{conn.host}:{port}/wsman",
            auth=(conn.username, conn.password_encrypted or ""),
            transport="ntlm",
            server_cert_validation="ignore",
        )
        result = session.run_ps("(Get-VMHost).Name; (Get-VM).Count")
        if result.status_code != 0:
            raise RuntimeError(result.std_err.decode(errors="replace")[:300])

        lines = result.std_out.decode(errors="replace").strip().splitlines()
        host_name = lines[0].strip() if lines else conn.host
        vm_count = int(lines[1].strip()) if len(lines) > 1 and lines[1].strip().isdigit() else 0

        conn.is_active = True
        conn.last_seen_at = datetime.utcnow()
        db.commit()
        return HypervisorTestResult(
            success=True,
            message=f"Connected to Hyper-V host {host_name!r}",
            version="Hyper-V",
            nodes_found=1,
        )
    except ImportError:
        return HypervisorTestResult(
            success=False,
            message="pywinrm is not installed on the API server",
            version="n/a",
            nodes_found=0,
        )
    except Exception as exc:
        conn.is_active = False
        db.commit()
        return HypervisorTestResult(success=False, message=f"Connection failed: {exc}", version="n/a", nodes_found=0)


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


@router.post("/connections/{conn_id}/discover")
def discover_nodes(conn_id: uuid.UUID, db: Session = Depends(get_db)):
    conn = db.get(HypervisorConnection, str(conn_id))
    if not conn:
        raise HTTPException(404, "Connection not found")
    if conn.hypervisor_type == "proxmox":
        return _discover_proxmox(conn_id, conn, db)
    if conn.hypervisor_type == "vsphere":
        return _discover_vsphere(conn_id, conn, db)
    if conn.hypervisor_type == "hyperv":
        return _discover_hyperv(conn_id, conn, db)
    return {"message": f"{conn.hypervisor_type} discovery not yet supported", "nodes_discovered": 0}


def _discover_proxmox(conn_id, conn: HypervisorConnection, db) -> dict:
    try:
        from proxmoxer import ProxmoxAPI

        prox = ProxmoxAPI(
            conn.host,
            user=conn.username,
            password=conn.password_encrypted or "",
            verify_ssl=conn.verify_ssl,
            port=conn.port,
            timeout=15,
        )
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
            n.get("uptime", 0)
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


def _discover_vsphere(conn_id, conn: HypervisorConnection, db) -> dict:
    """Discover vSphere hosts and cluster via the vCenter REST API."""
    import httpx

    base = f"https://{conn.host}"
    try:
        with httpx.Client(verify=conn.verify_ssl, timeout=15) as client:
            r = client.post(f"{base}/api/session", auth=(conn.username, conn.password_encrypted or ""))
            r.raise_for_status()
            token = r.json()
            headers = {"vmware-api-session-id": token}

            hosts_resp = client.get(f"{base}/api/vcenter/host", headers=headers)
            hosts_resp.raise_for_status()
            hosts = hosts_resp.json()

            discovered = 0
            for h in hosts:
                node_name = h.get("name", h.get("host", "unknown"))
                existing = db.query(HypervisorNode).filter_by(node_name=node_name).first()
                status = "online" if h.get("connection_state") == "CONNECTED" else "offline"
                if existing:
                    existing.status = status
                else:
                    db.add(
                        HypervisorNode(
                            connection_id=str(conn_id),
                            node_name=node_name,
                            ip_address=conn.host,
                            status=status,
                            cpu_total=0,
                            cpu_used=0.0,
                            memory_total_gb=0.0,
                            memory_used_gb=0.0,
                            storage_total_gb=0.0,
                            storage_used_gb=0.0,
                            vm_count=0,
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


def _discover_hyperv(conn_id, conn: HypervisorConnection, db) -> dict:
    """Discover Hyper-V via WinRM — single-node implementation."""
    try:
        import winrm  # type: ignore[import]

        scheme = "https" if conn.verify_ssl else "http"
        port = conn.port or 5985
        session = winrm.Session(
            f"{scheme}://{conn.host}:{port}/wsman",
            auth=(conn.username, conn.password_encrypted or ""),
            transport="ntlm",
            server_cert_validation="ignore",
        )
        result = session.run_ps(
            "(Get-VMHost).Name; "
            "(Get-VM | Measure-Object).Count; "
            "(Get-VMHost).MemoryCapacity / 1GB; "
            "(Get-VMHost).LogicalProcessorCount"
        )
        if result.status_code != 0:
            raise RuntimeError(result.std_err.decode(errors="replace")[:300])

        lines = result.std_out.decode(errors="replace").strip().splitlines()
        node_name = lines[0].strip() if lines else conn.host
        vm_count = int(lines[1].strip()) if len(lines) > 1 and lines[1].strip().isdigit() else 0
        mem_gb = float(lines[2].strip()) if len(lines) > 2 else 0.0
        cpu_count = int(lines[3].strip()) if len(lines) > 3 and lines[3].strip().isdigit() else 0

        existing = db.query(HypervisorNode).filter_by(node_name=node_name).first()
        discovered = 0
        if existing:
            existing.status = "online"
            existing.memory_total_gb = round(mem_gb, 1)
            existing.cpu_total = cpu_count
            existing.vm_count = vm_count
        else:
            db.add(
                HypervisorNode(
                    connection_id=str(conn_id),
                    node_name=node_name,
                    ip_address=conn.host,
                    status="online",
                    cpu_total=cpu_count,
                    cpu_used=0.0,
                    memory_total_gb=round(mem_gb, 1),
                    memory_used_gb=0.0,
                    storage_total_gb=0.0,
                    storage_used_gb=0.0,
                    vm_count=vm_count,
                )
            )
            discovered += 1

        conn.is_active = True
        conn.last_seen_at = datetime.utcnow()
        db.commit()
        return {"message": f"Discovered Hyper-V host {node_name!r}", "nodes_discovered": discovered}
    except ImportError:
        return {"message": "pywinrm is not installed on the API server", "nodes_discovered": 0}
    except Exception as exc:
        return {"message": f"Hyper-V discovery failed: {exc}", "nodes_discovered": 0}


@router.get("/connections/{conn_id}/nodes", response_model=list[HypervisorNodeOut])
def list_nodes(conn_id: uuid.UUID, db: Session = Depends(get_db)):
    return db.query(HypervisorNode).filter(HypervisorNode.connection_id == str(conn_id)).all()


@router.get("/connections/{conn_id}/pools", response_model=list[HypervisorPoolOut])
def list_pools(conn_id: uuid.UUID, db: Session = Depends(get_db)):
    return db.query(HypervisorPool).filter(HypervisorPool.connection_id == str(conn_id)).all()


# -- Set Primary ---------------------------------------------------------
@router.post("/connections/{conn_id}/set-primary")
def set_primary(conn_id: uuid.UUID, db: Session = Depends(get_db)):
    conn = db.get(HypervisorConnection, str(conn_id))
    if not conn:
        raise HTTPException(404, "Connection not found")
    others = (
        db.query(HypervisorConnection)
        .filter(
            HypervisorConnection.hypervisor_type == conn.hypervisor_type,
            HypervisorConnection.id != str(conn_id),
        )
        .all()
    )
    for other in others:
        other.is_primary = False
    conn.is_primary = True
    db.commit()
    return {"message": f"{conn.name} set as primary {conn.hypervisor_type} connection"}


# -- Summary -------------------------------------------------------------
@router.get("/summary", response_model=HypervisorSummaryOut)
def hypervisor_summary(db: Session = Depends(get_db)):
    conns = db.query(HypervisorConnection).all()
    all_nodes = db.query(HypervisorNode).all()
    active = [c for c in conns if c.is_active]

    # ── DEDUPLICATE NODES BY NAME ──
    # In a cluster, the same physical node may be recorded under
    # different connections.  Keep only one row per node_name.
    seen: dict[str, HypervisorNode] = {}
    for n in all_nodes:
        seen[n.node_name] = n  # last-write wins (all rows are updated identically)
    nodes = list(seen.values())

    online = [n for n in nodes if n.status == "online"]
    by_type: dict[str, int] = {}
    for c in conns:
        by_type[c.hypervisor_type] = by_type.get(c.hypervisor_type, 0) + 1
    return HypervisorSummaryOut(
        total_connections=len(conns),
        active_connections=len(active),
        total_nodes=len(nodes),
        online_nodes=len(online),
        total_vms=sum(n.vm_count for n in nodes),
        total_cpu=sum(n.cpu_total or 0 for n in nodes),
        total_memory_gb=sum(n.memory_total_gb or 0 for n in nodes),
        total_storage_gb=sum(n.storage_total_gb or 0 for n in nodes),
        by_type=by_type,
    )
