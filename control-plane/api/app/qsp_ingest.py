"""CFITES / QSP crosswalk ingestion.

Parses ``crosswalk.csv`` (the derived PO/EO contract) into the structured
qualification spine: Qualification -> PerformanceObjective -> EnablingObjective.

This module never reads QSP ``.docx`` chapter text — that is CAF controlled
content resolved on-box by GLM/Taz. Only the derived crosswalk contract is
ingested here. Re-import is idempotent (upsert on natural keys).
"""

from __future__ import annotations

import csv
import io
import json

from sqlalchemy.orm import Session

from .models import (
    EnablingObjective,
    PerformanceObjective,
    POStatus,
    POTier,
    Qualification,
    QSPEnvironment,
)

# crosswalk `environment` string -> QSPEnvironment member
_ENV_MAP = {
    "cste": QSPEnvironment.cste,
    "cste-sterile": QSPEnvironment.cste_sterile,
    "cote": QSPEnvironment.cote,
    "mobile": QSPEnvironment.mobile,
}
_TIER_MAP = {"core": POTier.core, "gate": POTier.gate}
_STATUS_VALUES = {s.value for s in POStatus}

# `eos` field sentinels that carry no concrete EO codes
_EOS_SENTINELS = {"", "-", "multiple", "todo", "see qsp", "todo-map", "n/a"}


def _to_int(value: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _split_semicolon(value: str) -> list[str]:
    return [p.strip() for p in str(value or "").split(";") if p.strip()]


def parse_eos(eos_field: str) -> list[dict]:
    """Decompose a crosswalk `eos` cell into [{eo_code, title}].

    Handles: "007.01;007.02", "001.01 passive;001.02 active",
    "001.01-001.04" (range kept as one code), and sentinels ("-", "multiple",
    "TODO", "see QSP") which yield no EOs.
    """
    field = str(eos_field or "").strip()
    if field.lower() in _EOS_SENTINELS:
        return []
    out: list[dict] = []
    for tok in field.split(";"):
        tok = tok.strip()
        if not tok:
            continue
        parts = tok.split(None, 1)
        code = parts[0].strip()
        title = parts[1].strip() if len(parts) > 1 else ""
        # a real EO code carries a digit (skips stray words like "see")
        if not any(ch.isdigit() for ch in code):
            continue
        out.append({"eo_code": code, "title": title})
    return out


def parse_crosswalk(csv_text: str) -> list[dict]:
    """Parse crosswalk CSV text into normalized, typed row dicts.

    Pure function (no DB) so it is unit-testable. Unknown/blank enum-ish values
    fall back to sane defaults (environment=COTE, tier=core, status=todo).
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    rows: list[dict] = []
    for raw in reader:
        if not raw:
            continue
        qsp_code = (raw.get("qsp_code") or "").strip()
        po_code = (raw.get("po_id") or "").strip()
        if not qsp_code or not po_code:
            continue
        env_key = (raw.get("environment") or "").strip().lower()
        tier_key = (raw.get("tier") or "").strip().lower()
        status_key = (raw.get("status") or "").strip().lower()
        rows.append(
            {
                "qsp_code": qsp_code,
                "nqual": (raw.get("nqual") or qsp_code).strip(),
                "component_version": (raw.get("component_version") or "v2.1.0").strip(),
                "po": {
                    "po_code": po_code,
                    "title": (raw.get("po_title") or "").strip(),
                    "tier": _TIER_MAP.get(tier_key, POTier.core),
                    "conditions": (raw.get("conditions") or "").strip(),
                    "critical_events": _split_semicolon(raw.get("critical_events", "")),
                    "assessment_type": (raw.get("assessment_type") or "").strip(),
                    "duration_min": _to_int(raw.get("duration_min", 0)),
                    "pass_standard": (raw.get("pass_standard") or "").strip(),
                    "deliverable": (raw.get("deliverable") or "").strip(),
                    "environment": _ENV_MAP.get(env_key, QSPEnvironment.cote),
                    "target_role": (raw.get("target_role") or "").strip(),
                    "nice_dcwf_task": (raw.get("nice_dcwf_task") or "").strip(),
                    "scenario_count": _to_int(raw.get("scenario_count", 0)),
                    "build_hours": _to_int(raw.get("build_hours", 0)),
                    "status": POStatus(status_key) if status_key in _STATUS_VALUES else POStatus.todo,
                },
                "eos": parse_eos(raw.get("eos", "")),
            }
        )
    return rows


def import_crosswalk(db: Session, csv_text: str, tenant_id: str | None = None) -> dict:
    """Upsert the qualification spine from crosswalk CSV. Idempotent.

    Returns counts of qualifications/POs/EOs created and updated.
    """
    rows = parse_crosswalk(csv_text)
    stats = {"qualifications": 0, "performance_objectives": 0, "enabling_objectives": 0,
             "updated_pos": 0, "rows": len(rows)}

    qual_cache: dict[str, Qualification] = {}
    for row in rows:
        qsp_code = row["qsp_code"]
        qual = qual_cache.get(qsp_code)
        if qual is None:
            qual = db.query(Qualification).filter_by(qsp_code=qsp_code).one_or_none()
            if qual is None:
                qual = Qualification(
                    qsp_code=qsp_code,
                    nqual=row["nqual"],
                    component_version=row["component_version"],
                    tenant_id=tenant_id,
                )
                db.add(qual)
                db.flush()
                stats["qualifications"] += 1
            else:
                qual.nqual = row["nqual"]
                qual.component_version = row["component_version"]
            qual_cache[qsp_code] = qual

        p = row["po"]
        po = (
            db.query(PerformanceObjective)
            .filter_by(qualification_id=qual.id, po_code=p["po_code"])
            .one_or_none()
        )
        created = po is None
        if created:
            po = PerformanceObjective(qualification_id=qual.id, po_code=p["po_code"])
            db.add(po)
        po.title = p["title"]
        po.tier = p["tier"]
        po.conditions = p["conditions"]
        po.critical_events = json.dumps(p["critical_events"])
        po.assessment_type = p["assessment_type"]
        po.duration_min = p["duration_min"]
        po.pass_standard = p["pass_standard"]
        po.deliverable = p["deliverable"]
        po.environment = p["environment"]
        po.target_role = p["target_role"]
        po.nice_dcwf_task = p["nice_dcwf_task"]
        po.scenario_count = p["scenario_count"]
        po.build_hours = p["build_hours"]
        po.status = p["status"]
        db.flush()
        if created:
            stats["performance_objectives"] += 1
        else:
            stats["updated_pos"] += 1

        for eo in row["eos"]:
            existing = (
                db.query(EnablingObjective)
                .filter_by(po_id=po.id, eo_code=eo["eo_code"])
                .one_or_none()
            )
            if existing is None:
                db.add(EnablingObjective(po_id=po.id, eo_code=eo["eo_code"], title=eo["title"]))
                stats["enabling_objectives"] += 1
            else:
                existing.title = eo["title"] or existing.title

    db.commit()
    return stats
