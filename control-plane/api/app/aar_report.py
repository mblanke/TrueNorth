"""After-action report data, gathered from the database.

``build_report`` is what ``POST /exercises/{id}/aar/generate`` stores as
``report_json``; ``aar_html.render_html`` turns it into the page served by
``GET /exercises/{id}/aar/html``. The worker's ``generate_aar`` (worker/aar.py) writes
the same shape with the subset of sections it can read.

Sections: exercise, scenario, range, scores, summary, objectives, injects (the
scenario's planned timeline and any MESL serials), timeline (what happened, in time
order), participants, generated_at/by.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import yaml
from sqlalchemy.orm import Session

from .models import (
    AnalystAnnotation,
    AuditLog,
    CompetencyAutoAssessment,
    Exercise,
    MeslEvent,
    Objective,
    Range,
    Scenario,
    Team,
    TeamMembership,
    User,
)

# Audit actions on an exercise worth showing on its timeline.
_AUDIT_EVENTS = {
    "create": "Exercise created",
    "start": "Exercise started",
    "run": "Exercise run started",
    "pause": "Exercise paused",
    "resume": "Exercise resumed",
    "complete": "Exercise completed",
}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _scenario_injects(scenario: Scenario | None) -> list[dict[str, Any]]:
    if scenario is None or not scenario.yaml:
        return []
    try:
        parsed = yaml.safe_load(scenario.yaml) or {}
    except yaml.YAMLError:
        return []
    timeline = parsed.get("timeline") if isinstance(parsed, dict) else None
    out: list[dict[str, Any]] = []
    for step in timeline if isinstance(timeline, list) else []:
        if not isinstance(step, dict):
            continue
        params = step.get("params") if isinstance(step.get("params"), dict) else {}
        detail = step.get("description") or ", ".join(
            f"{k}: {params[k]}" for k in ("mitre_technique", "target", "subject") if params.get(k)
        )
        out.append(
            {
                "at": f"T+{step['t']}" if step.get("t") is not None else "",
                "title": str(step.get("action") or step.get("name") or "inject"),
                "detail": str(detail or ""),
                "status": "",
                "source": "scenario",
            }
        )
    return out


def _mesl_injects(db: Session, exercise_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = db.query(MeslEvent).filter(MeslEvent.exercise_id == exercise_id).order_by(MeslEvent.serial).all()
    return [
        {
            "at": e.scenario_time or f"Serial {e.serial:03d}",
            "title": e.title,
            "detail": " / ".join(
                x for x in (e.from_cell and f"{e.from_cell} -> {e.to_participant}", e.description) if x
            ),
            "status": e.status,
            "source": "mesl",
        }
        for e in rows
    ]


def _timeline(db: Session, ex: Exercise, objectives: list[Objective]) -> list[dict[str, Any]]:
    events: list[tuple[datetime, dict[str, Any]]] = []

    def add(at: datetime | None, kind: str, title: str, detail: str = "") -> None:
        if at is None:
            return
        key = at if at.tzinfo else at.replace(tzinfo=UTC)
        events.append((key, {"at": _iso(at), "kind": kind, "title": title, "detail": detail}))

    audit = (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "exercise", AuditLog.resource_id == str(ex.id))
        .filter(AuditLog.action.in_(list(_AUDIT_EVENTS)))
        .all()
    )
    seen = {a.action for a in audit}
    for a in audit:
        add(a.timestamp, "lifecycle", _AUDIT_EVENTS[a.action])
    # The worker completes a run without an audit row; fall back to the exercise's own stamps.
    if not seen & {"start", "run"}:
        add(ex.started_at, "lifecycle", "Exercise started")
    if "complete" not in seen:
        add(ex.completed_at, "lifecycle", "Exercise completed")
    for o in objectives:
        if o.achieved:
            add(o.achieved_at, o.objective_type.value, f"Objective {o.ref_id} achieved", o.description or "")
    for n in db.query(AnalystAnnotation).filter(AnalystAnnotation.exercise_id == ex.id).all():
        add(n.created_at, "annotation", f"{n.annotation_type} by {n.user_display_name or 'analyst'}", n.content)
    events.sort(key=lambda pair: pair[0])
    return [e for _, e in events]


def _participants(db: Session, ex: Exercise) -> list[dict[str, Any]]:
    people: dict[uuid.UUID, dict[str, Any]] = {}
    memberships = (
        db.query(TeamMembership, Team, User)
        .join(Team, TeamMembership.team_id == Team.id)
        .join(User, TeamMembership.user_id == User.id)
        .filter(Team.exercise_id == ex.id, User.tenant_id == ex.tenant_id)
        .all()
    )
    for m, team, u in memberships:
        people[u.id] = {"name": u.display_name, "team": team.name, "role": m.role}
    others = (
        db.query(User)
        .filter(User.tenant_id == ex.tenant_id)
        .filter(
            User.id.in_(
                db.query(CompetencyAutoAssessment.user_id).filter(CompetencyAutoAssessment.exercise_id == ex.id)
            )
            | User.id.in_(db.query(AnalystAnnotation.user_id).filter(AnalystAnnotation.exercise_id == ex.id))
        )
        .all()
    )
    for u in others:
        people.setdefault(u.id, {"name": u.display_name, "team": "", "role": u.role.value if u.role else ""})
    return sorted(people.values(), key=lambda p: (p["team"], p["name"]))


def build_report(db: Session, ex: Exercise, scenario: Scenario | None, generated_by: str) -> dict[str, Any]:
    """The AAR for ``ex`` (already tenant-checked by the caller)."""
    objectives = db.query(Objective).filter(Objective.exercise_id == ex.id).order_by(Objective.ref_id).all()
    rng = db.query(Range).filter(Range.id == ex.range_id, Range.tenant_id == ex.tenant_id).first()
    total, max_score = ex.total_score or 0, ex.max_score or 0
    pct = round(total / max(max_score, 1) * 100, 1)
    achieved = sum(1 for o in objectives if o.achieved)
    return {
        "exercise": {
            "id": str(ex.id),
            "name": ex.name,
            "kind": ex.kind,
            "state": ex.state.value,
            "started_at": _iso(ex.started_at),
            "completed_at": _iso(ex.completed_at),
        },
        "scenario": {"id": str(scenario.id), "name": scenario.name} if scenario else None,
        "range": {"id": str(rng.id), "name": rng.name} if rng else None,
        "scores": {"total": total, "max": max_score, "pct": pct},
        "summary": {"total_objectives": len(objectives), "achieved": achieved, "score_pct": pct},
        "objectives": [
            {
                "ref_id": o.ref_id,
                "type": o.objective_type.value,
                "description": o.description,
                "points": o.points,
                "achieved": o.achieved,
                "evidence": o.evidence,
                "achieved_at": _iso(o.achieved_at),
                "competency_code": o.competency_code or "",
            }
            for o in objectives
        ],
        "injects": _scenario_injects(scenario) + _mesl_injects(db, ex.id),
        "timeline": _timeline(db, ex, objectives),
        "participants": _participants(db, ex),
        "generated_at": datetime.now(UTC).isoformat(),
        "generated_by": generated_by,
    }
