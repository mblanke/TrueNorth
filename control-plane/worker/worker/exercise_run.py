"""TrueNorth Range - the scenario run loop: run_scenario, run_scenario_v2, run_inject,
run_scenario_execution.

Moved out of tasks.py (ADR 0003). The Celery names are unchanged
(``worker.tasks.run_scenario``, ``worker.tasks.run_scenario_v2``; the contract is
contracts.py), and ``from worker.tasks import run_scenario_v2`` still works.

Injection goes through one seam, ``inject_dispatch.dispatch_inject``, called once per
timeline event on every backend; every outcome is recorded in ``inject_records``
(docs/scenario-inject-execution.md). The worker never scores detections (ADR 0005): a
Student earns detection credit by submitting a detection to the API. On a real backend the
exercise stays running after its timeline, until an instructor completes it or its duration
runs out (app/exercise_completion.py); the mock backend, a simulation with no telemetry,
still auto-achieves every objective and completes. Database access is db_ops.py; the DB
session and notifications come from task_plumbing.py.
"""

from __future__ import annotations

import logging
import os
import time
import uuid

from . import db_ops, inject_dispatch
from .base_tasks import ReliableTask
from .celery_app import app
from .fencing import FINAL_ERRORS, last_attempt
from .task_plumbing import db_session as _db_session
from .task_plumbing import notify_api as _notify_api

logger = logging.getLogger("truenorth.worker")


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

    try:
        scenario_data = yaml.safe_load(row[1] or "") or {}
    except yaml.YAMLError as exc:
        return {"status": "error", "detail": f"Scenario YAML does not parse: {exc}"}
    timeline = scenario_data.get("timeline") if isinstance(scenario_data, dict) else None

    run_id = _run_id(self)
    executed = 0
    for idx, event in enumerate(timeline if isinstance(timeline, list) else []):
        _fire_event(exercise_id, idx, event, run_id=run_id)
        executed += 1

    logger.info(f"[scenario] Exercise {exercise_id} complete ({executed} events)")
    return {"status": "completed", "exercise_id": exercise_id, "events_executed": executed}


# -- Timeline helpers ------------------------------------------------------
def _offset_seconds(t) -> int | None:
    """Seconds from "m:ss" (or "h:mm:ss"); None when the offset is malformed."""
    try:
        parts = [int(p) for p in str(t).split(":")]
    except ValueError:
        return None
    if not parts or any(p < 0 for p in parts) or len(parts) > 3:
        return None
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _run_id(task) -> str:
    """The Celery task id (stable across this run's retries), or a fresh id when called directly."""
    rid = getattr(getattr(task, "request", None), "id", None)
    return str(rid) if rid else f"direct-{uuid.uuid4()}"


def _parse_event(event) -> tuple[str, dict, str, int | None, str | None]:
    """(action, params, t, offset seconds, problem) of a timeline event; problem is None when valid."""
    if not isinstance(event, dict):
        return "invalid", {}, "", None, f"timeline event is {type(event).__name__}, not a mapping"
    action = event.get("action")
    t = str(event.get("t", "0:00"))
    params = event.get("params") or {}
    offset = _offset_seconds(t)
    if not isinstance(action, str) or not action.strip():
        return "invalid", {}, t, offset, "timeline event has no action"
    if not isinstance(params, dict):
        return action, {}, t, offset, "timeline event params must be a mapping"
    if offset is None:
        return action, params, t, None, f"timeline offset {t!r} is not m:ss"
    return action, params, t, offset, None


def _fire_event(exercise_id: str, idx: int, event, *, run_id: str) -> dict:
    """Parse one timeline event and dispatch it; a malformed event is recorded as failed, not run."""
    action, params, t, offset, problem = _parse_event(event)
    if problem:
        logger.warning(f"[scenario] {exercise_id} event {idx}: {problem}")
        with _db_session() as db:
            db_ops.record_inject(
                db,
                exercise_id=exercise_id,
                run_id=run_id,
                seq=idx,
                t=t,
                action=action[:100],
                status="failed",
                detail=f"invalid event: {problem}",
            )
        return {"status": "failed", "action": action, "detail": problem, "dispatched": False}
    logger.info(f"[scenario] [{t}] event {idx} inject={action}")
    result = inject_dispatch.dispatch_inject(exercise_id, action, params, seq=idx, t=t, run_id=run_id)
    time.sleep(min((offset or 0) * 0.01, 1))  # compressed time for dev/test
    return result


