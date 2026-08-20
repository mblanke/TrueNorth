"""Collective exercise router — team events driven by a MESL.

A collective exercise (Exercise.kind='collective') carries objectives and a Master
Event Sequence List. The MESL can be **ingested** from an uploaded planner CSV or
**generated** on-box by the model from the exercise objectives.
"""

from __future__ import annotations

import json
import logging
import os
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import mesl as mesl_parse
from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import Exercise, ExerciseObjective, ExerciseState, MeslEvent, Range

AI_ORCHESTRATOR_URL = os.getenv("AI_ORCHESTRATOR_URL", "http://ai-orchestrator:6000")
logger = logging.getLogger("truenorth.api.collective")

router = APIRouter(prefix="/collective-exercises", tags=["collective-exercises"])
MAX_CSV_BYTES = 8 * 1024 * 1024


class CollectiveExerciseIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    range_id: str
    objectives: list[dict] = Field(default_factory=list)  # [{ref,text,moe,competency_code}]


class ObjectiveOut(BaseModel):
    id: str
    ref: str
    text: str
    moe: str
    competency_code: str


class MeslEventOut(BaseModel):
    id: str
    serial: int
    phase: str
    scenario_time: str
    title: str
    description: str
    objective_ref: str
    attack_technique: str
    delivery_method: str
    from_cell: str
    to_participant: str
    expected_action: str
    moe: str
    status: str


def _obj_out(o: ExerciseObjective) -> ObjectiveOut:
    return ObjectiveOut(id=str(o.id), ref=o.ref, text=o.text, moe=o.moe, competency_code=o.competency_code)


def _mesl_out(m: MeslEvent) -> MeslEventOut:
    return MeslEventOut(
        id=str(m.id),
        serial=m.serial,
        phase=m.phase,
        scenario_time=m.scenario_time,
        title=m.title,
        description=m.description,
        objective_ref=m.objective_ref,
        attack_technique=m.attack_technique,
        delivery_method=m.delivery_method,
        from_cell=m.from_cell,
        to_participant=m.to_participant,
        expected_action=m.expected_action,
        moe=m.moe,
        status=m.status,
    )


def _get_collective(db: Session, exercise_id: str) -> Exercise:
    ex = db.query(Exercise).filter_by(id=exercise_id, kind="collective").one_or_none()
    if ex is None:
        raise HTTPException(status_code=404, detail="collective exercise not found")
    return ex


def _add_objectives(db: Session, ex: Exercise, rows: list[dict]) -> int:
    existing = {o.ref for o in db.query(ExerciseObjective).filter_by(exercise_id=ex.id).all()}
    added = 0
    base = db.query(ExerciseObjective).filter_by(exercise_id=ex.id).count()
    for i, r in enumerate(rows):
        ref = str(r.get("ref") or f"O{base + i + 1}")
        if ref in existing:
            continue
        db.add(
            ExerciseObjective(
                exercise_id=ex.id,
                ref=ref,
                text=r.get("text", ""),
                moe=r.get("moe", ""),
                competency_code=r.get("competency_code", ""),
                ordinal=base + i,
            )
        )
        existing.add(ref)
        added += 1
    return added


