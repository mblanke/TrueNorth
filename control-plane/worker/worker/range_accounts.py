"""The accounts a range declares, handed to its provisioner for one build (ADR 0012 §12).

The API stores them sealed in ``range_credentials`` when a build is accepted (generated
passwords are made there, once per range). ``provision`` wraps a build:

* ``attach`` reads and unseals them and returns a copy of the rendered template whose VMs
  carry, in memory only, ``_accounts`` (``RangeAccount``; passwords in ``Secret``, which
  neither prints nor pickles), matched on the VM's template node id, and
  ``_default_login`` (``TN_RANGE_DEFAULT_PASSWORD`` as a ``Secret``, when set: what a
  Windows VM with no declared Administrator gets instead of a random password; Linux VMs
  keep their template's build password, ADR 0012 §5);
* a lab session's range gets neither: its VMs keep a random password per VM, as before
  (lab credentials are not part of ADR 0012's first slice);
* afterwards, ``record_applied`` marks the accounts the build verified exist
  (``accounts_applied`` on each built VM: usernames only) ``applied``; the API shows
  Students nothing else. A provisioner that reports nothing (mock, Proxmox, Hyper-V)
  leaves every account ``applied = false``.

The caller's ``template`` is not changed, so nothing after the build (the stored output,
Greyspace, logs) ever holds a password. The provisioner drops the keys when it returns.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextlib import AbstractContextManager

from . import db_ops
from .provisioners.vsphere_guest import RangeAccount, Secret

logger = logging.getLogger(__name__)

DEFAULT_PASSWORD_ENV = "TN_RANGE_DEFAULT_PASSWORD"
Session = Callable[[], AbstractContextManager]


def default_password() -> Secret | None:
    """The platform's generic range password (installer: vault ``vault_range_default_password``)."""
    value = os.getenv(DEFAULT_PASSWORD_ENV, "").strip()
    return Secret(value) if value else None


def load(session: Session, range_id: str) -> tuple[dict[str, list[RangeAccount]], bool]:
    """(machine (node id) -> its declared accounts, whether the range is a lab session's)."""
    with session() as db:
        lab = db_ops.is_lab_range(db, range_id)
        rows = [] if lab else db_ops.range_accounts(db, range_id)
    out: dict[str, list[RangeAccount]] = {}
    for machine, username, password, role in rows:
        if password:
            out.setdefault(str(machine), []).append(RangeAccount(username, Secret(password), role == "admin"))
    return out, lab


def _node(vm: dict, range_id: str) -> str:
    return str(vm.get("node_id") or str(vm.get("name") or "").removeprefix(f"{range_id[:8]}-"))


def attach(template: dict, session: Session, range_id: str) -> dict:
    """A copy of ``template`` whose ``vms`` carry their accounts and the default login."""
    vms = template.get("vms")
    if not isinstance(vms, list) or not vms:
        return template
    declared, lab = load(session, range_id)
    if lab:
        return template  # random per VM, as before
    default = default_password()
    copies = []
    matched: set[str] = set()
    for vm in vms:
        vm = dict(vm)
        node = _node(vm, range_id)
        if node in declared:
            vm["_accounts"] = list(declared[node])
            matched.add(node)
        if default is not None:
            vm["_default_login"] = default
        copies.append(vm)
    if unmatched := sorted(set(declared) - matched):
        logger.warning("range %s declares accounts on machines it does not build: %s", range_id, ", ".join(unmatched))
    return {**template, "vms": copies}


def record_applied(session: Session, range_id: str, built_vms: list[dict]) -> None:
    """Mark ``applied`` the accounts every built VM of their machine reports it set. Never
    raises: a failed write leaves them not applied (Students then see none), not a failed build."""
    per_node: dict[str, set[str]] = {}
    for vm in built_vms or []:
        if not isinstance(vm, dict) or not isinstance(vm.get("accounts_applied"), list):
            continue
        names = {str(n) for n in vm["accounts_applied"]}
        node = _node(vm, range_id)
        per_node[node] = per_node[node] & names if node in per_node else names
    pairs = {(node, name) for node, names in per_node.items() for name in names}
    if not pairs:
        return
    try:
        with session() as db:
            db_ops.mark_accounts_applied(db, range_id, pairs)
    except Exception:  # noqa: BLE001
        logger.warning("range %s: could not record which accounts were set", range_id)


async def provision(provisioner, range_id: str, template: dict, allocations: dict, session: Session):
    """``provisioner.provision`` with the range's accounts attached, then recorded."""
    result = await provisioner.provision(range_id, attach(template, session, range_id), allocations)
    record_applied(session, range_id, getattr(result, "vms", None) or [])
    return result
