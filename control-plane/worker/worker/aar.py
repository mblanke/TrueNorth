"""After-action report body built by the worker's ``generate_aar`` task.

Uses the API's report shape (``control-plane/api/app/aar_report.py``): its PDF and HTML
views read ``exercise``, ``scores``, ``objectives`` and ``timeline``. The worker has the
exercise row and its objectives, so it fills those sections; injects and participants
come from the API's ``POST /exercises/{id}/aar/generate``. The flat keys the worker used
to write are kept alongside for existing consumers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .aar_html import render_html


def _timeline(started: str | None, completed: str | None, objectives) -> list[dict[str, Any]]:
    events = [{"at": started, "kind": "lifecycle", "title": "Exercise started", "detail": ""}] if started else []
    events += [
        {"at": str(o[6]), "kind": str(getattr(o[2], "value", o[2])), "title": f"Objective {o[0]} achieved", "detail": o[1] or ""}
        for o in objectives
        if o[4] and o[6]
    ]
    if completed:
        events.append({"at": completed, "kind": "lifecycle", "title": "Exercise completed", "detail": ""})
    # ISO strings from one database sort chronologically; the stable sort keeps
    # "started" first and "completed" last on ties.
    return sorted(events, key=lambda e: e["at"])


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
        "injects": [],
        "timeline": _timeline(started, completed, objectives),
        "participants": [],
    }
    return report, render_html(report)
