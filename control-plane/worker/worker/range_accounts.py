"""The accounts a range's machines get, handed to its provisioner for one build (ADR 0012 §12,
ADR 0013 §3).

The API stores them sealed in ``range_credentials`` when a build is accepted (or a lab
session builds its range): the accounts the template declares and the implicit staff
logins (the built-in Administrator, the Linux build user, ``soadmin``), each with a
password generated for this range. ``provision`` wraps a build:

* ``attach`` reads and unseals them and returns a copy of the rendered template whose VMs
  carry, in memory only, ``_accounts`` (``RangeAccount``; passwords in ``Secret``, which
  neither prints nor pickles), matched on the VM's template node id. A lab session's range
  is no different. There is no default login: a machine with no account keeps a random
  Administrator password (Windows) and whatever its template has (it then fails the
  template-login check, provisioners/vsphere_credcheck.py);
* afterwards, ``record_applied`` marks the accounts the build verified exist
  (``accounts_applied`` on each built VM: usernames only) ``applied``; the API shows
  Students nothing else. A provisioner that reports nothing (mock, Proxmox, Hyper-V)
  leaves every account ``applied = false``.

The caller's ``template`` is not changed, so nothing after the build (the stored output,
Greyspace, logs) ever holds a password. The provisioner drops the keys when it returns.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager

from . import db_ops
from .provisioners.vsphere_guest import RangeAccount, Secret

logger = logging.getLogger(__name__)

Session = Callable[[], AbstractContextManager]


def load(session: Session, range_id: str) -> dict[str, list[RangeAccount]]:
    """Machine (node id) -> its accounts."""
    with session() as db:
        rows = db_ops.range_accounts(db, range_id)
    out: dict[str, list[RangeAccount]] = {}
    for machine, username, password, role in rows:
        if password:
            out.setdefault(str(machine), []).append(RangeAccount(username, Secret(password), role == "admin"))
    return out


def node_of(vm: dict, range_id: str) -> str:
    """The template node id a built VM is (its ``node_id``, else its name without the range prefix)."""
    return str(vm.get("node_id") or str(vm.get("name") or "").removeprefix(f"{range_id[:8]}-"))


_node = node_of


def attach(template: dict, session: Session, range_id: str) -> dict:
    """A copy of ``template`` whose ``vms`` carry their accounts."""
    vms = template.get("vms")
    if not isinstance(vms, list) or not vms:
        return template
    declared = load(session, range_id)
    copies = []
    matched: set[str] = set()
    for vm in vms:
        vm = dict(vm)
        node = _node(vm, range_id)
        if node in declared:
            vm["_accounts"] = list(declared[node])
            matched.add(node)
        copies.append(vm)
    if unmatched := sorted(set(declared) - matched):
        logger.warning("range %s has accounts on machines it does not build: %s", range_id, ", ".join(unmatched))
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
