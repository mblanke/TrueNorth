"""Discover one connection under its lease; one scheduled round over every active one.

``discover_connection`` is the only way discovery runs: ``POST
/hypervisors/connections/{id}/discover`` and the scheduled round (``runner``) both call it,
and it calls the connection's backend (``app/hypervisor_backends``, ADR 0001), so what
counts as a VM (powered-on range VMs only: no templates, no vCLS agents, no vCenter
appliance) is the backend's rule in both cases.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..hypervisor_backends import get_hypervisor_backend
from ..models import HypervisorConnection
from .models import HypervisorDiscoveryState as State

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 300.0
#: How long a lease is good for if its holder never releases it (the process died). Above
#: any real discovery: the vSphere backend's calls time out after 15 s each.
LEASE_SECONDS = 900
#: A round takes a connection whose last attempt is at least this share of the interval
#: old. Several API processes each run the loop out of phase; this keeps them from
#: rediscovering one connection once per process per interval.
DUE_FRACTION = 0.9
_ERROR_MAX = 1000


def interval() -> float:
    """``INFRA_DISCOVERY_SECONDS`` (default 300). 0 or less: no scheduled discovery."""
    try:
        return float(os.getenv("INFRA_DISCOVERY_SECONDS", str(int(DEFAULT_INTERVAL))))
    except ValueError:
        return DEFAULT_INTERVAL


def _now() -> datetime:
    return datetime.now(UTC)


def _holder() -> str:
    return f"{socket.gethostname()[:24]}:{os.getpid()}:{uuid.uuid4().hex[:16]}"


def _ensure_row(db: Session, conn_id: uuid.UUID) -> None:
    if db.get(State, conn_id) is not None:
        return
    db.add(State(connection_id=conn_id))
    try:
        db.commit()
    except IntegrityError:  # another process inserted it first: theirs is as good
        db.rollback()


def claim(db: Session, conn_id: uuid.UUID, holder: str, *, due_before: datetime | None = None) -> bool:
    """Take the connection's discovery lease. False when someone holds it, or, with
    ``due_before``, when its last attempt is newer than that. Committed either way."""
    _ensure_row(db, conn_id)
    now = _now()
    stmt = update(State).where(
        State.connection_id == conn_id,
        or_(State.lease_expires_at.is_(None), State.lease_expires_at < now),
    )
    if due_before is not None:
        stmt = stmt.where(or_(State.last_attempt_at.is_(None), State.last_attempt_at <= due_before))
    stmt = stmt.values(lease_holder=holder, lease_expires_at=now + timedelta(seconds=LEASE_SECONDS), last_attempt_at=now)
    taken = db.execute(stmt.execution_options(synchronize_session=False)).rowcount == 1
    db.commit()
    return taken


def release(db: Session, conn_id: uuid.UUID, holder: str, *, error: str | None) -> None:
    """Give the lease back and record the outcome (only if this holder still has it)."""
    values: dict = {"lease_holder": None, "lease_expires_at": None, "last_error": error}
    if error is None:
        values["last_discovered_at"] = _now()
    db.execute(
        update(State)
        .where(State.connection_id == conn_id, State.lease_holder == holder)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    db.commit()


def _clip(text: str) -> str:
    return text if len(text) <= _ERROR_MAX else text[: _ERROR_MAX - 1] + "…"


def discover_connection(db: Session, conn: HypervisorConnection, *, due_before: datetime | None = None) -> dict | None:
    """Rediscover one connection's hosts through its backend, under its lease.

    Returns the backend's ``{"message", "nodes_discovered"[, "error"]}``, or None when the
    lease is held elsewhere (or, with ``due_before``, the connection is not due). An
    exception from the backend (an unreadable stored password) is recorded and re-raised.
    """
    conn_id, kind = conn.id, conn.hypervisor_type
    holder = _holder()
    if not claim(db, conn_id, holder, due_before=due_before):
        return None
    try:
        backend = get_hypervisor_backend(kind)
        if backend is None:
            result = {
                "message": f"{kind} discovery not yet supported",
                "nodes_discovered": 0,
                "error": f"unsupported hypervisor type {kind!r}",
            }
        else:
            result = backend.discover(conn_id, conn, db)
    except Exception as exc:
        db.rollback()
        release(db, conn_id, holder, error=_clip(f"{type(exc).__name__}: {exc}"))
        raise
    error = result.get("error")
    if error:
        db.rollback()  # nothing half-written by a failed read survives
    release(db, conn_id, holder, error=_clip(str(error)) if error else None)
    return result


def run_round(session_factory) -> dict[str, int]:
    """Rediscover every active connection that is due, each on its own: one that fails or
    raises is recorded and the round goes on. Returns counts by outcome."""
    counts = {"ok": 0, "failed": 0, "skipped": 0}
    every = interval()
    due_before = _now() - timedelta(seconds=every * DUE_FRACTION) if every > 0 else None
    db = session_factory()
    try:
        ids = [cid for (cid,) in db.query(HypervisorConnection.id).filter(HypervisorConnection.is_active.is_(True))]
        for cid in ids:
            try:
                conn = db.get(HypervisorConnection, cid)
                if conn is None or not conn.is_active:
                    counts["skipped"] += 1
                    continue
                result = discover_connection(db, conn, due_before=due_before)
            except Exception:  # noqa: BLE001 — one connection must not stop the others
                logger.exception("Scheduled discovery of connection %s raised", cid)
                db.rollback()
                counts["failed"] += 1
                continue
            if result is None:
                counts["skipped"] += 1
            elif result.get("error"):
                logger.warning("Scheduled discovery of connection %s failed: %s", cid, result["error"])
                counts["failed"] += 1
            else:
                counts["ok"] += 1
    finally:
        db.close()
    return counts


def forget(db: Session, conn_id: uuid.UUID) -> None:
    """Drop a connection's discovery state (it is being deleted; the caller commits)."""
    db.query(State).filter(State.connection_id == conn_id).delete(synchronize_session=False)


def status_rows(db: Session, conn_ids: list[uuid.UUID]) -> dict[uuid.UUID, State]:
    if not conn_ids:
        return {}
    rows = db.query(State).filter(State.connection_id.in_(conn_ids)).all()
    return {r.connection_id: r for r in rows}


def is_running(row: State | None) -> bool:
    if row is None or row.lease_holder is None or row.lease_expires_at is None:
        return False
    expires = row.lease_expires_at
    if expires.tzinfo is None:  # SQLite hands back naive values; they were written in UTC
        expires = expires.replace(tzinfo=UTC)
    return expires > _now()