# -- Scenario Execution (enhanced) ----------------------------------------
@app.task(base=ReliableTask, bind=True, name="worker.tasks.run_scenario_v2")
def run_scenario_v2(self, exercise_id: str, scenario_definition: dict):
    """Run an exercise's timeline: one inject per event, through ``inject_dispatch``.

    Every event is dispatched, on every backend; on the mock backend the dispatch runs only
    injectors that do not need range hosts and records the rest as skipped. State changes
    are conditional: the run starts only a pending/running exercise, stops firing as soon
    as the exercise is no longer running (paused, completed or cancelled by an
    instructor), and completes or cancels it only if it is still running, so a late retry
    cannot overwrite a newer state. A retry skips the events this run already recorded.

    After the timeline (ADR 0005 §6): on a real backend the exercise stays running, for
    Students to submit detections, and nothing is achieved here; on the mock backend every
    objective is achieved and the exercise completed. The backend is the range's own, as for
    inject dispatch, not the worker's environment.
    """
    logger.info(f"[scenario_v2] Starting exercise {exercise_id}")
    run_id = _run_id(self)
    definition = scenario_definition if isinstance(scenario_definition, dict) else {}
    timeline = definition.get("timeline") if isinstance(definition.get("timeline"), list) else []
    objectives = definition.get("objectives") if isinstance(definition.get("objectives"), list) else []

    with _db_session() as db:
        started = db_ops.start_exercise(db, exercise_id)
        state = db_ops.exercise_state(db, exercise_id)
        done = db_ops.recorded_seqs(db, run_id) if started else set()
        backend = db_ops.exercise_range_backend(db, exercise_id) or os.getenv("PROVISIONER_BACKEND", "mock")
    if not started:
        logger.warning(f"[scenario_v2] Exercise {exercise_id} is {state or 'missing'}; not run")
        return {"status": "skipped", "exercise_id": exercise_id, "state": state}
    _notify_api("exercise", {"id": exercise_id, "state": "running", "phase": "starting"})

    try:
        executed = fired = 0
        halted_state = None
        total_events = len(timeline)
        for idx, event in enumerate(timeline):
            if idx in done:  # this run fired it before a retry
                executed += 1
                continue
            with _db_session() as db:
                current = db_ops.exercise_state(db, exercise_id)
            if current != "running":
                halted_state = current
                break
            result = _fire_event(exercise_id, idx, event, run_id=run_id)
            executed += 1
            fired += 1 if result.get("status") == "fired" else 0
            _notify_api(
                "exercise",
                {
                    "id": exercise_id,
                    "state": "running",
                    "progress": int(executed / max(total_events, 1) * 100),
                    "current_event": result.get("action"),
                },
            )

        if halted_state is not None:
            logger.info(f"[scenario_v2] Exercise {exercise_id} is {halted_state}; stopped after {executed} events")
            return _halted(exercise_id, halted_state, executed, fired)

        if backend != "mock":
            # A real exercise stays live after its timeline: Students earn detection credit by
            # submitting detections (API, ADR 0005) until an instructor completes it or its
            # duration runs out (app/exercise_completion.py).
            _notify_api("exercise", {"id": exercise_id, "state": "running", "phase": "timeline_complete"})
            logger.info(f"[scenario_v2] Exercise {exercise_id} timeline done ({executed} events); still running")
            return {
                "status": "running",
                "exercise_id": exercise_id,
                "events_executed": executed,
                "injects_fired": fired,
            }

        # Mock: a simulation with no telemetry, so every objective is achieved (ADR 0005).
        with _db_session() as db:
            for obj in objectives:
                ref_id = obj.get("ref_id") if isinstance(obj, dict) else None
                if ref_id:
                    db_ops.achieve_objective(db, exercise_id, str(ref_id))
        completed_objectives = len(objectives)

        with _db_session() as db:
            completed = db_ops.complete_exercise(db, exercise_id)
            final_state = db_ops.exercise_state(db, exercise_id)
        if not completed:
            logger.info(f"[scenario_v2] Exercise {exercise_id} became {final_state} during the run; left as is")
            return _halted(exercise_id, final_state, executed, fired)

        _notify_api(
            "exercise",
            {
                "id": exercise_id,
                "state": "completed",
                "events_executed": executed,
                "injects_fired": fired,
                "objectives_completed": completed_objectives,
            },
        )
        logger.info(
            f"[scenario_v2] Exercise {exercise_id} completed ({executed} events, {fired} fired, "
            f"{completed_objectives} objectives)"
        )
        return {
            "status": "completed",
            "exercise_id": exercise_id,
            "events_executed": executed,
            "injects_fired": fired,
            "objectives_completed": completed_objectives,
        }

    except Exception as e:
        # Cancel only when no retry follows: a retry resumes this run where it stopped.
        if isinstance(e, FINAL_ERRORS) or last_attempt(self):
            with _db_session() as db:
                db_ops.cancel_exercise(db, exercise_id)  # exercises has no error_message column
            _notify_api("exercise", {"id": exercise_id, "state": "failed", "error": str(e)})
        logger.error(f"[scenario_v2] Exercise {exercise_id} FAILED: {e}")
        raise


