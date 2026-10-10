"""TrueNorth Range - the one seam between a scenario run and scenario injection.

``dispatch_inject(exercise_id, action, params)`` delivers one inject to an exercise's
range; ``dispatch_for_execution`` does the same for a scenario execution (no exercise). Both:

1. build a ``RangeContext`` from the range (``provisioner_output`` -> vms / networks);
2. call ``scenario_engine.injectors.run_inject`` (never raises; returns ``InjectResult``).
   On the mock backend the range has no hosts, so only injectors that do not touch range
   hosts run; the rest come back ``skipped`` (docs/scenario-inject-execution.md);
3. ship ``result.telemetry`` through the existing ingest path: the
   ``ingest_telemetry_batch`` task on the telemetry queue, sent by name per the task
   contract (contracts.py), not by calling it;
4. record the outcome in ``inject_records`` and push it over ``notify_api``;
5. return a summary dict. Nothing here raises for a bad inject: an unknown action, bad
   params, an injector exception, a missing range or a missing scenario-engine are
   recorded as ``failed`` and returned.

The OpenSearch endpoint is read only by the ingest task (MOSA: one reader of
OPENSEARCH_URL outside the adapters). No injector reads ``RangeContext.opensearch_url``,
so the context keeps the engine default.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from . import db_ops
from .contracts import TASKS, validate_args
from .task_plumbing import db_session, notify_api

logger = logging.getLogger("truenorth.worker.inject")

MOCK_BACKEND = "mock"
INGEST_TASK = "ingest_telemetry_batch"


class EngineUnavailableError(RuntimeError):
    """The scenario-engine package cannot be imported in this worker."""


def _engine():
    """``scenario_engine.injectors``, imported lazily (it auto-discovers its modules).

    The worker image copies the package in (Dockerfile, ``--build-context
    scenario_engine=scenario-engine``), as detection scoring does; pytest has it on its
    pythonpath. A worker built without it records every inject failed and says why.
    """
    try:
        from scenario_engine import injectors  # noqa: PLC0415 — deliberate lazy import
    except ImportError as exc:
        raise EngineUnavailableError(f"scenario-engine not importable in this worker ({exc})") from exc
    return injectors


def _ship_telemetry(range_id: str, events: list[dict]) -> bool:
    """Send events to the ingest task. False (and logged) when the broker refuses."""
    if not events:
        return False
    contract = TASKS[INGEST_TASK]
    args = [range_id, events]
    try:
        validate_args(INGEST_TASK, args)
        from .celery_app import app

        app.send_task(contract.qualified_name, args=args, queue=contract.queue)
    except Exception as exc:  # noqa: BLE001 — telemetry loss is recorded, not fatal
        logger.warning("[inject] telemetry for range %s not sent: %s", range_id, exc)
        return False
    return True


def range_hosts(provisioner_output: str | None) -> tuple[list[dict], list[dict]]:
    """(vms, networks) from a range's provisioner_output JSON; empty when unprovisioned or unreadable."""
    if not provisioner_output:
        return [], []
    try:
        out = json.loads(provisioner_output)
    except (TypeError, ValueError):
        return [], []
    if not isinstance(out, dict):
        return [], []
    vms = [v for v in out.get("vms") or [] if isinstance(v, dict)]
    nets = [n for n in out.get("networks") or [] if isinstance(n, dict)]
    return vms, nets


def _resolve_backend(range_backend: str | None) -> str:
    return (range_backend or os.getenv("PROVISIONER_BACKEND", MOCK_BACKEND) or MOCK_BACKEND).strip()


