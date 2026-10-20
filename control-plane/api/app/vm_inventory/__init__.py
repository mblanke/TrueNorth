"""A hypervisor connection's VMs, grouped by TrueNorth's ``tn-type`` vCenter tag.

The adapter reads them (``hypervisor_backends``, ``list_vms``); this groups them for the
Infrastructure page: Golden templates, Host copies, Range VMs, Other. Tags, not names or
folders, decide the group, so a VM someone renamed or moved is still grouped right, and a VM
TrueNorth did not tag is Other, never guessed into a TrueNorth group.

Tenancy: a range VM is shown only to its own tenant (its ``tn-range`` tag names the range by
the first 12 hex digits of its id, ``range_key``) or to the platform administrator. A VM
tagged as a range VM whose range is not the caller's, or with no range at all, is left out.
"""

from __future__ import annotations

import logging
import re

from .schemas import InventoryVmOut, VmInventoryOut

logger = logging.getLogger(__name__)

GROUP_OF_TYPE = {"golden-template": "golden", "template-copy": "copies", "range-vm": "ranges"}
GROUPS = ("golden", "copies", "ranges", "other")
MAX_LIMIT = 2000
KEY_LEN = 12  # worker provisioners/vsphere_tags.KEY_LEN


def group_of(tags: dict[str, str]) -> str:
    return GROUP_OF_TYPE.get((tags or {}).get("tn-type", ""), "other")


def range_key(range_id) -> str:
    """The first 12 hex digits of a range id (worker vsphere_tags.range_key)."""
    return re.sub(r"[^0-9a-f]", "", str(range_id).lower())[:KEY_LEN]


def tag_key(label: str) -> str | None:
    """The range key a tn-range tag is named for (``<name>-<key>`` or ``<key>``)."""
    m = re.search(rf"(?:^|-)([0-9a-f]{{{KEY_LEN}}})$", str(label or "").lower())
    return m.group(1) if m else None


def own_range_keys(db, user) -> set[str] | None:
    """The range keys of every range of the caller's tenant (any state: a VM a destroy left
    behind is still theirs to see), or None for the platform administrator (sees all)."""
    from ..models import Range
    from ..rbac import is_platform_admin
    from ..tenancy import tenant_uuid

    if is_platform_admin(user, db):
        return None
    return {range_key(rid) for (rid,) in db.query(Range.id).filter(Range.tenant_id == tenant_uuid(user)).all()}


def visible(tags: dict[str, str], own_keys: set[str] | None) -> bool:
    """Whether a VM may be shown: ``own_keys`` None is the platform administrator (all)."""
    if own_keys is None:
        return True
    label = (tags or {}).get("tn-range")
    if label is None:
        return (tags or {}).get("tn-type") != "range-vm"  # a range VM must say whose it is
    return tag_key(label) in own_keys


def inventory(backend, conn, *, own_keys: set[str] | None = None, group: str | None = None,
              limit: int = 500, offset: int = 0) -> VmInventoryOut:
    """The connection's VMs the caller may see, grouped; ``group`` filters, ``limit``/``offset``
    page (``total`` is the filtered count). A failure is reported in ``message``, never raised."""
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
                          tags=r.get("tags") or {}, group=group_of(r.get("tags") or {}))
           for r in rows if visible(r.get("tags") or {}, own_keys)]
    groups = {g: sum(1 for v in vms if v.group == g) for g in GROUPS}
    if group:
        vms = [v for v in vms if v.group == group]
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    page = vms[offset:offset + limit]
    message = note or f"{len(vms)} VMs"
    if len(page) < len(vms):
        message += f" (showing {offset + 1 if page else 0}-{offset + len(page)} of {len(vms)})"
    return VmInventoryOut(supported=True, message=message, groups=groups, total=len(vms), limit=limit,
                          offset=offset, vms=page)
