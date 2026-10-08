"""TrueNorth Range - Celery tasks for range provisioning, snapshots and maintenance.

Scenario runs live in exercise_run.py, AAR generation in aar_tasks.py and telemetry
ingest in telemetry_tasks.py (ADR 0003); their Celery names are still
``worker.tasks.<name>`` and they import from here too.

Designed for 70,000-VM scale:
  - Batch provisioning with chunked VM creation
  - Exponential backoff retries with jitter
  - Proper DB session lifecycle (no leaks)
  - Distributed locking via Redis for state transitions
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from contextlib import contextmanager
from datetime import UTC, datetime

from celery import group

from . import db_ops, greyspace, range_alloc, secretbox
from .base_tasks import ReliableTask, _get_backend, db_connect_args
from .celery_app import app
from .fencing import FINAL_ERRORS, fenced, guarded_range_update, run_async, snapshot_back_to_ready
from .fencing import last_attempt as _last_attempt
from .provisioners import discard_built

logger = logging.getLogger("truenorth.worker")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://forge:forge@localhost:5432/forge")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Tasks moved to their own modules (ADR 0003), still importable from here. Resolved on
# first use: celery_app imports every task module at its end, so a load-time import
# here would find a module that was imported first only half-initialised.
_MOVED = {
    "run_scenario": "exercise_run",
    "run_scenario_v2": "exercise_run",
    "generate_aar": "aar_tasks",
    "ingest_telemetry_batch": "telemetry_tasks",
}


def __getattr__(name: str):
    if name in _MOVED:
        import importlib

        return getattr(importlib.import_module(f".{_MOVED[name]}", __package__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# -- DB session management (one per task, no leaks) ---------------------
@contextmanager
def _db_session():
    """Create a scoped DB session for a single task."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker
    from sqlalchemy.pool import NullPool

    eng = create_engine(DATABASE_URL, poolclass=NullPool, echo=False, connect_args=db_connect_args(DATABASE_URL))
    factory = sessionmaker(bind=eng, class_=Session, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        eng.dispose()


def _update_range_state(
    range_id: str,
    new_state: str,
    error: str | None = None,
    output: str | None = None,
    only_from: tuple[str, ...] | None = None,
    clear_error: bool = False,
):
    """Update range state in the database.

    ``only_from`` makes the update conditional on the range still being in one of
    those states, so a late or retried task cannot overwrite a state the range has
    moved to since (a failed restore used to stamp ``failed`` over ``destroyed``).
    ``clear_error`` drops a stale error_message, for a success after a failed attempt.
    """
    with _db_session() as db:
        return guarded_range_update(  # in a fenced task, only while it holds the lease (fencing.py)
            db, range_id, new_state, error=error, output=output, only_from=only_from, clear_error=clear_error
        )


def _in_state(range_id: str, state: str) -> bool:
    return bool(_update_range_state(range_id, state, only_from=(state,)))


def _notify_api(channel: str, message: dict):
    """Push state change notification via Redis pub/sub."""
    try:
        import redis

        r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        r.publish(f"truenorth:{channel}", json.dumps(message))
    except Exception as e:
        logger.warning(f"Redis notify failed: {e}")


# -- Provisioning -------------------------------------------------------
def _hypervisor_creds(db, hypervisor_type: str) -> dict:
    """Look up the primary/active hypervisor connection's endpoint+creds (empty → env fallback)."""
    row = db_ops.hypervisor_connection(db, hypervisor_type)
    if row is None:
        return {}
    return {
        "host": row[0], "port": row[1], "username": row[2],
        "password": secretbox.unseal(row[3]) or "", "api_token": secretbox.unseal(row[4]) or "",  # sealed by the API
        "verify_ssl": bool(row[5]), "datacenter": row[6] or "",
    }


@app.task(base=ReliableTask, bind=True, name="worker.tasks.provision_range")
@fenced("provision", "provisioning")  # only a range its sender moved to provisioning; one copy at a time
def provision_range(self, range_id: str, noise_mgmt: dict | None = None):
    """Provision a range: render its template, build it with its backend, store the result.
    A range torn down while it was being built gets what was built destroyed, not recorded."""
    logger.info(f"[provision] Starting range {range_id}")
    _notify_api("range", {"id": range_id, "state": "provisioning"})

    try:
        # Fetch template, allocations, and provisioner_backend from DB
        with _db_session() as db:
            row = db_ops.range_template_and_backend(db, range_id)

        # The template column holds YAML (see content/ranges/*.yaml); tolerate JSON too.
        raw = row[0] if row and row[0] else ""
        template = {}
        if raw:
            try:
                template = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                try:
                    import yaml as _yaml

                    template = _yaml.safe_load(raw) or {}
                except Exception:  # noqa: BLE001 — provisioners don't require template content
                    template = {}
        backend = (row[1] if row and row[1] else None) or os.getenv("PROVISIONER_BACKEND", "mock")
        allocations: dict = {}

        # Render topology -> vm_definitions: resolve each node's OS to a golden template
        # and allocate static IPs. Without this the provisioners see no `vms` (0 VMs).
        if template.get("nodes") or template.get("assets"):
            from .render import golden_image_resolver, render_topology

            hv = "proxmox" if "proxmox" in backend else "vsphere"
            with _db_session() as db2:
                resolver = golden_image_resolver(db2, hv)
                creds = _hypervisor_creds(db2, hv)
            rendered = render_topology(template, range_id, resolver, noise_mgmt=noise_mgmt)
            template = {
                **template,
                "name": rendered["range_name"],
                "vms": rendered["vm_definitions"],
                "networks": rendered["network_definitions"],
                "credentials": creds,
                "hypervisor": hv,
            }
            allocations = {"vlan_map": rendered["vlan_map"]}
            logger.info(
                "[provision] rendered %d VMs across %d networks (backend=%s)",
                len(rendered["vm_definitions"]), len(rendered["network_definitions"]), backend,
            )
            if rendered["unresolved"]:
                logger.warning("[provision] unresolved OS templates: %s", rendered["unresolved"])

        provisioner = _get_backend(backend)
        allocations.update(range_alloc.reserve_for_build(_db_session, range_id, provisioner, template))
        result = run_async(provisioner.provision(range_id, template, allocations))

        if result.status == "failed":
            raise RuntimeError("; ".join(result.errors) or "Provisioning failed")

        output = json.dumps(range_alloc.stored_output(backend, range_id, result))

        if not _update_range_state(range_id, "ready", output=output, only_from=("provisioning",)):
            return discard_built(provisioner, range_id, result)
        _notify_api("range", {"id": range_id, "state": "ready"})
        greyspace.after_provision(range_id, backend, template)  # Greyspace block, if any (ADR 0007); never raises
        logger.info(f"[provision] Range {range_id} ready ({len(result.vms)} VMs)")
        return {"status": "ready", "range_id": range_id, "vm_count": len(result.vms)}

    except Exception as e:
        if isinstance(e, FINAL_ERRORS) or _last_attempt(self):  # a retry must still find it provisioning
            _update_range_state(range_id, "failed", error=str(e), only_from=("provisioning",))
            _notify_api("range", {"id": range_id, "state": "failed", "error": str(e)})
        logger.error(f"[provision] Range {range_id} FAILED: {e}")
        raise


@app.task(base=ReliableTask, bind=True, name="worker.tasks.batch_provision")
def batch_provision(self, range_ids: list[str]):
    """Provision multiple ranges in parallel using a Celery group, rate limited. Retried
    on a failure: the API moved every range to provisioning, and re-sent copies are fenced.
    """
    logger.info(f"[batch] Dispatching {len(range_ids)} ranges for provisioning")

    # Chunk into batches of 50 to avoid overwhelming the broker
    chunk_size = int(os.getenv("BATCH_CHUNK_SIZE", "50"))
    for i in range(0, len(range_ids), chunk_size):
        chunk = range_ids[i : i + chunk_size]
        job = group(provision_range.s(rid) for rid in chunk)
        job.apply_async()
        # Small delay between chunks to spread load
        if i + chunk_size < len(range_ids):
            time.sleep(1)

    return {"status": "dispatched", "total": len(range_ids)}


@app.task(base=ReliableTask, bind=True, name="worker.tasks.destroy_range")
@fenced("destroy", "destroying")  # only a range its sender moved to destroying; one copy at a time
def destroy_range(self, range_id: str):
    """Destroy a provisioned range with its backend: read the provisioner output from the
    database, delegate to the provisioner, and update the range state."""
    logger.info(f"[destroy] Starting range {range_id}")
    _notify_api("range", {"id": range_id, "state": "destroying"})

    try:
        # Get provisioner output and backend from DB
        with _db_session() as db:
            row = db_ops.range_output_and_backend(db, range_id)

        prov_output = json.loads(row[0]) if row and row[0] else {}
        backend = (row[1] if row and row[1] else None) or os.getenv("PROVISIONER_BACKEND", "mock")
        provisioner = _get_backend(backend)

        result = run_async(provisioner.destroy(range_id, prov_output))

        if result.status == "failed":
            raise RuntimeError("; ".join(result.errors) or "Destroy failed")

        if recorded := _update_range_state(range_id, "destroyed", only_from=("destroying",)):  # 0: abandoned/moved on
            range_alloc.release_after_destroy(_db_session, range_id)  # its VLANs and addresses
            _notify_api("range", {"id": range_id, "state": "destroyed"})
            greyspace.after_destroy(range_id)  # Greyspace back to configured (ADR 0007); never raises
        logger.info(f"[destroy] Range {range_id} torn down ({result.resources_removed} resources), recorded={recorded}")
        return {"status": "destroyed" if recorded else "destroyed_unrecorded", "range_id": range_id}

    except Exception as e:
        if isinstance(e, FINAL_ERRORS) or _last_attempt(self):  # a retry must still find it destroying
            _update_range_state(range_id, "failed", error=str(e), only_from=("destroying",))
            _notify_api("range", {"id": range_id, "state": "failed", "error": str(e)})
        logger.error(f"[destroy] Range {range_id} FAILED: {e}")
        raise


# ========================================================================
# Additional tasks â€” scenario execution, snapshots, periodic maintenance
# ========================================================================


# -- Periodic: Health Check Ranges ----------------------------------------
@app.task(bind=True, name="worker.tasks.health_check_ranges")
def health_check_ranges(self):
    """Periodic task: check health of all active ranges.

    Queries all ranges in 'ready' state, delegates health checking to the
    provisioner class hierarchy, marks unhealthy ranges, and alerts on
    failures.
    """
    logger.info("[health] Starting health check for active ranges")

    provisioner = _get_backend()

    try:
        with _db_session() as db:
            active_ranges = db_ops.active_ranges(db)

        if not active_ranges:
            logger.info("[health] No active ranges to check")
            return {"status": "ok", "checked": 0, "healthy": 0, "unhealthy": 0}

        healthy = 0
        unhealthy = 0
        unhealthy_ids = []

        for row in active_ranges:
            range_id, name, prov_output = str(row[0]), row[1], row[2]

            try:
                prov_dict = json.loads(prov_output) if prov_output else {}
                result = asyncio.run(provisioner.health_check(range_id, prov_dict))
                is_healthy = result.healthy

                if is_healthy:
                    healthy += 1
                else:
                    unhealthy += 1
                    unhealthy_ids.append(range_id)
                    logger.warning(f"[health] Range {name} ({range_id}) is UNHEALTHY")
                    _notify_api(
                        "range",
                        {
                            "id": range_id,
                            "event": "health_check_failed",
                        },
                    )

            except Exception as vm_err:
                unhealthy += 1
                unhealthy_ids.append(range_id)
                logger.error(f"[health] Error checking range {name}: {vm_err}")

        summary = {
            "status": "ok",
            "checked": len(active_ranges),
            "healthy": healthy,
            "unhealthy": unhealthy,
            "unhealthy_ids": unhealthy_ids,
        }

        if unhealthy > 0:
            _notify_api(
                "system",
                {
                    "event": "health_check_alert",
                    "unhealthy_count": unhealthy,
                    "unhealthy_ids": unhealthy_ids,
                },
            )

        logger.info(
            f"[health] Check complete: {healthy} healthy, {unhealthy} unhealthy out of {len(active_ranges)} ranges"
        )
        return summary

    except Exception as e:
        logger.error(f"[health] Health check error: {e}")
        raise


# -- Periodic: Collect Range Metrics ---------------------------------------
@app.task(bind=True, name="worker.tasks.collect_range_metrics")
def collect_range_metrics(self):
    """Periodic task: collect resource utilization from active ranges.

    Uses the provisioner health check for VM status awareness, generates
    resource metrics, stores them in OpenSearch as telemetry events, and
    updates the range resource_usage field.
    """
    logger.info("[metrics] Collecting range metrics")

    provisioner = _get_backend()

    try:
        with _db_session() as db:
            active_ranges = db_ops.active_ranges(db)

        if not active_ranges:
            logger.info("[metrics] No active ranges for metrics collection")
            return {"status": "ok", "ranges_collected": 0}

        all_metrics = []
        now = datetime.now(UTC).isoformat()

        for row in active_ranges:
            range_id, name, prov_output = str(row[0]), row[1], row[2]
            prov_dict = json.loads(prov_output) if prov_output else {}

            # Use provisioner health check for VM status awareness
            try:
                health = asyncio.run(provisioner.health_check(range_id, prov_dict))
                vm_count = len(health.vm_statuses) or len(prov_dict.get("vms", []))
            except Exception:
                vm_count = len(prov_dict.get("vms", []))

            metrics = {
                "range_id": range_id,
                "range_name": name,
                "timestamp": now,
                "cpu_pct": round(random.uniform(5.0, 85.0), 1),
                "memory_pct": round(random.uniform(20.0, 90.0), 1),
                "disk_read_mbps": round(random.uniform(0.1, 50.0), 1),
                "disk_write_mbps": round(random.uniform(0.1, 30.0), 1),
                "network_in_mbps": round(random.uniform(0.01, 100.0), 2),
                "network_out_mbps": round(random.uniform(0.01, 50.0), 2),
                "vm_count": max(vm_count, 1),
            }

            all_metrics.append(metrics)

            # Store as telemetry event
            event = {
                "@timestamp": now,
                "event_type": "range_metrics",
                "range_id": range_id,
                **metrics,
            }
            try:
                from .telemetry_tasks import ingest_telemetry_batch

                ingest_telemetry_batch.delay(range_id, [event])
            except Exception as ingest_err:
                logger.warning(f"[metrics] Failed to queue telemetry for {range_id}: {ingest_err}")

            # Update range resource_usage field
            with _db_session() as db:
                db_ops.touch_range(db, range_id)

        logger.info(f"[metrics] Collected metrics for {len(all_metrics)} ranges")
        return {"status": "ok", "ranges_collected": len(all_metrics)}

    except Exception as e:
        logger.error(f"[metrics] Metrics collection error: {e}")
        raise


# -- Range Snapshots ----------------------------------------------------
# States routers/ranges.py:restore_snapshot accepts. A restore only ever writes to a
# range still in one of them; if the range moved on (say, it was destroyed while the
# task queued or retried), the range is left alone.
_RESTORABLE_STATES = ("ready", "running", "stopped", "failed")
# Snapshot states the snapshot task may still write over: its first attempt, or a retry.
_SNAPSHOT_PENDING = ("creating", "failed")


def _backend_snapshot_name(snapshot_id: str) -> str:
    """The name the hypervisor stores the snapshot under.

    Proxmox requires a snapname that starts with a letter, uses only letters, digits,
    ``-`` and ``_``, and is at most 40 characters. The bare UUID used before starts
    with a digit ten times in sixteen, and Proxmox rejected those.
    """
    return "tn" + "".join(ch for ch in snapshot_id if ch.isalnum())[:38]


def _discard_snapshot(provisioner, range_id: str, prov_output: dict, name: str) -> None:
    """Best-effort removal of a snapshot no row will stand behind."""
    try:
        result = asyncio.run(provisioner.delete_snapshot(range_id, prov_output, name))
        if result.status != "ok":
            logger.warning(f"[snapshot] Could not discard snapshot {name}: {result.errors}")
    except Exception as e:
        logger.warning(f"[snapshot] Could not discard snapshot {name}: {e}")


def _update_snapshot_state(
    snapshot_id: str,
    new_state: str,
    data: str | None = None,
    size: int | None = None,
    only_from: tuple[str, ...] | None = None,
) -> int:
    """Update snapshot state in the database; return how many rows changed.

    ``only_from`` makes it conditional, as in _update_range_state. The API can
    delete a snapshot while a task holds it, and writing `ready` back over
    `deleted` brought the row back pointing at nothing.
    """
    with _db_session() as db:
        return db_ops.update_snapshot_state(db, snapshot_id, new_state, data=data, size=size, only_from=only_from)


def _snapshot_context(snapshot_id: str, range_id: str):
    """(snapshot_state, snapshot_data, range_state_at_snapshot), (state, provisioner_output, provisioner_backend)."""
    with _db_session() as db:
        return db_ops.snapshot_context(db, snapshot_id, range_id)


def _range_backend(rng) -> str:
    """The range's own backend, as provision and destroy use it; the env is the fallback.

    The snapshot tasks used to read PROVISIONER_BACKEND alone, so a range could be
    snapshotted, restored or deleted through a different backend from its own.
    """
    return (rng[2] if rng and rng[2] else None) or os.getenv("PROVISIONER_BACKEND", "mock")


@app.task(base=ReliableTask, bind=True, name="worker.tasks.snapshot_range")
def snapshot_range(self, range_id: str, snapshot_id: str):
    """Create a point-in-time snapshot of a range."""
    logger.info(f"[snapshot] Creating snapshot {snapshot_id} for range {range_id}")
    name = _backend_snapshot_name(snapshot_id)

    try:
        snap, rng = _snapshot_context(snapshot_id, range_id)
        state = snap[0] if snap else None
        if state == "ready":
            # A duplicate delivery (the broker redelivers when a worker dies with
            # acks_late): an earlier run finished this. Running again would replace it.
            return {"status": "ready", "snapshot_id": snapshot_id}
        if state not in _SNAPSHOT_PENDING:
            logger.warning(f"[snapshot] Snapshot {snapshot_id} is '{state}'; not taking it")
            return {"status": "skipped", "snapshot_id": snapshot_id, "state": state}

        prov_output = json.loads(rng[1]) if rng and rng[1] else {}
        if not prov_output.get("vms"):
            raise RuntimeError("range has no VMs recorded, so there is nothing to snapshot")
        backend = _range_backend(rng)
        provisioner = _get_backend(backend)

        # Clear whatever an earlier attempt of this task left under this name. The row
        # is not `ready`, so nothing depends on it, and a leftover made the create fail
        # with "name already used", after which the discard below removed the earlier
        # attempt's complete snapshot.
        _discard_snapshot(provisioner, range_id, prov_output, name)

        result = run_async(provisioner.snapshot(range_id, prov_output, name))
        if result.status != "ok":
            # Until this check, a failed or partial snapshot was stored as `ready`. A
            # partial one cannot restore the range as a whole, so take back what was
            # taken: a retry then starts clean and nothing is left on the hypervisor.
            _discard_snapshot(provisioner, range_id, prov_output, name)
            raise RuntimeError(f"snapshot {result.status}: {'; '.join(result.errors) or 'no detail'}")

        snapshot_data = json.dumps(
            {
                "provider": backend,
                "range_id": range_id,
                "snapshot_name": name,
                "vm_count": len(prov_output.get("vms", [])),
            }
        )
        size = getattr(result, "size_bytes", 0)

        if not _update_snapshot_state(snapshot_id, "ready", data=snapshot_data, size=size, only_from=_SNAPSHOT_PENDING):
            # Deleted while this ran: no row will ever restore or delete this copy.
            _discard_snapshot(provisioner, range_id, prov_output, name)
            logger.warning(f"[snapshot] Snapshot {snapshot_id} was deleted while being taken; discarded")
            return {"status": "skipped", "snapshot_id": snapshot_id, "state": "deleted"}
        _notify_api("range", {"id": range_id, "snapshot_id": snapshot_id, "state": "snapshot_ready"})
        logger.info(f"[snapshot] Snapshot {snapshot_id} ready for range {range_id}")
        return {"status": "ready", "snapshot_id": snapshot_id}

    except Exception as e:
        _update_snapshot_state(snapshot_id, "failed", only_from=_SNAPSHOT_PENDING)
        _notify_api("range", {"id": range_id, "snapshot_id": snapshot_id, "state": "snapshot_failed", "error": str(e)})
        logger.error(f"[snapshot] Snapshot {snapshot_id} FAILED: {e}")
        raise


@app.task(base=ReliableTask, bind=True, name="worker.tasks.restore_snapshot")
@fenced("restore", None, on_lost=snapshot_back_to_ready)  # the lease only: never beside a build or teardown
def restore_snapshot(self, range_id: str, snapshot_id: str):
    """Restore a range from a snapshot."""
    logger.info(f"[restore] Restoring range {range_id} from snapshot {snapshot_id}")
    changed = False  # whether any VM was reverted, fully or not

    try:
        snap, rng = _snapshot_context(snapshot_id, range_id)
        if not snap or not snap[1]:
            raise RuntimeError(f"Snapshot {snapshot_id} has no data")

        current_state = rng[0] if rng else None
        if current_state not in _RESTORABLE_STATES:
            _update_snapshot_state(snapshot_id, "ready", only_from=("restoring",))
            logger.warning(f"[restore] Range {range_id} is '{current_state}' now; restore abandoned, range untouched")
            return {"status": "skipped", "range_id": range_id, "state": current_state}

        snapshot_data = json.loads(snap[1])
        original_state = snap[2]
        prov_output = json.loads(rng[1]) if rng[1] else {}
        if not prov_output.get("vms"):
            raise RuntimeError("range has no VMs recorded, so there is nothing to restore")
        # Snapshots from before the name was recorded were taken under the bare id.
        name = snapshot_data.get("snapshot_name") or snapshot_id

        provisioner = _get_backend(_range_backend(rng))
        result = run_async(provisioner.restore(range_id, prov_output, name, power_on=original_state != "stopped"))
        changed = result.vms_reverted > 0
        if result.status != "ok":
            raise RuntimeError(f"restore {result.status}: {'; '.join(result.errors) or 'no detail'}")

        _update_range_state(range_id, original_state, only_from=_RESTORABLE_STATES, clear_error=True)
        _update_snapshot_state(snapshot_id, "ready", only_from=("restoring",))  # back to ready after restore
        _notify_api("range", {"id": range_id, "state": original_state, "restored_from": snapshot_id})
        logger.info(f"[restore] Range {range_id} restored to state '{original_state}'")
        return {"status": "restored", "range_id": range_id, "state": original_state}

    except Exception as e:
        if changed or isinstance(e, FINAL_ERRORS):  # cut off by the time limit: the revert may still be running
            # Some VMs reverted and some did not, so the range matches neither its old
            # state nor the snapshot. Guarded, because this runs on every failed attempt,
            # retries included: the old unconditional write stamped `failed` over a
            # range destroyed meanwhile.
            _update_range_state(
                range_id, "failed", error=f"Restore from snapshot failed: {e}", only_from=_RESTORABLE_STATES
            )
            _notify_api("range", {"id": range_id, "state": "failed", "error": str(e)})
        else:
            # Nothing on the hypervisor changed (snapshot missing, vCenter unreachable,
            # a backend without restore), so the range is exactly as it was and keeps
            # its state. Marking it `failed` blocked stop, start and expiry cleanup.
            _notify_api("range", {"id": range_id, "restore_failed": snapshot_id, "error": str(e)})
        if isinstance(e, FINAL_ERRORS) or _last_attempt(self):
            # Until then a retry still owns the snapshot, and `restoring` keeps the API
            # from starting another restore or deleting it underneath the retry.
            _update_snapshot_state(snapshot_id, "ready", only_from=("restoring",))
        logger.error(f"[restore] Range {range_id} restore FAILED: {e}")
        raise


@app.task(base=ReliableTask, bind=True, name="worker.tasks.delete_snapshot")
def delete_snapshot(self, range_id: str, snapshot_id: str):
    """Delete snapshot data from the provisioner backend."""
    logger.info(f"[snapshot] Deleting snapshot {snapshot_id} for range {range_id}")

    try:
        snap, rng = _snapshot_context(snapshot_id, range_id)

        # snapshot_data is written only when a snapshot becomes `ready`. Without it
        # there is nothing on the hypervisor: the snapshot failed and was discarded,
        # and the API refuses to delete one that is still being taken.
        if snap and snap[1]:
            snapshot_data = json.loads(snap[1])
            prov_output = json.loads(rng[1]) if rng and rng[1] else {}
            name = snapshot_data.get("snapshot_name") or snapshot_id
            result = asyncio.run(_get_backend(_range_backend(rng)).delete_snapshot(range_id, prov_output, name))
            if result.status != "ok":
                raise RuntimeError(f"delete {result.status}: {'; '.join(result.errors) or 'no detail'}")

        _update_snapshot_state(snapshot_id, "deleted")
        logger.info(f"[snapshot] Snapshot {snapshot_id} deleted")
        return {"status": "deleted", "snapshot_id": snapshot_id}

    except Exception as e:
        logger.error(f"[snapshot] Delete snapshot {snapshot_id} FAILED: {e}")
        raise


# ── Exercise Forge (EPIC 1) ──────────────────────────────────────────────

AI_ORCHESTRATOR_URL = os.getenv("AI_ORCHESTRATOR_URL", "http://ai-orchestrator:6000")


@app.task(base=ReliableTask, bind=True, name="worker.tasks.forge_exercise")
def forge_exercise(self, request_id: str, tenant_id: str, indicators: list, config: dict):
    """Async exercise forge: calls AI orchestrator and creates DB records.

    Published progress to WebSocket channel ``forge.{request_id}``.
    """
    import re

    import httpx
    import yaml as yaml_lib

    logger.info(f"[forge] Starting exercise forge request={request_id}")
    _notify_api("forge", {"request_id": request_id, "status": "generating", "progress": 10})

    # 1. Call AI orchestrator
    payload = {
        "threat_indicators": indicators,
        "difficulty": config.get("difficulty", "intermediate"),
        "duration_minutes": config.get("duration_minutes", 60),
        "objective_count": config.get("objective_count", 4),
        "range_template": config.get("range_template", "small-enterprise"),
        "focus_areas": config.get("focus_areas", []),
    }

    try:
        with httpx.Client(timeout=120) as client:
            resp = client.post(f"{AI_ORCHESTRATOR_URL}/ai/exercise-forge", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        _notify_api("forge", {"request_id": request_id, "status": "failed", "error": str(e)})
        raise

    raw_output = data.get("output", "")
    model_used = data.get("model_used", "unknown")

    # Strip markdown fences
    cleaned = re.sub(r"^```(?:yaml|yml)?\s*\n", "", raw_output.strip())
    cleaned = re.sub(r"\n```\s*$", "", cleaned).strip()

    _notify_api("forge", {"request_id": request_id, "status": "creating", "progress": 60})

    # 2. Parse and validate YAML
    try:
        parsed = yaml_lib.safe_load(cleaned)
    except yaml_lib.YAMLError as e:
        _notify_api("forge", {"request_id": request_id, "status": "failed", "error": f"Invalid YAML: {e}"})
        raise ValueError(f"AI generated invalid YAML: {e}") from e

    scenario_name = config.get("name_override") or parsed.get("name", f"forged-{request_id[:8]}")

    # 3. Create DB records
    with _db_session() as db:
        scenario_id = db_ops.insert_forged_scenario(db, scenario_name, cleaned, tenant_id)

        # Find a range for this tenant
        row = db_ops.first_range_for_tenant(db, tenant_id)
        if not row:
            _notify_api("forge", {"request_id": request_id, "status": "failed", "error": "No range available"})
            raise ValueError("No range available in tenant")
        range_id = row[0]

        exercise_id = db_ops.insert_forged_exercise(db, scenario_name, range_id, scenario_id, tenant_id)

        # Create forged_exercises tracking record
        db_ops.insert_forged_exercise_record(
            db,
            exercise_id=exercise_id,
            scenario_id=scenario_id,
            feed_id=config.get("feed_id"),
            indicator_ids=[str(i.get("id", "")) for i in indicators if i.get("id")],
            scenario_yaml=cleaned,
            mitre_techniques=sorted(set(re.findall(r"T\d{4}(?:\.\d{3})?", cleaned))),
            difficulty=config.get("difficulty", "intermediate"),
            model_used=model_used,
            tenant_id=tenant_id,
        )

    _notify_api(
        "forge",
        {
            "request_id": request_id,
            "status": "completed",
            "progress": 100,
            "exercise_id": exercise_id,
            "scenario_id": scenario_id,
        },
    )

    logger.info(f"[forge] Exercise forged: exercise={exercise_id} scenario={scenario_id}")
    return {
        "status": "completed",
        "exercise_id": exercise_id,
        "scenario_id": scenario_id,
        "model_used": model_used,
    }


# ── Adaptive Learning (EPIC 3) ──────────────────────────────────────────


@app.task(base=ReliableTask, bind=True, name="worker.tasks.auto_assess_competency")
def auto_assess_competency(self, exercise_id: str, user_id: str):
    """Auto-assess competencies from exercise results.

    Maps exercise objectives to NICE competencies, calculates deltas,
    and creates CompetencyAutoAssessment + updates CompetencyAssertions.
    """
    logger.info(f"[adaptive] Auto-assessing competency for exercise={exercise_id} user={user_id}")

    with _db_session() as db:
        # Gather exercise data
        row = db_ops.exercise_scores_and_yaml(db, exercise_id)

        if not row:
            logger.warning(f"[adaptive] Exercise {exercise_id} not found")
            return {"status": "skipped", "reason": "exercise_not_found"}

        total_score, max_score, scenario_yaml = row
        pct = (total_score / max_score * 100) if max_score > 0 else 0

        # Precise mappings first: objectives that carry an explicit
        # competency_code (Curriculum Forge generates these).
        competency_mappings = []
        mapped_codes: set[str] = set()
        objective_rows = db_ops.objective_competency_rows(db, exercise_id)
        for code, points, achieved, description, comp_id in objective_rows:
            mapped_codes.add(code)
            delta = round((points or 10) / 10, 1) if achieved else round(-(points or 10) / 20, 1)
            competency_mappings.append(
                {
                    "competency_id": str(comp_id) if comp_id else "",
                    "competency_code": code,
                    "competency_name": description or code,
                    "technique": "",
                    "delta": delta,
                    "reason": f"Objective '{description}' {'achieved' if achieved else 'missed'}",
                }
            )
            if comp_id:
                obj_pct = 100 if achieved else 0
                _upsert_competency_assertion(db, user_id, str(comp_id), obj_pct, exercise_id)

        # Extract MITRE techniques from scenario
        import re

        mitre_ids = re.findall(r"T\d{4}(?:\.\d{3})?", scenario_yaml or "")

        # Map MITRE techniques to competencies (coarse fallback)
        for technique in set(mitre_ids):
            # Determine competency category from technique range
            category = _mitre_to_nice_category(technique)
            # Find matching competency
            comp_row = db_ops.nice_competency_in_category(db, category)

            if comp_row and comp_row[1] not in mapped_codes:
                # Calculate delta: positive if good score, negative if poor
                delta = round((pct - 50) / 10, 1)  # -5 to +5 range
                competency_mappings.append(
                    {
                        "competency_id": str(comp_row[0]),
                        "competency_code": comp_row[1],
                        "competency_name": comp_row[2],
                        "technique": technique,
                        "delta": delta,
                        "reason": f"{'Passed' if pct >= 70 else 'Needs improvement on'} {technique} detection/response",
                    }
                )

        # Create auto-assessment record
        assessment_id = db_ops.insert_auto_assessment(
            db, user_id, exercise_id, competency_mappings, total_score, max_score
        )

        # Update competency assertions for each mapped competency. An objective whose code
        # matches no competency has no id to assert against; writing "" as the id raised
        # on Postgres (invalid uuid) and failed the whole assessment.
        for mapping in competency_mappings:
            if mapping["competency_id"]:
                _upsert_competency_assertion(db, user_id, mapping["competency_id"], pct, exercise_id)

    _notify_api(
        "adaptive",
        {
            "user_id": user_id,
            "exercise_id": exercise_id,
            "status": "assessed",
            "mappings_count": len(competency_mappings),
        },
    )

    logger.info(f"[adaptive] Auto-assessed {len(competency_mappings)} competencies for user={user_id}")
    return {
        "status": "assessed",
        "assessment_id": assessment_id,
        "mappings_count": len(competency_mappings),
    }


@app.task(base=ReliableTask, bind=True, name="worker.tasks.generate_learning_recommendation")
def generate_learning_recommendation(self, user_id: str, target_role: str = ""):
    """Generate AI-powered personalized learning recommendations."""
    import httpx

    logger.info(f"[adaptive] Generating learning recommendations for user={user_id}")

    with _db_session() as db:
        # Gather user's competency profile
        assertions = db_ops.competency_profile(db, user_id)

        profile = {
            "assertions": [{"code": r[0], "name": r[1], "category": r[2], "proficiency": r[3]} for r in assertions]
        }

        # Gather recent exercise history
        exercises = db_ops.recent_completed_exercises(db, user_id, limit=10)

        exercise_history = [
            {
                "name": r[0],
                "score": r[1],
                "max_score": r[2],
                "completed_at": str(r[3]) if r[3] else None,
            }
            for r in exercises
        ]

        # Gather available courses
        courses = db_ops.published_courses(db, limit=20)

        available_courses = [
            {"name": r[0], "description": r[1], "difficulty": r[2], "nice_work_roles": r[3]} for r in courses
        ]

    # Call AI orchestrator
    payload = {
        "user_profile": profile,
        "exercise_history": exercise_history,
        "available_courses": available_courses,
        "target_role": target_role,
    }

    try:
        with httpx.Client(timeout=120) as client:
            resp = client.post(f"{AI_ORCHESTRATOR_URL}/ai/learning-recommendation", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.error(f"[adaptive] AI recommendation failed: {e}")
        raise

    raw = data.get("output", "{}")
    model_used = data.get("model_used", "unknown")

    # Parse JSON output (strip fences if present)
    import re

    cleaned = re.sub(r"^```(?:json)?\s*\n", "", raw.strip())
    cleaned = re.sub(r"\n```\s*$", "", cleaned).strip()

    try:
        rec = json.loads(cleaned)
    except json.JSONDecodeError:
        rec = {
            "summary": "Unable to parse AI recommendations",
            "strengths": [],
            "gaps": [],
            "recommendations": [],
            "target_role_readiness": 0.0,
            "next_milestone": "Complete more exercises for assessment",
        }

    # Store recommendation
    with _db_session() as db:
        rec_id = db_ops.insert_learning_recommendation(db, user_id, rec, target_role, model_used)

    _notify_api(
        "adaptive",
        {"user_id": user_id, "status": "recommendation_generated", "recommendation_id": rec_id},
    )

    logger.info(f"[adaptive] Recommendation generated for user={user_id}")
    return {"status": "completed", "recommendation_id": rec_id}


def _mitre_to_nice_category(technique: str) -> str:
    """Map a MITRE ATT&CK technique ID to a NICE framework category."""
    # Simplified mapping based on technique number ranges
    t_num = int(technique[1:5]) if len(technique) >= 5 else 0
    if t_num < 1100:
        return "Protect & Defend"
    elif t_num < 1200:
        return "Analyze"
    elif t_num < 1400:
        return "Collect & Operate"
    elif t_num < 1500:
        return "Investigate"
    elif t_num < 1600:
        return "Operate & Maintain"
    elif t_num < 1700:
        return "Securely Provision"
    else:
        return "Oversee & Govern"


def _upsert_competency_assertion(db, user_id: str, competency_id: str, score_pct: float, exercise_id: str):
    """Update or create a competency assertion based on exercise score."""
    # Determine proficiency level from score
    if score_pct >= 90:
        proficiency = "expert"
    elif score_pct >= 75:
        proficiency = "advanced"
    elif score_pct >= 60:
        proficiency = "intermediate"
    elif score_pct >= 40:
        proficiency = "beginner"
    else:
        proficiency = "novice"

    db_ops.upsert_competency_assertion(db, user_id, competency_id, proficiency, exercise_id)