def _run(
    *,
    action: str,
    params: dict,
    range_id: str | None,
    tenant_id: str | None,
    exercise_id: str | None,
    execution_id: str | None,
    source: str,
    seq: int | None,
    t: str | None,
    run_id: str | None,
    range_row,
) -> dict[str, Any]:
    """Run one inject against a range row already read, record it, and summarise it."""
    summary: dict[str, Any] = {
        "dispatched": False,
        "action": action,
        "status": "failed",
        "detail": "",
        "skipped": False,
        "execution_mode": None,
        "mitre_technique": None,
        "telemetry_count": 0,
        "telemetry_shipped": False,
        "backend": None,
        "exercise_id": exercise_id,
        "execution_id": execution_id,
        "range_id": range_id,
    }
    if not isinstance(params, dict):
        params = {}

    if range_row is None:
        summary["detail"] = f"range {range_id} not found" if range_id else "no range to inject into"
    else:
        output, backend = range_row
        backend = _resolve_backend(backend)
        summary["backend"] = backend
        vms, networks = range_hosts(output)
        try:
            eng = _engine()
        except EngineUnavailableError as exc:
            summary["detail"] = str(exc)
        else:
            ctx = eng.RangeContext(
                range_id=str(range_id),
                tenant_id=str(tenant_id or ""),
                vms=vms,
                networks=networks,
                exercise_id=exercise_id or execution_id,
            )
            # Host-touching injectors need hosts: none on the mock backend, none on an
            # unprovisioned range.
            allow = backend != MOCK_BACKEND and bool(vms)
            result = eng.run_inject(action, params, ctx, allow_host_effects=allow)
            if result.skipped and backend != MOCK_BACKEND:
                result.detail = "skipped: range has no provisioned hosts"
            # Greyspace breadcrumbs: the injector prepared them; the range's Greyspace host
            # gets them through the worker's Greyspace seam (ADR 0007).
            gs_op = (result.raw or {}).get("greyspace") if result.success and not result.skipped else None
            if isinstance(gs_op, dict) and range_id:
                from . import greyspace

                ok, what = greyspace.deliver_breadcrumbs(str(range_id), backend, gs_op)
                result.success = ok
                result.detail = f"{result.detail} ({what})" if ok else f"{result.detail}: not delivered: {what}"
            summary.update(
                dispatched=not result.skipped and result.raw is not None,
                status="skipped" if result.skipped else ("fired" if result.success else "failed"),
                detail=result.detail,
                skipped=result.skipped,
                execution_mode=result.execution_mode,
                mitre_technique=(str(result.mitre_technique)[:32] if result.mitre_technique else None),
                telemetry_count=len(result.telemetry or []),
            )
            if result.telemetry and range_id:
                summary["telemetry_shipped"] = _ship_telemetry(str(range_id), list(result.telemetry))

    with db_session() as db:
        summary["record_id"] = db_ops.record_inject(
            db,
            exercise_id=exercise_id,
            execution_id=execution_id,
            range_id=range_id,
            tenant_id=tenant_id,
            run_id=run_id,
            source=source,
            seq=seq,
            t=t,
            action=str(action)[:100],
            status=summary["status"],
            detail=summary["detail"],
            execution_mode=summary["execution_mode"],
            mitre_technique=summary["mitre_technique"],
            telemetry_count=summary["telemetry_count"],
            telemetry_shipped=summary["telemetry_shipped"],
        )
    channel_id = exercise_id or execution_id
    notify_api(
        "exercise" if exercise_id else "scenario_execution",
        {"id": channel_id, "event": "inject", "action": action, "status": summary["status"], "seq": seq, "t": t},
    )
    logger.info("[inject] %s %s -> %s (%s)", channel_id, action, summary["status"], summary["detail"])
    return summary


def dispatch_inject(
    exercise_id: str,
    action: str,
    params: dict,
    *,
    source: str = "timeline",
    seq: int | None = None,
    t: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Deliver one inject to an exercise's range; record and return the outcome. Never raises
    for a bad inject (unknown action, bad params, injector error, missing exercise or range)."""
    with db_session() as db:
        ex = db_ops.exercise_context(db, exercise_id)
        range_id = str(ex[0]) if ex and ex[0] else None
        range_row = db_ops.range_output_and_backend(db, range_id) if range_id else None
    if ex is None:
        logger.warning("[inject] exercise %s not found; %s not dispatched", exercise_id, action)
        return {
            "dispatched": False,
            "action": action,
            "status": "failed",
            "detail": "exercise not found",
            "exercise_id": exercise_id,
            "skipped": False,
        }
    tenant_id = str(ex[1]) if ex[1] else None
    return _run(
        action=action,
        params=params,
        range_id=range_id,
        tenant_id=tenant_id,
        exercise_id=exercise_id,
        execution_id=None,
        source=source,
        seq=seq,
        t=t,
        run_id=run_id,
        range_row=range_row,
    )


def dispatch_for_execution(
    execution_id: str, action: str, params: dict, *, seq: int | None, t: str | None, run_id: str | None = None
) -> dict:
    """``dispatch_inject`` for a scenario execution (no exercise)."""
    with db_session() as db:
        x = db_ops.execution_context(db, execution_id)
        range_id = str(x[0]) if x and x[0] else None
        range_row = db_ops.range_output_and_backend(db, range_id) if range_id else None
    tenant_id = str(x[1]) if x and x[1] else None
    return _run(
        action=action,
        params=params,
        range_id=range_id,
        tenant_id=tenant_id,
        exercise_id=None,
        execution_id=execution_id,
        source="timeline",
        seq=seq,
        t=t,
        run_id=run_id,
        range_row=range_row,
    )
