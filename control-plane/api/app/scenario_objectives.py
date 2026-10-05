"""A scenario's objectives, materialised on the exercise that runs it.

The worker scores the Objective rows an exercise has, by ``ref_id``
(``run_scenario_v2``), and the AAR reports them. Curriculum-driven exercises get theirs
from app/qsp_paths.py; an exercise created from a scenario got none, so it scored 0/0
with an empty AAR. ``materialise`` reads the scenario YAML's ``objectives`` list
(``id``, ``type``, ``validator``, ``params``, ``points``; see
scenario-engine/examples/*.yaml) into rows, in the same shape qsp_paths writes:
validators are named ``validate.<name>``. Entries without an id or that are not
mappings are skipped, and an unknown type counts as a detection, so a scenario with a
typo still runs.
"""

from __future__ import annotations

import json
import uuid

import yaml
from sqlalchemy.orm import Session

from .models import Objective, ObjectiveType


def parse(scenario_yaml: str | None) -> list[dict]:
    """The scenario's usable objectives, normalised; [] when there are none."""
    try:
        doc = yaml.safe_load(scenario_yaml or "") or {}
    except yaml.YAMLError:
        return []
    items = doc.get("objectives") if isinstance(doc, dict) else None
    out, seen = [], set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            continue
        ref_id = str(item["id"]).strip()[:100]
        if ref_id in seen:
            continue
        seen.add(ref_id)
        kind = str(item.get("type") or "").strip().lower()
        validator = str(item.get("validator") or "manual").strip()
        try:
            points = max(0, int(item.get("points") or 0))
        except (TypeError, ValueError):
            points = 0
        out.append(
            {
                "ref_id": ref_id,
                "objective_type": ObjectiveType(kind)
                if kind in ObjectiveType._value2member_map_
                else ObjectiveType.detection,
                "validator": validator if validator.startswith("validate.") else f"validate.{validator}",
                "validator_params": json.dumps(item.get("params") or {}, sort_keys=True),
                "description": str(item.get("description") or item.get("title") or ""),
                "points": points,
            }
        )
    return out


def materialise(db: Session, exercise_id: uuid.UUID, scenario_yaml: str | None) -> int:
    """Add the scenario's objectives to the exercise. Returns their total points (0 if none)."""
    objectives = parse(scenario_yaml)
    for o in objectives:
        db.add(Objective(exercise_id=exercise_id, achieved=False, **o))
    return sum(o["points"] for o in objectives)
