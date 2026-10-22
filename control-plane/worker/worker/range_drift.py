"""The periodic drift check (ADR 0013 §5, range certification S5, issue #243).

Beat-scheduled next to ``health_check_ranges`` (celery_app.py, every RANGE_DRIFT_SECONDS,
default 300). For every built range nothing is changing (ready, running or stopped, its
lease free: worker/range_inventory_db.drift_ranges) whose provisioner_output records an
inventory (S5 and later builds), the backend reads what vCenter holds now under the
recorded ids (``live_inventory``; one login per vCenter, worker/range_sweep.py) and
``range_inventory.compare`` grades it against the record: VM ids (moid + instance UUID),
CPU/RAM, NIC port groups and MACs, port group VLANs, and snapshots (against the ids
recorded as they were taken).

Results go to three places:

* the range's ``drift`` check (range_inventory_checks), which the range page shows as its
  drift warnings; ``changed_at`` moves only when what was found changed;
* an API notification (``drift_detected`` / ``drift_cleared``) when it changes;
* the range's newest certification record, ``drift`` section, appended only when the
  result differs from the section's latest entry (range_inventory_db.append_entry), so
  the record keeps every change over the range's life without one entry per run.

A reading that fails (vCenter unreachable) is stored as ``error`` on the check and goes
into no record: it says nothing about the range. Kept out of tasks.py (ADR 0003).
"""

from __future__ import annotations

import json
import logging

from . import range_inventory, range_inventory_db, range_sweep
from .celery_app import app
from .task_plumbing import db_session, notify_api

logger = logging.getLogger("truenorth.worker.range_drift")

TASK = "worker.tasks.check_range_drift"


def _output(raw) -> dict:
    try:
        out = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def recorded(output: dict) -> bool:
    """Whether the range's build recorded an inventory to compare with (S5 and later)."""
    return bool(output.get("inventory")) or any(isinstance(v, dict) and isinstance(v.get("identity"), dict)
                                                for v in output.get("vms") or [])


@app.task(bind=True, name=TASK)
def check_range_drift(self):
    with db_session() as db:
        rows = [r for r in range_inventory_db.drift_ranges(db) if recorded(_output(r[2]))]
        snapshots = range_inventory_db.snapshot_rows(db, [str(r[0]) for r in rows])
    if not rows:
        return {"status": "ok", "checked": 0}
    readings = range_sweep.live_inventories(rows)
    summary = {"status": "ok", "checked": len(rows), "drifted": [], "errors": []}
    for row in rows:
        range_id = str(row[0])
        reading = readings.get(range_id)
        try:
            if reading is None or isinstance(reading, BaseException):
                raise reading or RuntimeError("not read")
            result = range_inventory.compare(_output(row[2]), reading, snapshots.get(range_id, []))
            status, text = result["status"], result["summary"]
            report = {k: result[k] for k in ("findings", "checked", "gaps")}
            if reading.get("errors"):
                report["read_errors"] = list(reading["errors"])
        except Exception as exc:  # noqa: BLE001 -- one range's failure is that range's
            status, text, report = "error", f"the drift check could not read the range: {exc}"[:500], {"error": str(exc)}
            summary["errors"].append(range_id)
        try:
            with db_session() as db:
                changed = range_inventory_db.save_check(db, range_id, "drift", status, text, report)
                if status != "error":
                    range_inventory_db.append_entry(db, range_id, "drift", status, text,
                                                    {"findings": report["findings"], "gaps": report["gaps"]})
        except Exception:  # noqa: BLE001
            logger.error("[drift] range %s: result not stored (%s: %s)", range_id, status, text, exc_info=True)
            continue
        if status in (range_inventory.FAIL, range_inventory.INCOMPLETE) and report.get("findings"):
            summary["drifted"].append(range_id)
        if changed and status != "error":
            drifted = bool(report.get("findings"))
            notify_api("range", {"id": range_id, "event": "drift_detected" if drifted else "drift_cleared",
                                 "status": status, "summary": text})
            (logger.warning if drifted else logger.info)("[drift] range %s (%s): %s", range_id, row[1], text)
    return summary
