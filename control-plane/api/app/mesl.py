"""MESL (Master Event Sequence List) ingest.

Parses an uploaded MESL spreadsheet (CSV) into structured serials linked to
exercise objectives. Tolerant of column-name variants used by exercise planners.
Also parses a simple objectives CSV. Deterministic; no LLM (the generate path
uses the on-box model separately).
"""

from __future__ import annotations

import csv
import io

# header alias -> canonical field
_MESL_ALIASES: dict[str, str] = {
    "serial": "serial", "ser": "serial", "no": "serial", "num": "serial", "#": "serial", "event_no": "serial",
    "phase": "phase", "day": "phase",
    "time": "scenario_time", "dtg": "scenario_time", "scenario_time": "scenario_time", "when": "scenario_time",
    "title": "title", "inject": "title", "event": "title", "summary": "title",
    "description": "description", "detail": "description", "narrative": "description",
    "objective": "objective_ref", "obj": "objective_ref", "objective_ref": "objective_ref", "obj_ref": "objective_ref",
    "technique": "attack_technique", "attack_technique": "attack_technique", "mitre": "attack_technique",
    "delivery": "delivery_method", "method": "delivery_method", "means": "delivery_method",
    "delivery_method": "delivery_method",
    "from": "from_cell", "from_cell": "from_cell", "source": "from_cell", "cell": "from_cell",
    "to": "to_participant", "to_participant": "to_participant", "target": "to_participant", "audience": "to_participant",
    "expected": "expected_action", "expected_action": "expected_action", "response": "expected_action",
    "action": "expected_action", "expected_response": "expected_action",
    "moe": "moe", "assessment": "moe", "measure": "moe", "mop": "moe",
}

_DELIVERY = {"cyber", "white_cell", "email", "radio", "physical", "opfor"}


def _norm(h: str) -> str:
    return (h or "").strip().lower().replace(" ", "_")


def parse_mesl(csv_text: str) -> list[dict]:
    """Parse MESL CSV into normalized serial dicts (tolerant of column-name variants)."""
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return []
    headers = [_norm(h) for h in rows[0]]
    field_for = [_MESL_ALIASES.get(h, h) for h in headers]
    out: list[dict] = []
    serial_auto = 0
    for raw in rows[1:]:
        if not any(c.strip() for c in raw):
            continue
        rec: dict = {}
        for i, val in enumerate(raw):
            if i < len(field_for):
                rec[field_for[i]] = (val or "").strip()
        serial_auto += 1
        try:
            serial = int(str(rec.get("serial", "")).strip().lstrip("0") or serial_auto)
        except ValueError:
            serial = serial_auto
        delivery = _norm(rec.get("delivery_method", "cyber")).replace("-", "_")
        if delivery not in _DELIVERY:
            delivery = "white_cell" if "white" in delivery else ("cyber" if delivery in ("", "network") else delivery)
            if delivery not in _DELIVERY:
                delivery = "cyber"
        out.append({
            "serial": serial,
            "phase": rec.get("phase", ""),
            "scenario_time": rec.get("scenario_time", ""),
            "title": rec.get("title", ""),
            "description": rec.get("description", ""),
            "objective_ref": rec.get("objective_ref", ""),
            "attack_technique": rec.get("attack_technique", ""),
            "delivery_method": delivery,
            "from_cell": rec.get("from_cell", ""),
            "to_participant": rec.get("to_participant", ""),
            "expected_action": rec.get("expected_action", ""),
            "moe": rec.get("moe", ""),
        })
    return out


def parse_objectives(csv_text: str) -> list[dict]:
    """Parse an exercise-objectives CSV: columns ref, text/objective, moe, competency_code."""
    reader = csv.DictReader(io.StringIO(csv_text))
    out: list[dict] = []
    for i, raw in enumerate(reader, start=1):
        row = {_norm(k): (v or "").strip() for k, v in raw.items()}
        text = row.get("text") or row.get("objective") or row.get("description") or ""
        if not text and not row.get("ref"):
            continue
        out.append({
            "ref": row.get("ref") or row.get("obj") or str(i),
            "text": text,
            "moe": row.get("moe") or row.get("assessment") or "",
            "competency_code": row.get("competency_code") or row.get("nice") or "",
            "ordinal": i,
        })
    return out
