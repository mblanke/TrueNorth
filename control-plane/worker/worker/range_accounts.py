"""The accounts a range declares, handed to its provisioner for one build (ADR 0012 §12).

The API stores them sealed in ``range_credentials`` when a build is accepted (generated
passwords are made there, once per range). ``attach`` reads and unseals them and returns a
copy of the rendered template whose VMs carry, in memory only:

* ``_accounts``: the VM's declared accounts (``RangeAccount``; passwords in ``Secret``,
  which neither prints nor pickles), matched on the VM's template node id;
* ``_default_login``: ``TN_RANGE_DEFAULT_PASSWORD`` as a ``Secret``, when it is set: the
  password a VM with no declared admin account gets instead of a random one.

The caller's ``template`` is not changed, so nothing after the build (the stored output,
Greyspace, logs) ever holds a password. The provisioner drops both keys when it returns.
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


def default_password() -> Secret | None:
    """The platform's generic range password (installer: vault ``tn_range_default_password``)."""
    value = os.getenv(DEFAULT_PASSWORD_ENV, "").strip()
    return Secret(value) if value else None


def load(session: Callable[[], AbstractContextManager], range_id: str) -> dict[str, list[RangeAccount]]:
    """machine (node id) -> its declared accounts."""
    with session() as db:
        rows = db_ops.range_accounts(db, range_id)
    out: dict[str, list[RangeAccount]] = {}
    for machine, username, password, role in rows:
        if password:
            out.setdefault(str(machine), []).append(RangeAccount(username, Secret(password), role == "admin"))
    return out


def attach(template: dict, session: Callable[[], AbstractContextManager], range_id: str) -> dict:
    """A copy of ``template`` whose ``vms`` carry their accounts and the default login."""
    vms = template.get("vms")
    if not isinstance(vms, list) or not vms:
        return template
    declared = load(session, range_id)
    default = default_password()
    prefix = f"{range_id[:8]}-"
    copies = []
    matched: set[str] = set()
    for vm in vms:
        vm = dict(vm)
        node = str(vm.get("node_id") or str(vm.get("name") or "").removeprefix(prefix))
        if node in declared:
            vm["_accounts"] = list(declared[node])
            matched.add(node)
        if default is not None:
            vm["_default_login"] = default
        copies.append(vm)
    if unmatched := sorted(set(declared) - matched):
        logger.warning("range %s declares accounts on machines it does not build: %s", range_id, ", ".join(unmatched))
    if declared:
        logger.info("range %s: declared accounts for %d machine(s)", range_id, len(matched))
    return {**template, "vms": copies}
