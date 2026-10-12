"""Lab-session tasks (app/lab_sessions drives them; it owns the session records).

Kept out of tasks.py (ADR 0003). The API passes in everything a task needs; the only
database read is each range's hypervisor connection (base_tasks._get_backend, through
db_ops on the generated table mirror), so there is no raw SQL to drift from the API's models.
"""

from __future__ import annotations

import asyncio
import json
import logging

from .base_tasks import ReliableTask, _get_backend
from .celery_app import app

logger = logging.getLogger(__name__)


def _by_endpoint(range_ids: list, backend: str) -> tuple[list[tuple[object, list[str]]], dict[str, str]]:
    """The ranges grouped by the provisioner (endpoint and login) each one's own tenant's
    connection gives, and the ranges whose connection could not be resolved, with why."""
    groups: dict[str, tuple[object, list[str]]] = {}
    unresolved: dict[str, str] = {}
    for range_id in range_ids:
        try:
            provisioner = _get_backend(backend, str(range_id), teardown=True)
        except Exception as exc:  # noqa: BLE001 — that range is left for the next run; the rest go on
            unresolved[str(range_id)] = str(exc)
            logger.error("[lab] range %s: no usable hypervisor connection, its leftovers are not cleaned: %s",
                         range_id, exc)
            continue
        record = getattr(provisioner, "connection_record", None)
        rec = record() if callable(record) else None
        key = json.dumps(rec if isinstance(rec, dict) else None, sort_keys=True)
        groups.setdefault(key, (provisioner, []))[1].append(str(range_id))
    return list(groups.values()), unresolved


@app.task(base=ReliableTask, bind=True, name="worker.tasks.reconcile_lab_vms")
def reconcile_lab_vms(self, range_ids: list, backend: str):
    """Delete every VM still on the hypervisor for these lab-session ranges.

    The API passes only ranges of lab sessions that are finished, so whatever is left is
    a leak: a partial provision whose output was never recorded, or a destroy that failed
    half-way. A VM belongs to a range when the backend tagged it with the range's full id
    (``range_id`` in find_vms: vSphere's annotation, set as the VM is created), or when its
    name starts with the full id (VMs built before the tag, and backends without one);
    nothing else on the hypervisor can match a range's UUID.

    Each range is looked for on its own vCenter (its tenant's connection, as its build
    used; until 2026-10-11 only the worker's VSPHERE_* one, so a tenant vCenter's leaks
    were never found), with one inventory listing per vCenter for the whole batch.
    """
    removed: dict[str, int] = {}
    groups, unresolved = _by_endpoint(range_ids, backend)
    for provisioner, ids in groups:
        inventory = asyncio.run(provisioner.find_vms(""))
        for range_id in ids:
            leftovers = [
                vm for vm in inventory
                if vm.get("range_id") == range_id or str(vm.get("name", "")).startswith(f"{range_id}-")
            ]
            if not leftovers:
                continue
            result = asyncio.run(provisioner.destroy(range_id, {"vms": leftovers}))
            removed[range_id] = result.resources_removed
            logger.warning("[lab] removed %d leaked VM(s) of range %s", result.resources_removed, range_id)
    out: dict = {"status": "ok", "removed": removed}
    if unresolved:
        out["unresolved"] = unresolved
    return out
