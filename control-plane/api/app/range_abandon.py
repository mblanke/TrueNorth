"""Abandoning a range: record it destroyed without touching the hypervisor (#131 review 3).

The worker refuses to call a range destroyed when it cannot prove its VMs are gone: a
range that does not record which vCenter built it, and none of whose VMs exist on the
one it resolves to, fails its destroy (worker/provisioners/vsphere_api.py destroy). If
the VMs really are gone (deleted by hand, or their vCenter decommissioned), nothing on
the platform can finish that destroy. This is the operator's way out.

A platform administrator only (not a tenant's range_ops): it writes off whatever is still
on a hypervisor, so it is a deliberate act with a required reason. Only from ``failed``.
The range keeps its provisioner_output, plus ``abandoned = {by, at, reason, vms,
networks}``, so whoever cleans up by hand knows what to look for; an audit-log entry
records the same. Its network reservations become reclaimable as a destroyed range's do
(network_inventory), so any port group still on a vCenter should be removed by hand.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth import CurrentUser
from .models import AuditLog, Range, RangeState

ABANDONABLE = (RangeState.failed,)


class RangeAbandonIn(BaseModel):
    """Why the range is written off; recorded on the range and in the audit log."""

    reason: str = Field(min_length=10, max_length=500)
    # A range that records VMs may still have them, holding its VLANs and NIC MACs on the
    # wire: abandoning frees those reservations for the next range. Only with this set.
    force_release_resources: bool = False


RECORDED_VMS_REFUSAL = (
    "This range records {n} VM(s) ({names}). Abandoning it frees its VLANs and NIC MAC addresses for other "
    "ranges while those VMs may still exist and use them (two ranges on one VLAN, or two NICs with one MAC). "
    "Destroy the range instead; or, once you have checked on the hypervisor that the VMs are gone (or removed "
    "them by hand), abandon it again with force_release_resources: true."
)


def abandon(db: Session, range_id: uuid.UUID, user: CurrentUser, reason: str,
            force_release_resources: bool = False) -> Range:
    """Mark ``range_id`` destroyed without a hypervisor call. Does not commit. Refused (409)
    while it records VMs, unless ``force_release_resources``."""
    # tenant-safe: platform administrators only (require_platform_admin on the route), who
    # act across tenants; the row is locked so a concurrent destroy or build cannot interleave.
    rng = db.query(Range).filter(Range.id == range_id).with_for_update().first()
    if rng is None:
        raise HTTPException(404, "Range not found")
    if rng.state not in ABANDONABLE:
        raise HTTPException(409, f"Only a failed range can be abandoned; this one is {rng.state.value}. "
                                 "Destroy it, or let its operation finish or fail first.")
    try:
        out = json.loads(rng.provisioner_output) if rng.provisioner_output else {}
    except (TypeError, ValueError):
        out = {}
    out = out if isinstance(out, dict) else {}
    vms = [{"name": v.get("name"), "vm_id": v.get("vm_id")} for v in out.get("vms") or [] if isinstance(v, dict)]
    networks = [n.get("portgroup") for n in out.get("networks") or [] if isinstance(n, dict) and n.get("portgroup")]
    if vms and not force_release_resources:
        names = ", ".join(str(v["name"] or v["vm_id"]) for v in vms[:5]) + (", ..." if len(vms) > 5 else "")
        raise HTTPException(409, RECORDED_VMS_REFUSAL.format(n=len(vms), names=names))
    out["abandoned"] = {"by": str(user.id), "at": datetime.now(UTC).isoformat(), "reason": reason, "vms": vms,
                        "networks": networks, "force_release_resources": force_release_resources}
    rng.provisioner_output = json.dumps(out)
    rng.state = RangeState.destroyed
    db.add(AuditLog(
        user_id=uuid.UUID(user.id), tenant_id=uuid.UUID(user.tenant_id), action="abandon_range",
        resource_type="range", resource_id=str(rng.id),
        detail=(f"marked destroyed without a hypervisor call (range tenant {rng.tenant_id}); reason: {reason}; "
                f"left on the hypervisor, if anywhere: {len(vms)} VM(s) {[v['vm_id'] for v in vms]}, "
                f"port groups {networks}"),
    ))
    return rng
