"""Re-run a ready range's isolation probes on demand (range certification S4, issue #242).

The API sends ``check_range_isolation`` (POST /ranges/{id}/certification/isolation-check)
after a change to the network around a range (a firewall rule, a route planted for the
acceptance test). It probes every zone as range_isolation.run does after a build and
stores the new evidence in the range's provisioner_output; the certification record picks
it up on its next read (app/range_certification ``refresh_if_changed``).

Contract:

* only a ``ready`` range is probed, and the evidence is stored only if the range is still
  ``ready`` with the same VMs (a rebuild meanwhile makes it another build's) and no probe
  that started later stored first (db_ops.store_isolation, under the range's row lock);
* one probe run per range at a time (a Redis lock; no Redis: no run, as for the drift sweep);
* it **never changes the range's state**, in enforce mode too: enforce decides at build
  time only. A weaker verdict shows on the certification record.
"""

from __future__ import annotations

import json
import logging
import time

from . import db_ops, range_accounts, range_isolation
from .base_tasks import _get_backend
from .celery_app import app
from .fencing import run_async
from .redis_lock import RedisLock

logger = logging.getLogger("truenorth.worker.isolation_task")

TASK = "worker.tasks.check_range_isolation"
LOCK_KEY = "tn:range-isolation:{}"


def _template(raw: str | None) -> dict:
    """The range template as tasks.provision_range reads it (JSON, else YAML), or {}."""
    if not raw:
        return {}
    try:
        out = json.loads(raw)
    except (TypeError, ValueError):
        try:
            import yaml

            out = yaml.safe_load(raw)
        except Exception:  # noqa: BLE001
            return {}
    return out if isinstance(out, dict) else {}


def _lock(range_id: str) -> RedisLock:
    from .range_drift import redis_client

    budget_ms = int(max(range_isolation.settings().budget, 60) * 1000) + 120_000
    return RedisLock(redis_client(), LOCK_KEY.format(range_id), budget_ms)


def check(range_id: str, lock_factory=_lock) -> dict:
    from .tasks import _db_session

    with _db_session() as db:
        state = db_ops.range_state(db, range_id)
        row = db_ops.range_output_and_backend(db, range_id)
        tmpl = db_ops.range_template_and_backend(db, range_id)
    if state != "ready" or row is None:
        return {"status": "skipped", "reason": f"the range is {state or 'gone'}, not ready"}
    output = json.loads(row[0]) if row[0] else {}
    vm_ids = {str(v.get("vm_id")) for v in output.get("vms") or [] if isinstance(v, dict) and v.get("vm_id")}
    try:
        lock = lock_factory(range_id)
    except range_isolation.SettingsError as exc:
        return {"status": "skipped", "reason": str(exc)}
    with lock:
        if not lock.held:
            return {"status": "skipped", "reason": "another isolation probe of this range is running, or Redis is down"}
        provisioner = _get_backend(row[1], range_id, output=output)
        accounts = range_accounts.load(_db_session, range_id)
        record = run_async(range_isolation.run(provisioner, range_id, output, _template(tmpl[0] if tmpl else None),
                                               accounts, finish_by=time.monotonic()
                                               + range_isolation.settings().budget + 30.0))
        with _db_session() as db:
            outcome = db_ops.store_isolation(db, range_id, record, vm_ids)
    logger.info("[isolation] range %s: %s (%s); %s", range_id, record["verdict"], record["mode"], outcome)
    return {"status": "stored" if outcome == "stored" else "discarded", "reason": outcome,
            "verdict": record["verdict"]}


@app.task(bind=True, name=TASK)
def check_range_isolation(self, range_id: str):
    return check(range_id)
