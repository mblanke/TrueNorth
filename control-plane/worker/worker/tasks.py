"""TrueNorth Range - Celery tasks for range provisioning and scenario execution.

Designed for 70,000-VM scale:
  - Batch provisioning with chunked VM creation
  - Exponential backoff retries with jitter
  - Proper DB session lifecycle (no leaks)
  - Telemetry batch ingest to OpenSearch
  - Distributed locking via Redis for state transitions
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime

from celery import group

from . import db_ops, periodic
from .aar import build_report as build_aar_report
from .celery_app import app
from .detection import DetectionScorer, range_index
from .fencing import FINAL_ERRORS, PermanentError, SoftTimeLimitExceeded, fenced, run_async
from .periodic import HEALTH_CHECK_BUDGET, METRICS_BUDGET
from .provisioners import get_provisioner
from .range_alloc import reserve_for_build
from .reliable import ReliableTask, _last_attempt
from .render import load_template

logger = logging.getLogger("truenorth.worker")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://forge:forge@localhost:5432/forge")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


# -- Helper: provisioner backend -----------------------------------------
def _get_backend(backend: str | None = None):
    """Return an instantiated provisioner for the given backend name.

    Falls back to the ``PROVISIONER_BACKEND`` environment variable, then
    to ``"mock"`` when neither the caller nor the env provides a value.
    """
    resolved = backend or os.getenv("PROVISIONER_BACKEND", "mock")
    provisioner = get_provisioner(resolved)
    # vSphere without VSPHERE_URL in the environment: use the primary vSphere
    # connection registered through the API (Hypervisors page) instead.
    if resolved == "vsphere_api" and not os.getenv("VSPHERE_URL"):
        try:
            with _db_session() as db:
                creds = _hypervisor_creds(db, "vsphere")
        except Exception as e:  # noqa: BLE001 — the provisioner then reports the missing endpoint
            logger.warning(f"Could not read the vSphere connection from the database: {e}")
            creds = {}
        if creds:
            provisioner.use_credentials(creds)
    return provisioner


# -- DB session management (one per task, no leaks) ---------------------
@contextmanager
def _db_session():
    """Create a scoped DB session for a single task."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker
    from sqlalchemy.pool import NullPool

    eng = create_engine(DATABASE_URL, poolclass=NullPool, echo=False)
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
        return db_ops.update_range_state(
            db, range_id, new_state, error=error, output=output, only_from=only_from, clear_error=clear_error
        )


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
        "host": row[0],
        "port": row[1],
        "username": row[2],
        "password": row[3] or "",
        "api_token": row[4] or "",
        "verify_ssl": bool(row[5]),
        "datacenter": row[6] or "",
    }