@router.post("", status_code=201)
def create_exercise(
    body: CollectiveExerciseIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Create a collective exercise on a range (optionally with objectives)."""
    rng = db.query(Range).filter_by(id=body.range_id).one_or_none()
    if rng is None:
        raise HTTPException(status_code=422, detail="range not found")
    ex = Exercise(
        name=body.name,
        kind="collective",
        range_id=rng.id,
        scenario_id=None,
        state=ExerciseState.pending,
        tenant_id=user.tenant_id or None,
    )
    db.add(ex)
    db.flush()
    added = _add_objectives(db, ex, body.objectives)
    db.commit()
    return {"id": str(ex.id), "name": ex.name, "kind": ex.kind, "objectives_added": added}


@router.get("")
def list_exercises(
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    out = []
    for ex in db.query(Exercise).filter_by(kind="collective").filter(Exercise.deleted_at.is_(None)).all():
        out.append(
            {
                "id": str(ex.id),
                "name": ex.name,
                "state": ex.state.value,
                "range_id": str(ex.range_id),
                "objectives": db.query(ExerciseObjective).filter_by(exercise_id=ex.id).count(),
                "mesl_events": db.query(MeslEvent).filter_by(exercise_id=ex.id).count(),
            }
        )
    return out


@router.get("/{exercise_id}")
def get_exercise(
    exercise_id: str,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
) -> dict:
    ex = _get_collective(db, exercise_id)
    objs = db.query(ExerciseObjective).filter_by(exercise_id=ex.id).order_by(ExerciseObjective.ordinal).all()
    mesl = db.query(MeslEvent).filter_by(exercise_id=ex.id).order_by(MeslEvent.serial).all()
    return {
        "id": str(ex.id),
        "name": ex.name,
        "kind": ex.kind,
        "state": ex.state.value,
        "range_id": str(ex.range_id),
        "objectives": [_obj_out(o).model_dump() for o in objs],
        "mesl": [_mesl_out(m).model_dump() for m in mesl],
    }


@router.post("/{exercise_id}/objectives/import")
async def import_objectives(
    exercise_id: str,
    file: UploadFile,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Ingest exercise objectives from a CSV."""
    ex = _get_collective(db, exercise_id)
    text = (await file.read())[:MAX_CSV_BYTES].decode("utf-8-sig", errors="replace")
    added = _add_objectives(db, ex, mesl_parse.parse_objectives(text))
    db.commit()
    return {"imported": True, "objectives_added": added}


@router.post("/{exercise_id}/mesl/import")
async def import_mesl(
    exercise_id: str,
    file: UploadFile,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Ingest a planner MESL (CSV) into structured serials linked to objectives."""
    ex = _get_collective(db, exercise_id)
    text = (await file.read())[:MAX_CSV_BYTES].decode("utf-8-sig", errors="replace")
    serials = mesl_parse.parse_mesl(text)
    # replace the MESL wholesale on import
    db.query(MeslEvent).filter_by(exercise_id=ex.id).delete()
    for s in serials:
        db.add(MeslEvent(exercise_id=ex.id, generated_by_model="", **s))
    db.commit()
    return {"imported": True, "serials": len(serials)}


class MeslGenerateReq(BaseModel):
    event_count: int = Field(default=12, ge=3, le=60)
    adversary: str = Field(default="", description="named actor / adversary package context")
    duration_days: int = Field(default=1, ge=1, le=5)


@router.post("/{exercise_id}/mesl/generate")
def generate_mesl(
    exercise_id: str,
    body: MeslGenerateReq,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Draft a MESL on-box from the exercise objectives (model-generated, human-refined)."""
    ex = _get_collective(db, exercise_id)
    objs = db.query(ExerciseObjective).filter_by(exercise_id=ex.id).order_by(ExerciseObjective.ordinal).all()
    if not objs:
        raise HTTPException(status_code=422, detail="add objectives before generating a MESL")
    payload = {
        "exercise_name": ex.name,
        "objectives": [{"ref": o.ref, "text": o.text, "moe": o.moe} for o in objs],
        "event_count": body.event_count,
        "adversary": body.adversary,
        "duration_days": body.duration_days,
    }
    try:
        resp = httpx.post(f"{AI_ORCHESTRATOR_URL}/ai/mesl-generate", json=payload, timeout=330)
        resp.raise_for_status()
        raw = resp.json().get("output", "")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"MESL generation failed: {exc}") from exc

    events = _parse_ai_json(raw)
    if not isinstance(events, list) or not events:
        raise HTTPException(status_code=502, detail="model returned no usable MESL")
    model_used = resp.json().get("model_used", "")
    db.query(MeslEvent).filter_by(exercise_id=ex.id).delete()
    for i, e in enumerate(events, start=1):
        db.add(
            MeslEvent(
                exercise_id=ex.id,
                serial=int(e.get("serial", i)),
                phase=str(e.get("phase", "")),
                scenario_time=str(e.get("scenario_time", "")),
                title=str(e.get("title", ""))[:255],
                description=str(e.get("description", "")),
                objective_ref=str(e.get("objective_ref", "")),
                attack_technique=str(e.get("attack_technique", "")),
                delivery_method=str(e.get("delivery_method", "cyber")),
                from_cell=str(e.get("from_cell", "")),
                to_participant=str(e.get("to_participant", "")),
                expected_action=str(e.get("expected_action", "")),
                moe=str(e.get("moe", "")),
                status="planned",
                generated_by_model=model_used,
            )
        )
    db.commit()
    return {"generated": True, "serials": len(events), "model_used": model_used}


def _parse_ai_json(raw: str):
    """Tolerant JSON extraction from a model response (strips fences / prose)."""
    s = raw.strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.MULTILINE).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", s, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None
