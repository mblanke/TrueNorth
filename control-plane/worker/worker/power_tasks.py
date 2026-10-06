"""Range power: stop and start (CR1-05 in docs/review/codereview1.md).

Until these, POST /ranges/{id}/stop set the range to ``stopped`` and sent nothing: the
VMs kept running, and there was no start at all. The API now records the request by
moving the range to ``stopping`` / ``starting`` and sends one of these tasks. Each is
fenced on that state like provision and destroy (worker/fencing.py), and writes
``stopped`` / ``running`` only once the hypervisor has done it. Only the last attempt
records ``failed``.

Kept out of tasks.py (ADR 0003).
"""

from __future__ import annotations

import json
import logging
import os

import sqlalchemy as sa

from .celery_app import app
from .fencing import _GUID, FINAL_ERRORS, fenced, last_attempt, run_async
from .reliable import ReliableTask

logger = logging.getLogger("truenorth.worker.power")

# The columns of the API's ranges table this module reads (Core, not raw SQL).
_ranges = sa.Table(
    "ranges",
    sa.MetaData(),
    sa.Column("id", _GUID(), primary_key=True),
    sa.Column("provisioner_output", sa.Text()),
    sa.Column("provisioner_backend", sa.String(64)),
)


def _power(task, range_id: str, action: str, claim: str, done: str) -> dict:
    from .tasks import _db_session, _get_backend, _notify_api, _update_range_state

    logger.info("[%s] range %s", action, range_id)
    try:
        with _db_session() as db:
            cols = (_ranges.c.provisioner_output, _ranges.c.provisioner_backend)
            row = db.execute(sa.select(*cols).where(_ranges.c.id == range_id)).first()
        prov_output = json.loads(row[0]) if row and row[0] else {}
        backend = (row[1] if row and row[1] else None) or os.getenv("PROVISIONER_BACKEND", "mock")
        if not prov_output.get("vms"):
            raise RuntimeError(f"{action}: the range has no VMs recorded; nothing to power")
        result = run_async(getattr(_get_backend(backend), action)(range_id, prov_output))
        if result.status != "ok":
            raise RuntimeError(f"{action} {result.status}: {'; '.join(result.errors) or 'no detail'}")
        _update_range_state(range_id, done, only_from=(claim,), clear_error=True)
        _notify_api("range", {"id": range_id, "state": done})
        return {"status": done, "range_id": range_id}
    except Exception as e:
        if isinstance(e, FINAL_ERRORS) or last_attempt(task):  # a retry must still find it in `claim`
            _update_range_state(range_id, "failed", error=str(e), only_from=(claim,))
            _notify_api("range", {"id": range_id, "state": "failed", "error": str(e)})
        logger.error("[%s] range %s FAILED: %s", action, range_id, e)
        raise


@app.task(base=ReliableTask, bind=True, name="worker.tasks.stop_range")
@fenced("stop", "stopping")
def stop_range(self, range_id: str):
    """Power off every VM of a range."""
    return _power(self, range_id, "stop", "stopping", "stopped")


@app.task(base=ReliableTask, bind=True, name="worker.tasks.start_range")
@fenced("start", "starting")
def start_range(self, range_id: str):
    """Power on every VM of a range."""
    return _power(self, range_id, "start", "starting", "running")