def _halted(exercise_id: str, state: str | None, executed: int, fired: int) -> dict:
    return {
        "status": "halted",
        "exercise_id": exercise_id,
        "state": state,
        "events_executed": executed,
        "injects_fired": fired,
    }


# -- One inject on demand (instructor) ---------------------------------------
@app.task(bind=True, name="worker.tasks.run_inject")
def run_inject(self, exercise_id: str, action: str, params: dict | None = None):
    """Fire one inject into a running exercise (POST /ops/exercises/{id}/inject).

    Not retried: an inject is not idempotent, and the instructor sees the recorded outcome.
    An exercise that is not running gets a ``skipped`` record and nothing is run.
    """
    with _db_session() as db:
        state = db_ops.exercise_state(db, exercise_id)
        if state is not None and state != "running":
            db_ops.record_inject(
                db,
                exercise_id=exercise_id,
                source="instructor",
                action=str(action)[:100],
                status="skipped",
                detail=f"skipped: exercise is {state}",
            )
    if state is None:
        return {"status": "failed", "dispatched": False, "detail": "exercise not found", "exercise_id": exercise_id}
    if state != "running":
        return {"status": "skipped", "dispatched": False, "detail": f"exercise is {state}", "exercise_id": exercise_id}
    return inject_dispatch.dispatch_inject(exercise_id, action, params or {}, source="instructor")


# -- Scenario execution without an exercise ------------------------------------
@app.task(base=ReliableTask, bind=True, name="worker.tasks.run_scenario_execution")
def run_scenario_execution(self, execution_id: str, scenario_definition: dict):
    """Run a scenario's timeline against a range for POST /scenarios/execute.

    Same dispatch as an exercise; no objectives are scored (they stay unassessed).
    pending -> running -> completed | failed, each conditional on the current state.
    """
    run_id = _run_id(self)
    definition = scenario_definition if isinstance(scenario_definition, dict) else {}
    timeline = definition.get("timeline") if isinstance(definition.get("timeline"), list) else []
    with _db_session() as db:
        started = db_ops.set_execution_state(db, execution_id, "running", only_from=("pending", "running"))
        done = db_ops.recorded_seqs(db, run_id) if started else set()
    if not started:
        return {"status": "skipped", "execution_id": execution_id}
    _notify_api("scenario_execution", {"id": execution_id, "state": "running"})
    try:
        fired = 0
        for idx, event in enumerate(timeline):
            if idx in done:
                continue
            action, params, t, offset, problem = _parse_event(event)
            if problem:
                with _db_session() as db:
                    db_ops.record_inject(
                        db,
                        execution_id=execution_id,
                        run_id=run_id,
                        seq=idx,
                        t=t,
                        action=action[:100],
                        status="failed",
                        detail=f"invalid event: {problem}",
                    )
                continue
            result = inject_dispatch.dispatch_for_execution(execution_id, action, params, seq=idx, t=t, run_id=run_id)
            fired += 1 if result.get("status") == "fired" else 0
            time.sleep(min((offset or 0) * 0.01, 1))
        with _db_session() as db:
            db_ops.set_execution_state(db, execution_id, "completed", only_from=("running",))
        _notify_api("scenario_execution", {"id": execution_id, "state": "completed", "injects_fired": fired})
        return {"status": "completed", "execution_id": execution_id, "events": len(timeline), "injects_fired": fired}
    except Exception as e:
        if isinstance(e, FINAL_ERRORS) or last_attempt(self):
            with _db_session() as db:
                db_ops.set_execution_state(db, execution_id, "failed", only_from=("pending", "running"), error=str(e))
            _notify_api("scenario_execution", {"id": execution_id, "state": "failed", "error": str(e)})
        raise
