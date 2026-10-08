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
from datetime import UTC, datetime

from . import db_ops
from .aar import build_report as build_aar_report
from .aar_html import render_html
from .ai_client import orchestrator_headers
from .base_tasks import ReliableTask
from .celery_app import app
from .task_plumbing import db_session as _db_session
from .task_plumbing import notify_api as _notify_api

logger = logging.getLogger("truenorth.worker")

AI_TIMEOUT_SECONDS = 60
MAX_REPORT_CHARS = 50000  # AARAnalysisRequest.report_data max_length in ai-orchestrator/app/main.py


def ai_analysis(ai_url: str, exercise_id: str, report: dict) -> dict:
    """Ask the orchestrator for an AAR analysis; never raises.

    Calls ``POST /ai/aar-analysis`` with ``{report_data, context}`` and returns the section
    the API writes too (routers/exercises.py: ``summary``, ``model``, ``generated_at``), plus
    ``status: "available"``. On any failure it logs a WARNING and returns
    ``{"status": "unavailable", "reason": ...}``, so the stored report says the analysis was
    attempted and failed rather than silently having none. (This used to post the raw report
    to ``/analyze-aar``, a route the orchestrator never had, and drop every failure.)
    """
    import httpx

    try:
        with httpx.Client(timeout=AI_TIMEOUT_SECONDS) as client:
            resp = client.post(
                f"{ai_url.rstrip('/')}/ai/aar-analysis",
                json={"report_data": json.dumps(report)[:MAX_REPORT_CHARS], "context": {"exercise_id": exercise_id}},
                headers=orchestrator_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        summary = data.get("output") if isinstance(data, dict) else None
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("orchestrator returned no analysis text")
    except Exception as exc:  # noqa: BLE001 — any fault leaves the AAR without AI, visibly
        reason = f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else str(exc) or type(exc).__name__
        logger.warning("[aar] AI analysis unavailable for exercise %s: %s", exercise_id, reason)
        return {"status": "unavailable", "reason": reason[:500], "attempted_at": datetime.now(UTC).isoformat()}
    return {
        "status": "available",
        "summary": summary,
        "model": data.get("model_used", "unknown"),
        "generated_at": datetime.now(UTC).isoformat(),
    }


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

        # ── Optional AI analysis (when an orchestrator is configured) ─
        ai_url = os.getenv("AI_ORCHESTRATOR_URL")
        if ai_url:
            aar_report["ai_analysis"] = ai_analysis(ai_url, exercise_id, aar_report)
            if aar_report["ai_analysis"]["status"] == "available":
                report_html = render_html(aar_report)  # the page gains its AI section

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
