"""The periodic drift check (ADR 0013 §5, range certification S5, issue #243).

Beat-scheduled next to ``health_check_ranges`` (celery_app.py, every RANGE_DRIFT_SECONDS,
default 300). For every built range nothing is changing (ready, running or stopped, no
operation in flight, its lease free: worker/range_inventory_db.drift_ranges) whose
provisioner_output records an inventory (S5 and later builds), the backend reads what
vCenter holds now and ``range_inventory.compare`` grades it against the record: VM ids
(moid + instance UUID, a VM re-registered under another moid included), CPU/RAM, NIC port
groups and MACs, port group VLANs, snapshots (against the ids recorded as they were taken;
names starting with VSPHERE_DRIFT_IGNORE_SNAPSHOT_PREFIXES, a backup tool's, are not drift),
and items carrying the range's annotation, tag or owner mark that were never recorded.

**Scale.** Ranges are grouped by vCenter (worker/range_sweep.groups); each group is read
with one login and a handful of calls for all of its ranges (vSphere ``live_inventories``:
one property-collector call for every VM, one for the port groups, one pass for the marks).
Each range is saved as soon as its group is read; one group failing does not lose the
others. Sweeps never overlap: a Redis lock with a TTL, owned by a token and renewed while
the sweep runs (redis_lock.py), is held for the sweep; a sweep that finds it held, or cannot
reach Redis, does nothing, and one that loses it stops before its next range. (Not a
PostgreSQL session advisory lock: through pgbouncer's transaction pooling the lock and its
unlock land on different server connections.)

**Never false drift.** Right before a range's result is saved its state is read again in
the same transaction (``range_inventory_db.unsettled``): a range that is no longer idle, or
that it or one of its snapshots changed while it was read, is skipped this round, so a
snapshot, restore or operation that ran during the read never lands in the append-only
record.

Results go to three places:

* the range's ``drift`` check (range_inventory_checks), which the range page shows as its
  drift warnings; ``changed_at`` moves only when what was found changed;
* an API notification (``drift_detected`` / ``drift_cleared``) when it changes;
* the range's newest certification record, ``drift`` section, appended only when the
  result differs from the section's latest entry (range_inventory_db.append_entry).

A reading that fails (vCenter unreachable) is stored as ``error`` on the check and goes
into no record: it says nothing about the range. Kept out of tasks.py (ADR 0003).
"""

from __future__ import annotations

import json
import logging
import os
import threading

from . import range_inventory, range_inventory_db, range_sweep
from .celery_app import app
from .redis_lock import RedisLock
from .task_plumbing import db_session, notify_api

logger = logging.getLogger("truenorth.worker.range_drift")

TASK = "worker.tasks.check_range_drift"
# Snapshots a backup tool takes and removes on its own schedule are not drift.
IGNORE_SNAPSHOT_PREFIXES = tuple(
    p.strip() for p in os.getenv("VSPHERE_DRIFT_IGNORE_SNAPSHOT_PREFIXES", "VEEAM,Veeam,veeam").split(",") if p.strip())
# The sweep's Redis lock. Renewed every TTL/4 while the sweep runs, so a live sweep never loses
# it; a killed worker's lapses within the TTL. Longer than the default hard-minus-soft time
# limit margin (celery_app: 1900 - 1800 s) so the soft limit's cleanup still holds it.
SWEEP_LOCK_KEY = "truenorth:range-drift:lock"
SWEEP_LOCK_TTL_MS = int(float(os.getenv("RANGE_DRIFT_LOCK_TTL_SECONDS", "300")) * 1000)


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


def redis_client():
    """The worker's Redis (its broker, REDIS_URL), short socket timeouts."""
    import redis

    from .tasks import REDIS_URL  # tasks imports this module's siblings at load: read it at call time

    return redis.Redis.from_url(REDIS_URL, socket_timeout=5, socket_connect_timeout=5)


def sweep_lock(client=None) -> RedisLock:
    """The drift sweep's lock (redis_lock.py): one sweep at a time across every worker."""
    return RedisLock(client if client is not None else redis_client(), SWEEP_LOCK_KEY, SWEEP_LOCK_TTL_MS)


@app.task(bind=True, name=TASK)
def check_range_drift(self):
    with sweep_lock() as lock:
        if not lock.held:
            logger.info("[drift] another drift sweep holds the lock (or Redis is down); this one does nothing")
            return {"status": "skipped", "reason": "a drift sweep is already running, or Redis is unavailable"}
        return sweep(lock.lost)


def sweep(lost: threading.Event | None = None) -> dict:
    """``lost``: set when the lock was lost; the sweep then stops before its next range."""
    with db_session() as db:
        rows = [r for r in range_inventory_db.drift_ranges(db) if recorded(_output(r[2]))]
    summary = {"status": "ok", "checked": len(rows), "drifted": [], "errors": [], "skipped": []}
    if not rows:
        return summary
    names = {str(r[0]): r[1] for r in rows}
    grouped, failed = range_sweep.groups(rows)
    for range_id, exc in failed.items():
        settle(range_id, names[range_id], {}, exc, None, summary)
    for provisioner, items in grouped:
        if lost is not None and lost.is_set():
            return {**summary, "status": "lock_lost"}
        with db_session() as db:
            since = range_inventory_db.db_now(db)  # before the read: anything after it voids the reading
        readings = range_sweep.read_group(provisioner, items)
        for range_id, output in items:  # saved as it goes: a later group's failure loses nothing here
            if lost is not None and lost.is_set():  # another sweep may be running now: write nothing
                return {**summary, "status": "lock_lost"}
            settle(range_id, names[range_id], output, readings.get(range_id), since, summary)
    return summary


def settle(range_id: str, name: str, output: dict, reading, since, summary: dict) -> None:
    """Grade one range's reading and store it, unless it changed while it was read."""
    try:
        with db_session() as db:
            if since is not None and (why := range_inventory_db.unsettled(db, range_id, since)):
                logger.info("[drift] range %s (%s) not recorded this round: %s", range_id, name, why)
                summary["skipped"].append(range_id)
                return
            if reading is None or isinstance(reading, BaseException):
                status, text = "error", f"the drift check could not read the range: {reading}"[:500]
                report: dict = {"error": str(reading)}
                summary["errors"].append(range_id)
            else:
                snapshots = range_inventory_db.snapshot_rows(db, [range_id]).get(range_id, [])  # read now
                result = range_inventory.compare(output, reading, snapshots, IGNORE_SNAPSHOT_PREFIXES)
                status, text = result["status"], result["summary"]
                report = {k: result[k] for k in ("findings", "checked", "gaps")}
                if reading.get("errors"):
                    report["read_errors"] = list(reading["errors"])
            changed = range_inventory_db.save_check(db, range_id, "drift", status, text, report)
            if status != "error":
                range_inventory_db.append_entry(db, range_id, "drift", status, text,
                                                {"findings": report["findings"], "gaps": report["gaps"]})
    except Exception:  # noqa: BLE001 -- one range's failure is that range's
        logger.error("[drift] range %s: result not stored", range_id, exc_info=True)
        return
    drifted = bool(report.get("findings"))
    if drifted:
        summary["drifted"].append(range_id)
    if changed and status != "error":
        notify_api("range", {"id": range_id, "event": "drift_detected" if drifted else "drift_cleared",
                             "status": status, "summary": text})
        (logger.warning if drifted else logger.info)("[drift] range %s (%s): %s", range_id, name, text)
