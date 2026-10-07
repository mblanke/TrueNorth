"""TrueNorth Range - the exercise run loop: run_scenario and run_scenario_v2.

Moved out of tasks.py (ADR 0003). The Celery names are unchanged
(``worker.tasks.run_scenario``, ``worker.tasks.run_scenario_v2``; the contract is
contracts.py), and ``from worker.tasks import run_scenario_v2`` still works.

Injection goes through one seam, ``inject_dispatch.dispatch_inject``, called once per
timeline event (run_scenario_v2: off the mock backend only, where the stub was). The DB
session and notifications come from task_plumbing.py.
"""

from __future__ import annotations

import logging
import os
import time

from . import db_ops, inject_dispatch
from .base_tasks import ReliableTask
from .celery_app import app
from .detection import detection_scorer
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

        # The injection seam (inject_dispatch.py); not wired yet, so a no-op.
        inject_dispatch.dispatch_inject(exercise_id, action, params)

        # Compressed time for dev/test
        time.sleep(min(offset_seconds * 0.01, 2))
        executed += 1

    logger.info(f"[scenario] Exercise {exercise_id} complete ({executed} events)")
    return {"status": "completed", "exercise_id": exercise_id, "events_executed": executed}


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
        detections = detection_scorer(exercise_id, _db_session, backend)  # None unless DETECTION_SCORING=on

        # ── Update exercise state to running ─────────────────────────
        with _db_session() as db:
            db_ops.start_exercise(db, exercise_id)

        # ── Execute timeline events ──────────────────────────────────
        executed = 0
        total_events = len(timeline)

        for idx, event in enumerate(timeline):
            t = event.get("t", "0:00")
            action = event.get("action", "noop")
            params = event.get("params", {})

            parts = t.split(":")
            offset_seconds = int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else 0

            logger.info(f"[scenario_v2] [{t}] event {idx + 1}/{total_events} action={action} params={params}")

            if backend != "mock":
                # The injection seam (inject_dispatch.py); not wired yet, so a no-op.
                inject_dispatch.dispatch_inject(exercise_id, action, params)
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

        # ── Track objective completion ───────────────────────────────
        if detections:  # final pass against the range's telemetry (worker/detection.py)
            completed_objectives = detections.score()
        else:  # mock auto-completes every objective; a real backend with scoring off achieves none
            with _db_session() as db:
                for obj in objectives if backend == "mock" else []:
                    db_ops.achieve_objective(db, exercise_id, obj.get("ref_id", ""))
            completed_objectives = len(objectives) if backend == "mock" else 0

        # ── Mark exercise complete ───────────────────────────────────
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
