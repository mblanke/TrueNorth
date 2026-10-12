"""The vCenter a range lives on, as the API resolves it (the browser console).

The API's copy of the worker's rules (worker/connection_binding.py; the API cannot import
the worker): a range records ``provisioner_output["connection"] = {"id", "host"}`` at
build. With a record: exactly that connection (active or not), still the range's
tenant's or shared, still reaching the recorded host and port; else refused. With
``id`` None: the API's VSPHERE_* settings, if they still reach the recorded host.
With no record (built before 2026-10-11): the tenant's active connection (primary, then
oldest, then lowest id), else a shared one, else VSPHERE_*. Only that connection's own
login is used, and a connection with no password is refused, never filled from the
environment.
"""

from __future__ import annotations

import json
import os
import re

from sqlalchemy import case, or_
from sqlalchemy.orm import Session

from .hypervisor_ports import endpoint_key, endpoint_url
from .models import HypervisorConnection, Range
from .secretbox import unseal


class BindingError(Exception):
    """The range's vCenter cannot be reached safely; the message is safe to show."""


# The worker's range annotation (worker/provisioners/vsphere_api.RANGE_TAG; tests/contracts).
_RANGE_TAG_RE = re.compile(r"truenorth-range:\s*([0-9A-Za-z][0-9A-Za-z-]*)")


def owns(range_id: str, recorded_name: str, vm_name: str, annotation: str | None) -> bool:
    """Whether the VM vCenter has under a recorded id is this range's: its annotation names
    the range, or (built before the tag) it has none and still has its recorded
    ``<range8>-<node>`` name. The worker applies the same rule before acting on a VM."""
    m = _RANGE_TAG_RE.search(annotation or "")
    if m:
        return m.group(1) == range_id
    return bool(vm_name) and vm_name == recorded_name and vm_name.startswith(f"{range_id[:8]}-")


def recorded(rng: Range) -> dict | None:
    try:
        out = json.loads(rng.provisioner_output) if rng.provisioner_output else {}
    except (TypeError, ValueError):
        return None
    rec = out.get("connection") if isinstance(out, dict) else None
    return rec if isinstance(rec, dict) and rec.get("host") else None


def _env() -> dict:
    return {"url": os.getenv("VSPHERE_URL", "").rstrip("/"), "username": os.getenv("VSPHERE_USERNAME", ""),
            "password": os.getenv("VSPHERE_PASSWORD", ""),
            "verify_ssl": os.getenv("VSPHERE_VERIFY_SSL", "false").lower() == "true"}


def _from_row(conn: HypervisorConnection) -> dict:
    password = unseal(conn.password_encrypted) or ""
    if not (conn.host and conn.username and password):
        raise BindingError(f"hypervisor connection {conn.name!r} has no host, username or password")
    try:
        url = endpoint_url(conn.host, conn.port)
    except ValueError as exc:
        raise BindingError(f"hypervisor connection {conn.name!r}: {exc}") from exc
    return {"url": url, "username": conn.username, "password": password, "verify_ssl": bool(conn.verify_ssl)}


def _scope(rng: Range):
    own = HypervisorConnection.tenant_id == rng.tenant_id if rng.tenant_id is not None else None
    return own, (or_(own, HypervisorConnection.tenant_id.is_(None)) if own is not None
                 else HypervisorConnection.tenant_id.is_(None))


def resolve(db: Session, rng: Range, hypervisor_type: str) -> dict:
    """{"url", "username", "password", "verify_ssl"} of the vCenter ``rng`` lives on."""
    own, allowed = _scope(rng)
    rec = recorded(rng)
    if rec is not None:
        if rec.get("id"):
            # tenant-safe: ``allowed`` is the range's own tenant's connections, or shared ones.
            conn = (db.query(HypervisorConnection)
                    .filter(HypervisorConnection.id == str(rec["id"]),
                            HypervisorConnection.hypervisor_type == hypervisor_type, allowed)
                    .first())
            if conn is None:
                raise BindingError("the connection this range was built on no longer exists")
            found = _from_row(conn)
        else:
            found = _env()
        if endpoint_key(found["url"]) != endpoint_key(str(rec["host"])):
            raise BindingError(f"this range was built on {rec['host']}, and its connection now points elsewhere")
        return found
    query = db.query(HypervisorConnection).filter(HypervisorConnection.hypervisor_type == hypervisor_type,
                                                  HypervisorConnection.is_active.is_(True), allowed)
    order = [case((own, 0), else_=1)] if own is not None else []
    conn = query.order_by(*order, HypervisorConnection.is_primary.desc(), HypervisorConnection.created_at.asc(),
                          HypervisorConnection.id.asc()).first()
    return _from_row(conn) if conn is not None else _env()
