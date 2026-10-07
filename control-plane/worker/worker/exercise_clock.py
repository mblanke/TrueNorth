"""Exercises on a real backend end when their time is up (ADR 0005 §6).

``run_scenario_v2`` leaves a real exercise running after its timeline, so Students can
submit detections for the scenario's whole ``duration_minutes``. This beat task completes
the ones whose time has passed. An exercise whose scenario sets no duration stays running
until an instructor completes it. Paused time counts: the clock is wall time since start.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import yaml

from . import db_ops
from .celery_app import app

logger = logging.getLogger("truenorth.worker.exercise_clock")

DURATION_KEYS = ("duration_minutes", "duration_min")  # both spellings are in shipped content


def duration(scenario_yaml: str | None) -> timedelta | None:
    """The scenario's planned length, or None when it sets none (or a nonsense one)."""
    try:
        doc = yaml.safe_load(scenario_yaml or "") or {}
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    for key in DURATION_KEYS:
        try:
            minutes = float(doc[key])
        except (KeyError, TypeError, ValueError):
            continue
        if minutes > 0:
            return timedelta(minutes=minutes)
    return None


def overdue(rows, now: datetime) -> list[str]:
    """Ids of the running exercises (``db_ops.running_exercises`` rows) whose time is up."""
    out = []
    for exercise_id, started_at, scenario_yaml in rows:
        length = duration(scenario_yaml)
        start = started_at if started_at.tzinfo else started_at.replace(tzinfo=UTC)
        if length is not None and start + length <= now:
            out.append(str(exercise_id))
    return out


@app.task(bind=True, name="worker.tasks.close_overdue_exercises")
def close_overdue_exercises(self):
    """Periodic: complete every running exercise past its scenario's duration."""
    from .tasks import _db_session, _notify_api  # lazy: tasks imports this app at load time

    with _db_session() as db:
        ids = overdue(db_ops.running_exercises(db), datetime.now(UTC))
        for exercise_id in ids:
            db_ops.complete_exercise(db, exercise_id)  # a no-op if an instructor got there first
    for exercise_id in ids:
        _notify_api("exercise", {"id": exercise_id, "state": "completed", "reason": "duration elapsed"})
        logger.info("[clock] exercise %s completed: duration elapsed", exercise_id)
    return {"completed": ids}
