"""TrueNorth Range -- Hypervisor Configuration Router.

DB-backed CRUD for hypervisor connections (vSphere, plus legacy Proxmox and Hyper-V),
node discovery, pool management, and connection testing. Talking to a hypervisor is
the job of ``app/hypervisor_backends`` (ADR 0001); this module is HTTP only.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..hypervisor_backends import get_hypervisor_backend
from ..models import (
    HypervisorConnection,
    HypervisorNode,
    HypervisorPool,
)
from ..rbac import Permission, require_permission
from ..schemas import (
    HypervisorConnectionIn,
    HypervisorConnectionOut,
    HypervisorConnectionUpdate,
    HypervisorNodeOut,
    HypervisorPoolOut,
    HypervisorSummaryOut,
    HypervisorTestResult,
)

# Router-level authentication is read-level, so the host inventory and summary can feed the
# dashboard for anyone who may see platform health (instructors included). Every connection
# route — they expose hosts and usernames, store credentials, or reach out to vCenter — also
# carries WRITE below.
#
# Every route here was previously reachable with no credentials at all.
router = APIRouter(prefix="/hypervisors", tags=["Infrastructure"], dependencies=[Depends(require_permission(Permission.INFRA_READ))])
WRITE = [Depends(require_permission(Permission.INFRA_WRITE))]


# -- Connections CRUD ----------------------------------------------------
@router.get("/connections", response_model=list[HypervisorConnectionOut], dependencies=WRITE)
def list_connections(db: Session = Depends(get_db)):
    return db.query(HypervisorConnection).order_by(HypervisorConnection.created_at.desc()).all()


@router.post("/connections", response_model=HypervisorConnectionOut, status_code=201, dependencies=WRITE)
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


@router.get("/connections/{conn_id}", response_model=HypervisorConnectionOut, dependencies=WRITE)
def get_connection(conn_id: uuid.UUID, db: Session = Depends(get_db)):
    conn = db.get(HypervisorConnection, str(conn_id))
    if not conn:
        raise HTTPException(404, "Connection not found")
    return conn


@router.patch("/connections/{conn_id}", response_model=HypervisorConnectionOut, dependencies=WRITE)
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


@router.delete("/connections/{conn_id}", status_code=204, response_class=Response, dependencies=WRITE)
def delete_connection(conn_id: uuid.UUID, db: Session = Depends(get_db)):
    conn = db.get(HypervisorConnection, str(conn_id))
    if not conn:
        raise HTTPException(404, "Connection not found")
    db.delete(conn)
    db.commit()


# -- Connection Testing --------------------------------------------------
@router.post("/connections/{conn_id}/test", response_model=HypervisorTestResult, dependencies=WRITE)
def test_connection(conn_id: uuid.UUID, db: Session = Depends(get_db)):
    conn = db.get(HypervisorConnection, str(conn_id))
    if not conn:
        raise HTTPException(404, "Connection not found")
    backend = get_hypervisor_backend(conn.hypervisor_type)
    if backend:
        return backend.check_connection(conn, db)
    return HypervisorTestResult(
        success=False,
        message=f"Unsupported hypervisor type: {conn.hypervisor_type}",
        version="n/a",
        nodes_found=0,
    )


@router.post("/connections/{conn_id}/discover", dependencies=WRITE)
def discover_nodes(conn_id: uuid.UUID, db: Session = Depends(get_db)):
    conn = db.get(HypervisorConnection, str(conn_id))
    if not conn:
        raise HTTPException(404, "Connection not found")
    backend = get_hypervisor_backend(conn.hypervisor_type)
    if backend:
        return backend.discover(conn_id, conn, db)
    return {"message": f"{conn.hypervisor_type} discovery not yet supported", "nodes_discovered": 0}


@router.get("/connections/{conn_id}/nodes", response_model=list[HypervisorNodeOut], dependencies=WRITE)
def list_nodes(conn_id: uuid.UUID, db: Session = Depends(get_db)):
    return db.query(HypervisorNode).filter(HypervisorNode.connection_id == str(conn_id)).all()


@router.get("/connections/{conn_id}/pools", response_model=list[HypervisorPoolOut], dependencies=WRITE)
def list_pools(conn_id: uuid.UUID, db: Session = Depends(get_db)):
    return db.query(HypervisorPool).filter(HypervisorPool.connection_id == str(conn_id)).all()


# -- Set Primary ---------------------------------------------------------
@router.post("/connections/{conn_id}/set-primary", dependencies=WRITE)
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


def _unique_nodes(db: Session) -> list[HypervisorNode]:
    # In a Proxmox cluster the same physical node may be recorded under
    # different connections.  Keep only one row per node_name.
    seen: dict[str, HypervisorNode] = {}
    for n in db.query(HypervisorNode).order_by(HypervisorNode.node_name).all():
        seen[n.node_name] = n  # last-write wins (all rows are updated identically)
    return list(seen.values())


# -- Inventory -----------------------------------------------------------
@router.get("/nodes", response_model=list[HypervisorNodeOut])
def list_all_nodes(db: Session = Depends(get_db)):
    """Every discovered host across all connections, as last discovered. Read-only; the
    dashboard's cluster panel. No hypervisor is contacted — run discovery to refresh."""
    return _unique_nodes(db)


# -- Summary -------------------------------------------------------------
@router.get("/summary", response_model=HypervisorSummaryOut)
def hypervisor_summary(db: Session = Depends(get_db)):
    conns = db.query(HypervisorConnection).all()
    active = [c for c in conns if c.is_active]
    nodes = _unique_nodes(db)

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