@app.task(base=ReliableTask, bind=True, name="worker.tasks.provision_range")
@fenced("provision", "provisioning")
def provision_range(self, range_id: str, noise_mgmt: dict | None = None):
    """Provision a single range using the configured backend.

    Fetches the range template from the database, delegates to the
    provisioner class hierarchy via fencing.run_async(), and stores the
    structured ProvisionResult back to the database.
    """
    logger.info(f"[provision] Starting range {range_id}")

    try:
        # Fetch template, allocations, and provisioner_backend from DB
        with _db_session() as db:
            row = db_ops.range_template_and_backend(db, range_id)

        template = load_template(row[0] if row else None)
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
                len(rendered["vm_definitions"]),
                len(rendered["network_definitions"]),
                backend,
            )
            if rendered["unresolved"]:
                logger.warning("[provision] unresolved OS templates: %s", rendered["unresolved"])

        provisioner = _get_backend(backend)
        allocations.update(reserve_for_build(_db_session, range_id, provisioner, template))  # network_reservations

        result = run_async(provisioner.provision(range_id, template, allocations))

        if result.status == "failed":
            raise RuntimeError("; ".join(result.errors) or "Provisioning failed")

        stored = {
            "provider": backend,
            "range_id": range_id,
            "vms": result.vms,
            "networks": result.networks,
        }
        # vSphere: the edge firewall's WAN address (kept until destroy, like the VLANs),
        # and what was skipped on purpose (unknown software, no depot path).
        if getattr(result, "uplink", None):
            stored["uplink"] = result.uplink
        if getattr(result, "mirrors", None):  # vSphere port-mirroring sessions; destroy removes them
            stored["mirrors"] = result.mirrors
        if getattr(result, "warnings", None):
            stored["warnings"] = result.warnings
            for w in result.warnings:
                logger.warning(f"[provision] Range {range_id}: {w}")
        if result.status == "partial":
            stored["errors"] = result.errors
            logger.warning(f"[provision] Range {range_id} partial: {'; '.join(result.errors)}")
        output = json.dumps(stored)

        _update_range_state(range_id, "ready", output=output, only_from=("provisioning",))
        _notify_api("range", {"id": range_id, "state": "ready"})
        logger.info(f"[provision] Range {range_id} ready ({len(result.vms)} VMs)")
        return {"status": "ready", "range_id": range_id, "vm_count": len(result.vms)}

    except Exception as e:
        if isinstance(e, FINAL_ERRORS) or _last_attempt(self):  # a retry must still find it provisioning
            _update_range_state(range_id, "failed", error=str(e), only_from=("provisioning",))
            _notify_api("range", {"id": range_id, "state": "failed", "error": str(e)})
        logger.error(f"[provision] Range {range_id} FAILED: {e}")
        raise


