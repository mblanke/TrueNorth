"""Lab-session tasks (app/lab_sessions drives them; it owns the session records).

Kept out of tasks.py (ADR 0003). Nothing here touches the database: the API passes in
everything a task needs, so there is no raw SQL to drift from the API's models.
"""

from __future__ import annotations

import asyncio
import logging

from .base_tasks import ReliableTask, _get_backend
from .celery_app import app

logger = logging.getLogger(__name__)


@app.task(base=ReliableTask, bind=True, name="worker.tasks.reconcile_lab_vms")
def reconcile_lab_vms(self, range_ids: list, backend: str):
    """Delete every VM still on the hypervisor for these lab-session ranges.

    The API passes only ranges of lab sessions that are finished, so whatever is left is
    a leak: a partial provision whose output was never recorded, or a destroy that failed
    half-way. VM names start with their range id (vsphere_api.provision), which is how
    they are found; nothing else on the hypervisor can match a range's UUID.
    """
    provisioner = _get_backend(backend)
    removed: dict[str, int] = {}
    # One inventory listing for the whole batch, then match each range's name prefix.
    inventory = asyncio.run(provisioner.find_vms(""))
    for range_id in range_ids:
        leftovers = [vm for vm in inventory if str(vm.get("name", "")).startswith(f"{range_id}-")]
        if not leftovers:
            continue
        result = asyncio.run(provisioner.destroy(range_id, {"vms": leftovers}))
        removed[range_id] = result.resources_removed
        logger.warning("[lab] removed %d leaked VM(s) of range %s", result.resources_removed, range_id)
    return {"status": "ok", "removed": removed}
