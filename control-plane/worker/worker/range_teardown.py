"""Teardown by independently identified inventory (ADR 0013 §5, range certification S5, #243).

Two jobs, both outside tasks.py (ADR 0003, its line count is ratcheted):

* ``record``: ``destroy_range`` hands it the backend's DestroyResult; its ``teardown``
  report (what was deleted by recorded id, which recorded ids remain, what carries the
  range's mark but was not deleted, what could not be checked) is graded
  (range_inventory.teardown_status) and stored as the range's ``teardown`` check. The API
  writes it into the Range Certification Record when it settles the destroy, and shows it
  on the range page. A backend that does not verify its teardown (mock, Proxmox) records
  nothing here; the record then says so.

* ``delete_confirmed_teardown_items``: an administrator confirmed unidentified items by
  typing their moids back (app/range_inventory/service.py checked them against the listed
  set, audited it and recorded the override). Here they are checked again: only moids
  still listed in the range's stored report are passed on, only while the range is
  ``destroyed`` or ``failed``, and the backend deletes each only if vCenter still holds
  exactly what was listed (moid *and* instance UUID, or port group key and owner mark). It
  then verifies the teardown again from scratch and stores the new report. Under the
  range's lease (``fenced`` with no state), so it never runs beside a build or teardown.
  Not refused by a maintenance window: it is cleanup an administrator ordered with a
  reason, as an emergency teardown is (worker/maintenance_windows.py gates only provision,
  restore and destroy).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from . import db_ops, range_inventory, range_inventory_db
from .base_tasks import ReliableTask, _get_backend
from .celery_app import app
from .fencing import fenced, run_async
from .task_plumbing import db_session, notify_api

logger = logging.getLogger("truenorth.worker.range_teardown")

TASK = "worker.tasks.delete_confirmed_teardown_items"
CONFIRMABLE_STATES = ("destroyed", "failed")


def record(session_factory, range_id: str, result) -> str | None:
    """Store the teardown report of a destroy (None when the backend gave none). Never
    raises: a teardown is not failed for want of its report; the error log keeps it."""
    report = getattr(result, "teardown", None)
    if not isinstance(report, dict):
        return None
    status, summary = range_inventory.teardown_status(report)
    try:
        with session_factory() as db:
            range_inventory_db.save_check(db, range_id, "teardown", status, summary, report)
    except Exception:  # noqa: BLE001
        logger.error("range %s: teardown report not stored: %s", range_id, json.dumps(report, default=str)[:4000],
                     exc_info=True)
        return None
    if status != range_inventory.PASS:
        logger.warning("range %s: teardown %s: %s", range_id, status, summary)
    return status


@app.task(base=ReliableTask, bind=True, name=TASK)
@fenced("confirm_teardown", None)  # the lease only: never beside a build or teardown of the range
def delete_confirmed_teardown_items(self, range_id: str, moids: list, requested_by: str | None = None):
    """Delete the confirmed unidentified items of a torn-down range (module docstring)."""
    with db_session() as db:
        state = range_inventory_db.range_state(db, range_id)
        check = range_inventory_db.load_check(db, range_id, "teardown")
        row = db_ops.range_output_and_backend(db, range_id)
    if state not in CONFIRMABLE_STATES:
        logger.warning("range %s is %s now; confirmed items not deleted", range_id, state)
        return {"status": "skipped", "range_id": range_id, "state": state}
    prior = (check or {}).get("report") or {}
    listed = {str(u.get("moid")): u for u in prior.get("unidentified") or [] if isinstance(u, dict) and u.get("moid")}
    items = [listed[str(m)] for m in dict.fromkeys(moids or []) if str(m) in listed]
    not_listed = [{"moid": str(m), "why": "not listed in the range's last teardown report"}
                  for m in dict.fromkeys(moids or []) if str(m) not in listed]
    if not items:
        logger.warning("range %s: none of the confirmed items %s is listed now; nothing deleted", range_id, moids)
        return {"status": "nothing_listed", "range_id": range_id}
    output = json.loads(row[0]) if row and row[0] else {}
    backend = (row[1] if row and row[1] else None)
    provisioner = _get_backend(backend, range_id, output=output, teardown=True)  # the vCenter it was built on
    if not getattr(provisioner, "supports_inventory", False):
        raise RuntimeError(f"backend {backend!r} does not record a range inventory; delete the items by hand")
    fresh = run_async(provisioner.delete_confirmed(range_id, output, items))
    at = datetime.now(UTC).isoformat()
    done = [{**d, "by": requested_by, "at": at} for d in fresh.get("confirmed_deleted") or []]
    report = {**fresh, "deleted": prior.get("deleted") or [],
              "confirmed_deleted": [*(prior.get("confirmed_deleted") or []), *done],
              "confirm_skipped": [*(fresh.get("confirm_skipped") or []), *not_listed]}
    status, summary = range_inventory.teardown_status(report)
    with db_session() as db:
        range_inventory_db.save_check(db, range_id, "teardown", status, summary, report)
    notify_api("range", {"id": range_id, "event": "teardown_verified", "status": status})
    logger.warning("range %s: confirmed items deleted %s, skipped %s; teardown now %s", range_id,
                   [d.get("moid") for d in done], [s.get("moid") for s in report["confirm_skipped"]], status)
    return {"status": status, "range_id": range_id, "deleted": [d.get("moid") for d in done],
            "skipped": report["confirm_skipped"]}
