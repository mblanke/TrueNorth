"""TrueNorth Range -- Hypervisor Configuration Router.

DB-backed CRUD for hypervisor connections (vSphere, plus legacy Proxmox and Hyper-V),
node discovery, pool management, and connection testing. Talking to a hypervisor is
the job of ``app/hypervisor_backends`` (ADR 0001); this module is HTTP only.

Tenancy: a connection made here is tenant-owned, like the storage appliances beside it.
One with no tenant is shared (the worker builds on it for a tenant without its own);
it is managed outside this API and is never handed to a tenant. A caller sees,
changes and tests only their own tenant's connections and the hosts discovered through
them; a foreign id is 404. Until 2026-10-07 every by-id route used ``db.get`` with no
tenant predicate, so a range_ops user in one tenant could read, repoint, delete or
demote another tenant's vCenter.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..delete_guard import commit_delete
from ..hypervisor_backends import get_hypervisor_backend
from ..hypervisor_connection_guard import refuse_if_it_moves_unrecorded, refuse_if_live, require_password
from ..hypervisor_ports import default_port
from ..infra_discovery import service as discovery
from ..infra_discovery.schemas import ConnectionDiscoveryOut, InfraDiscoveryOut
from ..models import (
    HypervisorConnection,
    HypervisorNode,
    HypervisorPool,
)
from ..rbac import Permission, require_permission, user_has_permission
from ..schemas import (
    HypervisorConnectionIn,
    HypervisorConnectionOut,
    HypervisorConnectionUpdate,
    HypervisorNodeOut,
    HypervisorPoolOut,
    HypervisorSummaryOut,
    HypervisorTestResult,
)
from ..secretbox import seal
from ..tenancy import get_owned, tenant_uuid

# Router-level authentication is read-level, so the host inventory and summary can feed the
# dashboard for anyone who may see platform health (instructors included). Every connection
# route — they expose hosts and usernames, store credentials, or reach out to vCenter — also
# carries WRITE below.
#
# Every route here was previously reachable with no credentials at all.
router = APIRouter(prefix="/hypervisors", tags=["Infrastructure"], dependencies=[Depends(require_permission(Permission.INFRA_READ))])
WRITE = [Depends(require_permission(Permission.INFRA_WRITE))]


def _conn(db: Session, conn_id: uuid.UUID, user: CurrentUser) -> HypervisorConnection:
    return get_owned(db, HypervisorConnection, conn_id, user, not_found="Connection not found")


def _own_connections(db: Session, user: CurrentUser):
    return db.query(HypervisorConnection).filter(HypervisorConnection.tenant_id == tenant_uuid(user))


# -- Connections CRUD ----------------------------------------------------
@router.get("/connections", response_model=list[HypervisorConnectionOut], dependencies=WRITE)
def list_connections(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    return _own_connections(db, user).order_by(HypervisorConnection.created_at.desc()).all()


@router.post("/connections", response_model=HypervisorConnectionOut, status_code=201, dependencies=WRITE)
def create_connection(
    payload: HypervisorConnectionIn, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)
):
    require_password(payload.hypervisor_type, payload.password)  # never the platform's (#131 review)
    same_kind = _own_connections(db, user).filter(HypervisorConnection.hypervisor_type == payload.hypervisor_type)
    if payload.is_primary or not same_kind.filter(HypervisorConnection.is_active.is_(True)).count():
        # It becomes the tenant's preferred connection: ranges that do not record theirs would follow.
        refuse_if_it_moves_unrecorded(db, payload.hypervisor_type, tenant_uuid(user), "add this connection")
    if payload.is_primary:  # one primary per tenant and family, as set-primary leaves it
        same_kind.update({HypervisorConnection.is_primary: False}, synchronize_session=False)
    conn = HypervisorConnection(
        name=payload.name,
        hypervisor_type=payload.hypervisor_type,
        host=payload.host,
        port=payload.port or default_port(payload.hypervisor_type),  # 8006 for every family until 2026-10-11
        username=payload.username,
        password_encrypted=seal(payload.password),  # app/secretbox.py: never stored as typed
        api_token=seal(payload.api_token),
        verify_ssl=payload.verify_ssl,
        is_primary=payload.is_primary,
        datacenter=payload.datacenter,
        notes=payload.notes,
        tenant_id=tenant_uuid(user),
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


@router.get("/connections/{conn_id}", response_model=HypervisorConnectionOut, dependencies=WRITE)
def get_connection(conn_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    return _conn(db, conn_id, user)


@router.patch("/connections/{conn_id}", response_model=HypervisorConnectionOut, dependencies=WRITE)
def update_connection(
    conn_id: uuid.UUID,
    payload: HypervisorConnectionUpdate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    conn = _conn(db, conn_id, user)
    changes = payload.model_dump(exclude_unset=True)
    if "password" in changes:
        require_password(conn.hypervisor_type, changes["password"])  # rotate it, never clear it
    if changes.get("is_active") is False and conn.is_active:
        refuse_if_live(db, conn, "deactivate")
    if (changes.get("is_active") is True and not conn.is_active) or (
            changes.get("is_primary") is not None and changes["is_primary"] != conn.is_primary):
        refuse_if_it_moves_unrecorded(db, conn.hypervisor_type, conn.tenant_id, "change which connection is preferred")
    if changes.get("port") is None:
        changes.pop("port", None)  # explicit null: keep the stored port
    if ("host" in changes and changes["host"] != conn.host) or ("port" in changes and changes["port"] != conn.port):
        refuse_if_live(db, conn, "point to another host")
    for field, value in changes.items():
        if field == "password":
            conn.password_encrypted = seal(value)
        elif field == "api_token":
            conn.api_token = seal(value)
        else:
            setattr(conn, field, value)
    db.commit()
    db.refresh(conn)
    return conn


@router.delete("/connections/{conn_id}", status_code=204, response_class=Response, dependencies=WRITE)
def delete_connection(conn_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """Delete a connection with the hosts and pools discovered through it.

    Those rows are inventory read from the hypervisor, with no meaning once the
    connection is gone, so they go with it. Refused (409) while ranges not yet destroyed
    are on it (app/hypervisor_connection_guard.py)."""
    conn = _conn(db, conn_id, user)
    refuse_if_live(db, conn, "delete")
    # tenant-safe: conn came from _conn(), so what hangs off it is the caller's.
    db.query(HypervisorPool).filter(HypervisorPool.connection_id == conn.id).delete(synchronize_session=False)
    db.query(HypervisorNode).filter(HypervisorNode.connection_id == conn.id).delete(synchronize_session=False)
    discovery.forget(db, conn.id)
    db.flush()
    db.expire(conn, ["nodes"])
    db.delete(conn)
    commit_delete(db, "Hypervisor connection")


# -- Connection Testing --------------------------------------------------
@router.post("/connections/{conn_id}/test", response_model=HypervisorTestResult, dependencies=WRITE)
def test_connection(conn_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    conn = _conn(db, conn_id, user)
    backend = get_hypervisor_backend(conn.hypervisor_type)
    if backend:
        return backend.check_connection(conn, db)
    return HypervisorTestResult(
        success=False,
        message=f"Unsupported hypervisor type: {conn.hypervisor_type}",
        version="n/a",
        nodes_found=0,
    )


@router.post(
    "/connections/{conn_id}/discover",
    dependencies=WRITE,
    responses={409: {"description": "A discovery of this connection is already running"}},
)
def discover_nodes(conn_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """Rediscover the connection's hosts now. The scheduled rediscovery
    (INFRA_DISCOVERY_SECONDS, app/infra_discovery) runs this same call; one at a time."""
    conn = _conn(db, conn_id, user)
    result = discovery.discover_connection(db, conn)
    if result is None:
        raise HTTPException(409, "A discovery of this connection is already running; its result will show shortly")
    return result


@router.get("/discovery", response_model=InfraDiscoveryOut)
def discovery_status(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """When each of the caller's tenant's connections was last discovered, and whether one is
    running. Read-level, like the inventory it dates; the error text (which can name the
    vCenter) only for callers who may manage connections."""
    conns = _own_connections(db, user).with_entities(HypervisorConnection.id).all()
    ids = [cid for (cid,) in conns]
    rows = discovery.status_rows(db, ids)
    show_error = user_has_permission(user, Permission.INFRA_WRITE)
    every = discovery.interval()
    out = []
    for cid in ids:
        row = rows.get(cid)
        out.append(
            ConnectionDiscoveryOut(
                connection_id=cid,
                last_discovered_at=row.last_discovered_at if row else None,
                last_attempt_at=row.last_attempt_at if row else None,
                failing=bool(row and row.last_error),
                last_error=row.last_error if row and show_error else None,
                running=discovery.is_running(row),
            )
        )
    return InfraDiscoveryOut(interval_seconds=max(0, int(every)), connections=out)


@router.get("/connections/{conn_id}/nodes", response_model=list[HypervisorNodeOut], dependencies=WRITE)
def list_nodes(conn_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    _conn(db, conn_id, user)
    return db.query(HypervisorNode).filter(HypervisorNode.connection_id == str(conn_id)).all()


@router.get("/connections/{conn_id}/pools", response_model=list[HypervisorPoolOut], dependencies=WRITE)
def list_pools(conn_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    _conn(db, conn_id, user)
    return db.query(HypervisorPool).filter(HypervisorPool.connection_id == str(conn_id)).all()


# -- Set Primary ---------------------------------------------------------
@router.post("/connections/{conn_id}/set-primary", dependencies=WRITE)
def set_primary(conn_id: uuid.UUID, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    conn = _conn(db, conn_id, user)
    # Primary is per tenant: demoting another tenant's connection is not ours to do.
    others = (
        _own_connections(db, user)
        .filter(
            HypervisorConnection.hypervisor_type == conn.hypervisor_type,
            HypervisorConnection.id != str(conn_id),
        )
        .all()
    )
    if not conn.is_primary or any(o.is_primary for o in others):  # a no-op only if it is the sole primary
        refuse_if_it_moves_unrecorded(db, conn.hypervisor_type, conn.tenant_id, "change the primary connection")
    for other in others:
        other.is_primary = False
    conn.is_primary = True
    db.commit()
    return {"message": f"{conn.name} set as primary {conn.hypervisor_type} connection"}


def _unique_nodes(db: Session, user: CurrentUser) -> list[HypervisorNode]:
    # In a Proxmox cluster the same physical node may be recorded under
    # different connections.  Keep only one row per node_name.
    seen: dict[str, HypervisorNode] = {}
    mine = _own_connections(db, user).with_entities(HypervisorConnection.id)
    for n in db.query(HypervisorNode).filter(HypervisorNode.connection_id.in_(mine)).order_by(HypervisorNode.node_name):
        seen[n.node_name] = n  # last-write wins (all rows are updated identically)
    return list(seen.values())


# -- Inventory -----------------------------------------------------------
@router.get("/nodes", response_model=list[HypervisorNodeOut])
def list_all_nodes(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    """Every discovered host across the caller's tenant's connections, as last discovered.
    Read-only; the dashboard's cluster panel. No hypervisor is contacted — run discovery to
    refresh."""
    return _unique_nodes(db, user)


# -- Summary -------------------------------------------------------------
@router.get("/summary", response_model=HypervisorSummaryOut)
def hypervisor_summary(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    conns = _own_connections(db, user).all()
    active = [c for c in conns if c.is_active]
    nodes = _unique_nodes(db, user)

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
