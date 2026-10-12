"""A hypervisor connection that live ranges are on: what may and may not change.

The worker records, in a range's ``provisioner_output["connection"]``, the connection a
range was built on (``{"id", "host"}``: worker/connection_binding.py) and reaches only
that one for destroy, power and snapshots; a connection that is gone or names another
host is refused there. Until 2026-10-11 (review of #131) nothing was recorded, and the
API let a tenant deactivate or delete the connection its live ranges were on, so the
next operation re-resolved to another vCenter and acted on bare VM ids there.

So, for a connection with live ranges on it (any state but ``destroyed``):

* DELETE, deactivating it, or pointing it at another host: 409, naming the ranges;
* changing its login (password rotation) or anything else: allowed.

"On it": a range of one of the family's provisioner backends that records this
connection; or, built before the record existed (VMs or networks, nothing recorded) or
still being built, a range that resolves its connection afresh each time and so may be
on this one: its tenant's, or any tenant's for a shared connection. Conservative on
purpose: a refusal costs an operator a destroy first, a wrong vCenter costs VMs.

Creating or updating one requires a password where the family needs one
(``requires_password``): the worker never falls back to the platform's credentials.
"""

from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .hypervisor_backends import get_hypervisor_backend
from .models import HypervisorConnection, Range, RangeState


def _output(raw) -> dict:
    try:
        out = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _live(db: Session, hypervisor_type: str, tenant_id) -> list[tuple[Range, dict | None]]:
    """(range, its recorded connection or None) of every range not yet destroyed that a
    connection of this family and tenant (None: shared, any tenant) may serve, and that
    has something on a hypervisor (VMs, networks) or is being built."""
    backend = get_hypervisor_backend(hypervisor_type)
    backends = getattr(backend, "provisioner_backends", frozenset())
    if not backends:
        return []
    query = db.query(Range).filter(Range.state != RangeState.destroyed, Range.provisioner_backend.in_(backends))
    if tenant_id is not None:  # a tenant's connection serves only that tenant's ranges
        query = query.filter(Range.tenant_id == tenant_id)
    found = []
    for rng in query.order_by(Range.created_at):
        out = _output(rng.provisioner_output)
        rec = out.get("connection")
        if isinstance(rec, dict) and rec.get("host"):
            found.append((rng, rec))
        elif rng.state == RangeState.failed and out.get("unrecorded_vms_missing"):
            # Its destroy found none of its VMs on the vCenter it resolved to (the worker's
            # mark): changing or repointing connections cannot strand it further, and is
            # the fix its error recommends. Or an operator abandons it (POST /ranges/{id}/abandon).
            continue
        elif out.get("vms") or out.get("networks") or rng.state == RangeState.provisioning:
            found.append((rng, None))
    return found


def live_ranges_on(db: Session, conn: HypervisorConnection) -> list[Range]:
    """Ranges not yet destroyed that are, or may be, on ``conn`` (see the module doc)."""
    return [rng for rng, rec in _live(db, conn.hypervisor_type, conn.tenant_id)
            if rec is None or str(rec.get("id") or "") == str(conn.id)]


def _refuse(live: list[Range], what: str) -> None:
    if not live:
        return
    names = ", ".join(f"{r.name} ({r.id})" for r in live[:5]) + (f" and {len(live) - 5} more" if len(live) > 5 else "")
    raise HTTPException(409, f"{what}: {len(live)} range(s) not yet destroyed ({names}). Their destroy, power "
                             "and snapshots must reach the vCenter that built them; destroy them first.")


def refuse_if_live(db: Session, conn: HypervisorConnection, action: str) -> None:
    """409 when live ranges are on ``conn``; ``action`` says what was refused."""
    _refuse(live_ranges_on(db, conn), f"Cannot {action} hypervisor connection {conn.name!r}, ranges are on it")


def refuse_if_it_moves_unrecorded(db: Session, hypervisor_type: str, tenant_id, action: str) -> None:
    """409 when making a connection preferred (primary, newly active, the tenant's first)
    would change which vCenter an unrecorded range resolves to. A range built before the
    worker recorded its connection follows the tenant's preferred connection on every
    operation until one of them records it (worker/connection_binding.record_confirmed)."""
    unrecorded = [rng for rng, rec in _live(db, hypervisor_type, tenant_id) if rec is None]
    _refuse(unrecorded, f"Cannot {action}: it would move ranges that do not yet record their vCenter")


def require_password(hypervisor_type: str, password: str | None) -> None:
    """422 when the family needs a password and none is given."""
    backend = get_hypervisor_backend(hypervisor_type)
    if getattr(backend, "requires_password", False) and not (password or "").strip():
        raise HTTPException(
            422,
            f"A {hypervisor_type} connection needs a password: the worker logs in with the connection's own "
            "credentials only, never the platform's.",
        )
