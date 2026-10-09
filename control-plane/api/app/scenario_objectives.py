"""A scenario's objectives, materialised as Objective rows on the exercise that runs it.

The exercise's Objective rows are what it is scored on: the run sends them to the worker
(``ref_id`` is what the mock runner marks achieved), a Student's detection is judged
against them (ADR 0005, app/detections/credit.py), and the AAR reports them. An exercise
created through ``POST /exercises`` got none, so it scored 0/0 with an empty AAR (PR #28);
only the forge and QSP paths made rows.

``parse`` reads the scenario YAML's ``objectives`` list (``id``, ``type``, ``validator``,
``params``, ``points``; see scenario-engine/examples/*.yaml) into row values. Validators are
written in their canonical spelling (app/detections/names.py); params are kept as JSON so
the answer key travels with the row. An entry that is not a mapping is skipped, one with
no id gets ``obj-<n>``, a repeated id is skipped (scoring is by ``ref_id``), and an
unknown type counts as a deliverable, so a scenario with a typo still runs.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import yaml
from sqlalchemy.orm import Session

from . import safe_yaml
from .detections.names import canonical_validator
from .models import Objective, ObjectiveType

TYPE_MAP = {
    "detection": ObjectiveType.detection,
    "containment": ObjectiveType.response,
    "eradication": ObjectiveType.response,
    "recovery": ObjectiveType.response,
    "response": ObjectiveType.response,
    "analysis": ObjectiveType.deliverable,
    "deliverable": ObjectiveType.deliverable,
}


def _points(raw: Any) -> int:
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def parse_objectives(items: Any) -> list[dict[str, Any]]:
    """Row values for a parsed ``objectives`` list; [] when it is not a list."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        ref_id = (str(item.get("id") or "").strip() or f"obj-{len(out) + 1}")[:100]
        if ref_id in seen:
            continue
        seen.add(ref_id)
        competency = str(item.get("competency_code") or "")[:50] or None
        out.append(
            {
                "ref_id": ref_id,
                "objective_type": TYPE_MAP.get(str(item.get("type") or "").strip().lower(), ObjectiveType.deliverable),
                "description": str(item.get("name") or item.get("description") or item.get("title") or ref_id),
                "validator": canonical_validator(str(item.get("validator") or ""))[:255],
                "validator_params": json.dumps(item.get("params") or {}, sort_keys=True),
                "points": _points(item.get("points")),
                "competency_code": competency,
            }
        )
    return out


def parse(scenario_yaml: str | None) -> list[dict[str, Any]]:
    """The scenario YAML's objectives as row values; [] when there are none or it is unreadable."""
    try:
        doc = safe_yaml.load(scenario_yaml or "") or {}
    except yaml.YAMLError:
        return []
    return parse_objectives(doc.get("objectives") if isinstance(doc, dict) else None)


def add_rows(db: Session, exercise_id: uuid.UUID, objectives: list[dict[str, Any]]) -> int:
    """Add the rows to the session (not committed). Returns their total points."""
    for o in objectives:
        db.add(Objective(exercise_id=exercise_id, achieved=False, **o))
    return sum(o["points"] for o in objectives)


def materialise(db: Session, exercise_id: uuid.UUID, scenario_yaml: str | None) -> int:
    """Add the scenario's objectives to the exercise. Returns their total points (0 if none)."""
    return add_rows(db, exercise_id, parse(scenario_yaml))
