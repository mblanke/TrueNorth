"""Power a range's VMs off and on: the worker side of stop/start range operations.

The API records the operation and moves the range to ``stopping``/``starting``
(app/range_ops.py). As with provision and destroy (fencing.py), a task acts only while
the range is still in that state, so a duplicate or late delivery touches no hypervisor.

Outcome, written only from the in-progress state:

* success: ``stopped`` / ``ready``, any earlier error cleared;
* a provisioner that did not power every VM (``partial``) or raised: retried; the last
  attempt puts the range back where its VMs still are (``ready`` / ``stopped``) with the
  error, which the API settles as a failed operation. Powering off or on twice is
  harmless, so retrying a partial result is safe.
"""

from __future__ import annotations

import asyncio
import logging

from .celery_app import app
from .fencing import skipped
from .range_rows import provisioner_output
from .reliable import ReliableTask, _last_attempt

logger = logging.getLogger("worker.power")


def _power(task, range_id: str, action: str, in_progress: str, done: str, back: str) -> dict:
    from . import tasks  # not at import time: see reliable.py

    logger.info("[%s] range %s", action, range_id)
    if not tasks._update_range_state(range_id, in_progress, only_from=(in_progress,)):
        return skipped(action, range_id, in_progress)
    try:
        output, backend = provisioner_output(range_id)
        provisioner = tasks._get_backend(backend)
        call = provisioner.stop if action == "stop" else provisioner.start
        result = asyncio.run(call(range_id, output))
        if result.status != "ok":
            raise RuntimeError("; ".join(result.errors) or f"{action} did not reach every VM")
        tasks._update_range_state(range_id, done, only_from=(in_progress,), clear_error=True)
        tasks._notify_api("range", {"id": range_id, "state": done})
        return {"status": done, "range_id": range_id}
    except Exception as e:
        if _last_attempt(task):  # a retry must still find the range in progress
            tasks._update_range_state(range_id, back, error=str(e), only_from=(in_progress,))
            tasks._notify_api("range", {"id": range_id, "state": back, "error": str(e)})
        logger.error("[%s] range %s FAILED: %s", action, range_id, e)
        raise


@app.task(base=ReliableTask, bind=True, name="worker.tasks.stop_range")
def stop_range(self, range_id: str) -> dict:
    """Power a range's VMs off."""
    return _power(self, range_id, "stop", "stopping", done="stopped", back="ready")


@app.task(base=ReliableTask, bind=True, name="worker.tasks.start_range")
def start_range(self, range_id: str) -> dict:
    """Power a range's VMs on."""
    return _power(self, range_id, "start", "starting", done="ready", back="stopped")
