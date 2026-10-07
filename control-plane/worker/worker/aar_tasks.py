"""TrueNorth Range - after-action review generation (generate_aar).

Moved out of tasks.py (ADR 0003). The Celery name is unchanged
(``worker.tasks.generate_aar``), and ``from worker.tasks import generate_aar`` still works.
The report itself is built by aar.py; the DB session and notifications come from
task_plumbing.py.
"""

from __future__ import annotations

import json
import logging
import os

from . import db_ops
from .aar import build_report as build_aar_report
from .base_tasks import ReliableTask
from .celery_app import app
from .task_plumbing import db_session as _db_session
from .task_plumbing import notify_api as _notify_api

logger = logging.getLogger("truenorth.worker")


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
        # ── Gather exercise data ─────────────────────────────────────
        with _db_session() as db:
            exercise_row = db_ops.exercise_for_aar(db, exercise_id)

            if not exercise_row:
                raise ValueError(f"Exercise {exercise_id} not found")

            objectives = db_ops.objectives_for_aar(db, exercise_id)

        # ── Build report ─────────────────────────────────────────────
        aar_report, report_html = build_aar_report(exercise_id, exercise_row, objectives)

        # ── Optional AI analysis (if orchestrator available) ─────────
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

        # ── Store AAR ────────────────────────────────────────────────
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
