"""After-action report body built by the worker's ``generate_aar`` task.

Uses the API's report shape (``control-plane/api/app/routers/exercises.py`` generate_aar):
its PDF and HTML views read ``exercise`` and ``scores``. The flat keys the worker used to
write are kept alongside for existing consumers.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any


def build_report(exercise_id: str, exercise_row, objectives) -> tuple[dict[str, Any], str]:
    """(report dict, report_html) from ``db_ops.exercise_for_aar`` / ``objectives_for_aar`` rows."""
    _, name, state, total, max_score, started_at, completed_at = exercise_row
    total, max_score = total or 0, max_score or 0
    pct = round(total / max(max_score, 1) * 100, 1)
    started = str(started_at) if started_at else None
    completed = str(completed_at) if completed_at else None
    report = {
        "exercise": {"id": exercise_id, "name": name, "state": state, "started_at": started, "completed_at": completed},
        "scenario": None,
        "scores": {"total": total, "max": max_score, "pct": pct},
        "generated_by": "worker",
        "exercise_id": exercise_id,
        "exercise_name": name,
        "state": state,
        "total_score": total,
        "max_score": max_score,
        "started_at": started,
        "completed_at": completed,
        "generated_at": datetime.now(UTC).isoformat(),
        "objectives": [
            {
                "ref_id": o[0],
                "description": o[1],
                "type": o[2],
                "points": o[3],
                "achieved": o[4],
                "evidence": o[5],
                "achieved_at": str(o[6]) if o[6] else None,
            }
            for o in objectives
        ],
        "summary": {
            "total_objectives": len(objectives),
            "achieved": sum(1 for o in objectives if o[4]),
            "score_pct": pct,
        },
    }
    report_html = f"<html><body><h1>AAR: {html.escape(str(name))}</h1><p>Score: {pct}%</p></body></html>"
    return report, report_html