@app.task(bind=True, name="worker.tasks.batch_provision")
def batch_provision(self, range_ids: list[str]):
    """Provision multiple ranges in parallel using a Celery group.

    At 70k-VM scale, ranges are queued with rate limiting.
    The group dispatches individual provision tasks.
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
@fenced("destroy", "destroying")
def destroy_range(self, range_id: str):
    """Destroy a provisioned range using the configured backend.

    Fetches the provisioner output from the database, delegates to
    the provisioner class hierarchy, and updates range state.
    """
    logger.info(f"[destroy] Starting range {range_id}")

    try:
        from .range_rows import provisioner_output

        prov_output, backend = provisioner_output(range_id)
        provisioner = _get_backend(backend)

        result = run_async(provisioner.destroy(range_id, prov_output))

        if result.status == "failed":
            raise RuntimeError("; ".join(result.errors) or "Destroy failed")

        with _db_session() as db:  # the range's VLANs and addresses go back to their pools with it
            if db_ops.update_range_state(db, range_id, "destroyed", only_from=("destroying",)):
                db_ops.release_reservations(db, range_id)
        _notify_api("range", {"id": range_id, "state": "destroyed"})
        logger.info(f"[destroy] Range {range_id} destroyed ({result.resources_removed} resources)")
        return {"status": "destroyed", "range_id": range_id}

    except Exception as e:
        if isinstance(e, FINAL_ERRORS) or _last_attempt(self):  # a retry must still find the range in destroying
            _update_range_state(range_id, "failed", error=str(e), only_from=("destroying",))
            _notify_api("range", {"id": range_id, "state": "failed", "error": str(e)})
        logger.error(f"[destroy] Range {range_id} FAILED: {e}")
        raise


def _power_range(task, range_id: str, action: str, claim: str, done: str):
    """Power a range's VMs off (``stop``) or on (``start``) for the operation the API recorded
    by moving it to ``claim`` (stop_range/start_range are ``@fenced`` on it), write ``done``
    only once the hypervisor did it; a retry is safe, and only the last attempt records failed."""
    logger.info(f"[{action}] Range {range_id}")
    try:
        with _db_session() as db:
            row = db_ops.range_output_and_backend(db, range_id)
        prov_output = json.loads(row[0]) if row and row[0] else {}
        if not prov_output.get("vms"):  # a build that failed with nothing built is not "running"
            raise PermanentError(f"{action}: the range has no VMs recorded; nothing to power")
        provisioner = _get_backend((row[1] if row and row[1] else None) or os.getenv("PROVISIONER_BACKEND", "mock"))
        result = run_async(getattr(provisioner, action)(range_id, prov_output))
        if result.status != "ok":
            raise RuntimeError(f"{action} {result.status}: {'; '.join(result.errors) or 'no detail'}")
        _update_range_state(range_id, done, only_from=(claim,), clear_error=True)
        _notify_api("range", {"id": range_id, "state": done})
        return {"status": done, "range_id": range_id}
    except Exception as e:
        if isinstance(e, FINAL_ERRORS) or _last_attempt(task):  # a retry must still find it in `claim`
            _update_range_state(range_id, "failed", error=str(e), only_from=(claim,))
            _notify_api("range", {"id": range_id, "state": "failed", "error": str(e)})
        logger.error(f"[{action}] Range {range_id} FAILED: {e}")
        raise


@app.task(base=ReliableTask, bind=True, name="worker.tasks.stop_range")
@fenced("stop", "stopping")
def stop_range(self, range_id: str):
    """Power off every VM of a range (a stop operation: POST /ranges/{id}/stop)."""
    return _power_range(self, range_id, "stop", "stopping", "stopped")


@app.task(base=ReliableTask, bind=True, name="worker.tasks.start_range")
@fenced("start", "starting")
def start_range(self, range_id: str):
    """Power on every VM of a range (a start operation: POST /ranges/{id}/start)."""
    return _power_range(self, range_id, "start", "starting", "running")


# -- Scenario Execution --------------------------------------------------
@app.task(bind=True, name="worker.tasks.run_scenario")
def run_scenario(self, exercise_id: str):
    """Execute a scenario timeline for an exercise."""
    logger.info(f"[scenario] Starting exercise {exercise_id}")

    with _db_session() as db:
        row = db_ops.exercise_scenario_yaml(db, exercise_id)

        if not row:
            logger.error(f"[scenario] Exercise {exercise_id} not found")
            return {"status": "error", "detail": "Exercise not found"}

    import yaml

    scenario_data = yaml.safe_load(row[1])
    timeline = scenario_data.get("timeline", [])
    scenario_data.get("inject_packs", [])

    executed = 0
    for event in timeline:
        t = event.get("t", "0:00")
        action = event.get("action", "unknown")
        params = event.get("params", {})

        parts = t.split(":")
        offset_seconds = int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else 0

        logger.info(f"[scenario] [{t}] inject={action} params={params}")

        # In production: dispatch to injector registry
        # _dispatch_inject(action, params, range_context)

        # Compressed time for dev/test
        time.sleep(min(offset_seconds * 0.01, 2))
        executed += 1

    logger.info(f"[scenario] Exercise {exercise_id} complete ({executed} events)")
    return {"status": "completed", "exercise_id": exercise_id, "events_executed": executed}


# -- Telemetry Batch Ingest ----------------------------------------------
@app.task(bind=True, name="worker.tasks.ingest_telemetry_batch")
def ingest_telemetry_batch(self, range_id: str, events: list[dict]):
    """Batch-ingest telemetry events into OpenSearch.

    At scale, the API buffers events and dispatches to this task
    to avoid blocking request threads on OpenSearch I/O.
    """
    import httpx

    os_url = os.getenv("OPENSEARCH_URL", "http://opensearch:9200")
    index = range_index(range_id)

    bulk_body = ""
    for event in events:
        event.setdefault("range_id", range_id)
        event.setdefault("@timestamp", datetime.now(UTC).isoformat())
        bulk_body += json.dumps({"index": {"_index": index}}) + "\n"
        bulk_body += json.dumps(event) + "\n"

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{os_url}/_bulk",
                content=bulk_body,
                headers={"Content-Type": "application/x-ndjson"},
            )
            resp.raise_for_status()
            result = resp.json()
            errors = result.get("errors", False)
            if errors:
                failed = [item for item in result.get("items", []) if item.get("index", {}).get("error")]
                logger.warning(f"[telemetry] {len(failed)} events failed indexing")
    except Exception as e:
        logger.error(f"[telemetry] OpenSearch ingest error: {e}")
        raise

    logger.info(f"[telemetry] Ingested {len(events)} events for range {range_id}")
    return {"indexed": len(events), "range_id": range_id}


# ========================================================================
# Additional tasks â€” scenario execution, snapshots, periodic maintenance
# ========================================================================


# -- Scenario Execution (enhanced) ----------------------------------------
@app.task(base=ReliableTask, bind=True, name="worker.tasks.run_scenario_v2")
def run_scenario_v2(self, exercise_id: str, scenario_definition: dict):
    """Execute a scenario against a range using a structured definition.

    Parses the timeline from scenario_definition, executes each timeline
    event via the injector registry, updates exercise state as phases
    progress, tracks objective completion, and sends progress via Redis
    pub/sub.
    """
    logger.info(f"[scenario_v2] Starting exercise {exercise_id}")
    _notify_api("exercise", {"id": exercise_id, "state": "running", "phase": "starting"})

    backend = os.getenv("PROVISIONER_BACKEND", "mock")

    try:
        timeline = scenario_definition.get("timeline", [])
        objectives = scenario_definition.get("objectives", [])
        detections = None if backend == "mock" else DetectionScorer(exercise_id, _db_session)

        # â”€â”€ Update exercise state to running â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        with _db_session() as db:
            db_ops.start_exercise(db, exercise_id)

        # â”€â”€ Execute timeline events â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        executed = 0
        total_events = len(timeline)

        for idx, event in enumerate(timeline):
            t = event.get("t", "0:00")
            action = event.get("action", "noop")
            params = event.get("params", {})

            parts = t.split(":")
            offset_seconds = int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else 0

            logger.info(f"[scenario_v2] [{t}] event {idx + 1}/{total_events} action={action} params={params}")

            # Production: dispatch to injector registry (_dispatch_inject(action, params, range_context))
            time.sleep(min(offset_seconds * 0.01, 1 if backend == "mock" else 2))
            if detections:  # score the telemetry so far, so the scoreboard moves mid-run
                detections.score()
            executed += 1

            # Publish progress
            progress = int((executed / max(total_events, 1)) * 100)
            _notify_api(
                "exercise",
                {
                    "id": exercise_id,
                    "state": "running",
                    "progress": progress,
                    "current_event": action,
                },
            )

        # â”€â”€ Track objective completion â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if detections:  # final pass against the range's telemetry (worker/detection.py)
            completed_objectives = detections.score()
        else:  # mock: auto-complete every objective
            with _db_session() as db:
                for obj in objectives:
                    db_ops.achieve_objective(db, exercise_id, obj.get("ref_id", ""))
            completed_objectives = len(objectives)

        # â”€â”€ Mark exercise complete â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        with _db_session() as db:
            db_ops.complete_exercise(db, exercise_id)

        _notify_api(
            "exercise",
            {
                "id": exercise_id,
                "state": "completed",
                "events_executed": executed,
                "objectives_completed": completed_objectives,
            },
        )

        logger.info(
            f"[scenario_v2] Exercise {exercise_id} completed ({executed} events, {completed_objectives} objectives)"
        )
        return {
            "status": "completed",
            "exercise_id": exercise_id,
            "events_executed": executed,
            "objectives_completed": completed_objectives,
        }

    except Exception as e:
        with _db_session() as db:
            db_ops.cancel_exercise(db, exercise_id)  # exercises has no error_message column
        _notify_api("exercise", {"id": exercise_id, "state": "failed", "error": str(e)})
        logger.error(f"[scenario_v2] Exercise {exercise_id} FAILED: {e}")
        raise


# -- AAR Generation -------------------------------------------------------
@app.task(base=ReliableTask, bind=True, name="worker.tasks.generate_aar")
def generate_aar(self, exercise_id: str):
    """Generate After-Action Review for a completed exercise.

    Gathers exercise data, objective scores, timeline events, and telemetry.
    Optionally calls the AI orchestrator for analysis.
    Builds an AAR report JSON, stores it, and notifies via WebSocket.
    """
    logger.info(f"[aar] Generating AAR for exercise {exercise_id}")
    _notify_api("exercise", {"id": exercise_id, "event": "aar_generating"})

    try:
        # â”€â”€ Gather exercise data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        with _db_session() as db:
            exercise_row = db_ops.exercise_for_aar(db, exercise_id)

            if not exercise_row:
                raise ValueError(f"Exercise {exercise_id} not found")

            objectives = db_ops.objectives_for_aar(db, exercise_id)

        # â”€â”€ Build report â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        aar_report, report_html = build_aar_report(exercise_id, exercise_row, objectives)

        # â”€â”€ Optional AI analysis (if orchestrator available) â”€â”€â”€â”€â”€â”€â”€â”€â”€
        ai_url = os.getenv("AI_ORCHESTRATOR_URL")
        if ai_url:
            try:
                import httpx

                with httpx.Client(timeout=30) as client:
                    resp = client.post(
                        f"{ai_url}/analyze-aar",
                        json=aar_report,
                    )
                    if resp.status_code == 200:
                        aar_report["ai_analysis"] = resp.json()
            except Exception as ai_err:
                logger.warning(f"[aar] AI analysis unavailable: {ai_err}")
                aar_report["ai_analysis"] = None

        # â”€â”€ Store AAR â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        report_json = json.dumps(aar_report)
        with _db_session() as db:
            # Was `INSERT INTO aars`, a table that does not exist. Never replaces an existing
            # (API-generated, possibly AI-enhanced) report; returns whichever row is stored.
            aar_id = db_ops.upsert_aar(db, exercise_id, report_json, report_html)

        _notify_api(
            "exercise",
            {
                "id": exercise_id,
                "event": "aar_ready",
                "aar_id": aar_id,
            },
        )

        logger.info(f"[aar] AAR generated for exercise {exercise_id} (score: {aar_report['summary']['score_pct']}%)")
        return {"status": "generated", "exercise_id": exercise_id, "aar_id": aar_id}

    except Exception as e:
        _notify_api(
            "exercise",
            {
                "id": exercise_id,
                "event": "aar_failed",
                "error": str(e),
            },
        )
        logger.error(f"[aar] AAR generation FAILED for {exercise_id}: {e}")
        raise


# -- Periodic: Cleanup Expired Ranges -------------------------------------
@app.task(bind=True, name="worker.tasks.cleanup_expired_ranges")
def cleanup_expired_ranges(self):
    """Periodic task: destroy ranges past their expiry time.

    Queries ranges where expires_at < now() and state == 'ready',
    dispatches destroy tasks for each, and logs summary.
    """
    logger.info("[cleanup] Checking for expired ranges")
    if not db_ops.range_expiry_supported():
        # Say so rather than log "No expired ranges found" forever and look healthy.
        logger.warning("[cleanup] Range expiry is not implemented: ranges have no expires_at column")
        return {"status": "unsupported", "expired_count": 0}

    try:
        with _db_session() as db:
            expired = db_ops.expired_ranges(db)  # always empty: ranges have no expires_at column yet

        if not expired:
            logger.info("[cleanup] No expired ranges found")
            return {"status": "ok", "expired_count": 0}

        dispatched = 0
        for row in expired:
            range_id, name = str(row[0]), row[1]
            logger.info(f"[cleanup] Dispatching destroy for expired range {name} ({range_id})")
            destroy_range.delay(range_id)
            dispatched += 1

        _notify_api(
            "system",
            {
                "event": "cleanup_expired",
                "dispatched": dispatched,
            },
        )

        logger.info(f"[cleanup] Dispatched destroy for {dispatched} expired ranges")
        return {"status": "ok", "expired_count": dispatched}

    except Exception as e:
        logger.error(f"[cleanup] Error checking expired ranges: {e}")
        raise


# -- Periodic: health and metrics over the active ranges (worker/periodic.py) ----
# -- Periodic: Health Check Ranges ----------------------------------------
@app.task(
    bind=True,
    name="worker.tasks.health_check_ranges",
    soft_time_limit=HEALTH_CHECK_BUDGET + 10,
    time_limit=HEALTH_CHECK_BUDGET + 15,
)
def health_check_ranges(self):
    """Periodic task: check health of all active ranges.

    Each ready or running range is checked by its own backend's health_check. A range
    found unhealthy gets a ``health_check_failed`` notification; any unhealthy range
    raises one ``health_check_alert``. A range whose check raised counts as unhealthy.
    """
    logger.info("[health] Starting health check for active ranges")

    with periodic.run_lock("health_check_ranges", HEALTH_CHECK_BUDGET + 20) as mine:
        if not mine:
            logger.info("[health] The previous run is still going; skipping this one")
            return {"status": "skipped", "reason": "previous run still in progress"}
        return periodic.health_check_run(time.monotonic() + HEALTH_CHECK_BUDGET)


@app.task(
    bind=True,
    name="worker.tasks.collect_range_metrics",
    soft_time_limit=METRICS_BUDGET + 5,
    time_limit=METRICS_BUDGET + 8,
)
def collect_range_metrics(self):
    """Periodic task: resource usage of every active range, from its own backend.

    Per range, one ``range_metrics`` event goes to OpenSearch (ingest_telemetry_batch),
    including when the read failed (metrics_status ``failed``, values null), so a dead
    vCenter is visible. A backend without metrics (``unsupported``) sends nothing: there
    is nothing to report. A range read successfully has its updated_at bumped, as before.
    """
    logger.info("[metrics] Collecting range metrics")

    with periodic.run_lock("collect_range_metrics", METRICS_BUDGET + 10) as mine:
        if not mine:
            logger.info("[metrics] The previous run is still going; skipping this one")
            return {"status": "skipped", "reason": "previous run still in progress"}
        return periodic.collect_metrics_run(time.monotonic() + METRICS_BUDGET)


# -- Range Snapshots ----------------------------------------------------
# States routers/ranges.py:restore_snapshot accepts. A restore only ever writes to a
# range still in one of them; if the range moved on (say, it was destroyed while the
# task queued or retried), the range is left alone.
_RESTORABLE_STATES = ("ready", "stopped", "failed")
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
        result = run_async(provisioner.delete_snapshot(range_id, prov_output, name))
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
@fenced("restore", None)  # the range's lease only: one restore (or other range task) at a time
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
        result = run_async(provisioner.restore(range_id, prov_output, name, power_on=original_state == "ready"))
        changed = result.vms_reverted > 0
        if result.status != "ok":
            raise RuntimeError(f"restore {result.status}: {'; '.join(result.errors) or 'no detail'}")

        _update_range_state(range_id, original_state, only_from=_RESTORABLE_STATES, clear_error=True)
        _update_snapshot_state(snapshot_id, "ready", only_from=("restoring",))  # back to ready after restore
        _notify_api("range", {"id": range_id, "state": original_state, "restored_from": snapshot_id})
        logger.info(f"[restore] Range {range_id} restored to state '{original_state}'")
        return {"status": "restored", "range_id": range_id, "state": original_state}

    except Exception as e:
        if changed or isinstance(e, SoftTimeLimitExceeded):
            # Some VMs reverted and some did not (or, cut off by the soft limit, we cannot
            # tell, and the call may still be reverting), so the range matches neither its old
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
            result = run_async(_get_backend(_range_backend(rng)).delete_snapshot(range_id, prov_output, name))
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
