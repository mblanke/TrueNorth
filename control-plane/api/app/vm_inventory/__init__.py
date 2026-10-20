"""A hypervisor connection's VMs, grouped by TrueNorth's ``tn-type`` vCenter tag.

The adapter reads them (``hypervisor_backends``, ``list_vms``); this groups them for the
Infrastructure page: Golden templates, Host copies, Range VMs, Other. Tags, not names or
folders, decide the group, so a VM someone renamed or moved is still grouped right, and a VM
TrueNorth did not tag is Other, never guessed into a TrueNorth group.
"""

from __future__ import annotations

import logging

from .schemas import InventoryVmOut, VmInventoryOut

logger = logging.getLogger(__name__)

GROUP_OF_TYPE = {"golden-template": "golden", "template-copy": "copies", "range-vm": "ranges"}
GROUPS = ("golden", "copies", "ranges", "other")


def group_of(tags: dict[str, str]) -> str:
    return GROUP_OF_TYPE.get((tags or {}).get("tn-type", ""), "other")


def inventory(backend, conn) -> VmInventoryOut:
    """The connection's VMs, grouped. A failure is reported in ``message``, never raised."""
    kind = getattr(conn, "hypervisor_type", "?")
    if backend is None:
        return VmInventoryOut(supported=False, message=f"Unsupported hypervisor type: {kind}")
    try:
        got = backend.list_vms(conn)
    except Exception as exc:  # noqa: BLE001 -- the hypervisor's failure, reported on the page
        logger.warning("VM inventory of connection %s failed: %s", getattr(conn, "id", "?"), exc)
        return VmInventoryOut(supported=True, message=f"VM inventory failed: {exc}")
    if got is None:
        return VmInventoryOut(supported=False, message=f"Listing VMs is not supported for {kind} connections")
    rows, note = got
    vms = [InventoryVmOut(**{k: r.get(k) for k in ("vm_id", "name", "power_state", "host")},
                          tags=r.get("tags") or {}, group=group_of(r.get("tags") or {})) for r in rows]
    groups = {g: sum(1 for v in vms if v.group == g) for g in GROUPS}
    return VmInventoryOut(supported=True, message=note or f"{len(vms)} VMs", groups=groups, vms=vms)
