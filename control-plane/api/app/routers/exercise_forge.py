"""TrueNorth Range — Exercise Forge router.

AI-powered exercise generation from threat intelligence indicators.
Connects threat_intel feeds → AI orchestrator → scenario + exercise creation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import (
    Exercise,
    ExerciseState,
    ForgedExercise,
    Scenario,
    ThreatIndicator,
    ThreatIntelFeed,
)
from ..rbac import Permission, require_permission
from ..schemas import ForgePresetOut, ForgePreviewOut, ForgeRequest, ForgeResultOut

logger = logging.getLogger("truenorth.api.exercise_forge")

router = APIRouter(prefix="/exercise-forge", tags=["exercise-forge"])

AI_ORCHESTRATOR_URL = os.getenv("AI_ORCHESTRATOR_URL", "http://ai-orchestrator:6000")

# ── Presets ──────────────────────────────────────────────────────────────

FORGE_PRESETS: list[dict] = [
    {
        "name": "Quick Detection Drill",
        "difficulty": "beginner",
        "duration_minutes": 30,
        "objective_count": 3,
        "focus_areas": ["detection"],
        "description": "Short focused drill on detecting IOCs from threat intel.",
    },
    {
        "name": "Incident Response Tabletop",
        "difficulty": "intermediate",
        "duration_minutes": 60,
        "objective_count": 5,
        "focus_areas": ["detection", "containment", "eradication"],
        "description": "Full IR workflow from detection through containment and eradication.",
    },
    {
        "name": "Advanced Threat Hunt",
        "difficulty": "advanced",
        "duration_minutes": 120,
        "objective_count": 7,
        "focus_areas": ["detection", "analysis", "containment"],
        "description": "Deep-dive threat hunting exercise with complex multi-stage attack.",
    },
    {
        "name": "Red Team Replay",
        "difficulty": "expert",
        "duration_minutes": 240,
        "objective_count": 10,
        "focus_areas": ["detection", "containment", "eradication", "recovery", "analysis"],
        "description": "Full-spectrum exercise replaying a real threat campaign end-to-end.",
    },
]


@router.get(
    "/presets",
    response_model=list[ForgePresetOut],
    dependencies=[Depends(require_permission(Permission.EXERCISE_READ))],
)
def list_presets():
    """Return available forge presets (difficulty/duration combos)."""
    return [ForgePresetOut(**p) for p in FORGE_PRESETS]


# ── History ──────────────────────────────────────────────────────────────


@router.get(
    "/history",
    dependencies=[Depends(require_permission(Permission.EXERCISE_READ))],
)
def forge_history(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Provenance of previously forged exercises, newest first.

    The ForgedExercise table was write-only — nothing ever read it back. This
    surfaces it so the Forge UI can show what was generated, on which model,
    and link to the resulting exercise/scenario.
    """
    from ..models import Exercise

    limit = max(1, min(limit, 200))
    base = db.query(ForgedExercise).filter(ForgedExercise.tenant_id == user.tenant_id)
    total = base.count()
    rows = base.order_by(ForgedExercise.created_at.desc()).offset(offset).limit(limit).all()

    ex_names = {
        e.id: e.name
        for e in db.query(Exercise)
        .filter(
            Exercise.id.in_([r.exercise_id for r in rows]),
            Exercise.tenant_id == user.tenant_id,
        )
        .all()
    } if rows else {}

    items = [
        {
            "id": str(r.id),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "exercise_id": str(r.exercise_id),
            "scenario_id": str(r.scenario_id),
            "exercise_name": ex_names.get(r.exercise_id, ""),
            "source": "feed" if r.feed_id else "manual/curriculum",
            "difficulty": r.difficulty,
            "model_used": r.model_used,
            "mitre_techniques": json.loads(r.mitre_techniques or "[]"),
        }
        for r in rows
    ]
    return {"items": items, "total": total}


# ── Preview (dry-run) ────────────────────────────────────────────────────


@router.post(
    "/preview",
    response_model=ForgePreviewOut,
    dependencies=[Depends(require_permission(Permission.EXERCISE_CREATE))],
)
async def preview_forge(
    req: ForgeRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate a scenario preview without creating DB records."""
    indicators = _resolve_indicators(req, user.tenant_id, db)
    curriculum_context = await _resolve_curriculum_context(req, user.tenant_id, db)
    if not indicators and not req.learning_objectives:
        raise HTTPException(
            422, "Provide threat indicators (feed/inline) or learning objectives (curriculum mode)."
        )

    scenario_yaml, model_used = await _call_forge_ai(indicators, req, curriculum_context)
    mitre_techniques = _extract_mitre(scenario_yaml)

    return ForgePreviewOut(
        scenario_yaml=scenario_yaml,
        model_used=model_used,
        indicators_used=len(indicators),
        mitre_techniques=mitre_techniques,
        estimated_duration_minutes=req.duration_minutes,
        objective_count=req.objective_count,
    )


# ── Generate (creates exercise + scenario) ───────────────────────────────


@router.post(
    "/generate",
    response_model=ForgeResultOut,
    dependencies=[Depends(require_permission(Permission.EXERCISE_CREATE))],
)
async def generate_exercise(
    req: ForgeRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate a full exercise from threat intel or curriculum objectives, and persist."""
    indicators = _resolve_indicators(req, user.tenant_id, db)
    curriculum_context = await _resolve_curriculum_context(req, user.tenant_id, db)
    if not indicators and not req.learning_objectives:
        raise HTTPException(
            422, "Provide threat indicators (feed/inline) or learning objectives (curriculum mode)."
        )

    scenario_yaml, model_used = await _call_forge_ai(indicators, req, curriculum_context)
    mitre_techniques = _extract_mitre(scenario_yaml)

    # Parse the generated YAML to extract name
    try:
        parsed = yaml.safe_load(scenario_yaml)
    except yaml.YAMLError as e:
        raise HTTPException(502, f"AI generated invalid YAML: {e}") from e

    scenario_name = req.name_override or parsed.get("name", f"forged-{uuid.uuid4().hex[:8]}")

    # Create Scenario record
    scenario = Scenario(
        name=scenario_name,
        yaml=scenario_yaml,
        tenant_id=user.tenant_id,
    )
    db.add(scenario)
    db.flush()

    # Attach to the chosen range, or the tenant's first as before.
    from ..models import Range

    if req.range_id is not None:
        range_obj = (
            db.query(Range)
            .filter(Range.id == req.range_id, Range.tenant_id == user.tenant_id)
            .first()
        )
        if not range_obj:
            raise HTTPException(404, "Range not found in your tenant.")
    else:
        range_obj = db.query(Range).filter(Range.tenant_id == user.tenant_id).first()
        if not range_obj:
            raise HTTPException(
                409,
                "No range available in your tenant. Create a range first, then forge exercises.",
            )

    # Create Exercise record
    exercise = Exercise(
        name=scenario_name,
        range_id=range_obj.id,
        scenario_id=scenario.id,
        state=ExerciseState.pending,
        tenant_id=user.tenant_id,
    )
    db.add(exercise)
    db.flush()

    # Create Objective rows from the generated YAML so scoring and the
    # competency capability loop work without scenario-engine involvement.
    objectives_created = _create_objectives(db, exercise.id, parsed)

    # Create ForgedExercise tracking record
    indicator_ids = [str(i.get("id", "")) for i in indicators if i.get("id")]
    forged = ForgedExercise(
        exercise_id=exercise.id,
        scenario_id=scenario.id,
        feed_id=req.feed_id,
        indicator_ids=json.dumps(indicator_ids),
        scenario_yaml=scenario_yaml,
        mitre_techniques=json.dumps(mitre_techniques),
        difficulty=req.difficulty,
        model_used=model_used,
        tenant_id=user.tenant_id,
    )
    db.add(forged)
    db.commit()

    logger.info(
        "Exercise forged: exercise=%s scenario=%s indicators=%d objectives=%d model=%s",
        exercise.id,
        scenario.id,
        len(indicators),
        objectives_created,
        model_used,
    )

    return ForgeResultOut(
        exercise_id=exercise.id,
        scenario_id=scenario.id,
        name=scenario_name,
        scenario_yaml=scenario_yaml,
        model_used=model_used,
        indicators_used=len(indicators),
        mitre_techniques=mitre_techniques,
    )


# ── Helpers ──────────────────────────────────────────────────────────────


def _resolve_indicators(req: ForgeRequest, tenant_id: uuid.UUID, db: Session) -> list[dict]:
    """Resolve indicators from feed_id or inline list."""
    indicators: list[dict] = []

    if req.feed_id:
        feed = (
            db.query(ThreatIntelFeed)
            .filter(
                ThreatIntelFeed.id == req.feed_id,
                ThreatIntelFeed.tenant_id == tenant_id,
            )
            .first()
        )
        if not feed:
            raise HTTPException(404, "Threat intelligence feed not found.")
        db_indicators = (
            db.query(ThreatIndicator)
            .filter(
                ThreatIndicator.feed_id == feed.id,
                ThreatIndicator.is_active.is_(True),
            )
            .order_by(ThreatIndicator.confidence.desc())
            .limit(20)
            .all()
        )
        for ind in db_indicators:
            mitre = json.loads(ind.mitre_attack_ids) if ind.mitre_attack_ids else []
            indicators.append(
                {
                    "id": str(ind.id),
                    "indicator_type": ind.indicator_type,
                    "value": ind.value,
                    "severity": ind.severity,
                    "description": ind.description,
                    "mitre_attack_ids": mitre,
                }
            )
    elif req.indicators:
        for ind in req.indicators:
            indicators.append(
                {
                    "id": str(ind.indicator_id) if ind.indicator_id else "",
                    "indicator_type": ind.indicator_type,
                    "value": ind.value,
                    "severity": ind.severity,
                    "description": ind.description,
                    "mitre_attack_ids": ind.mitre_attack_ids,
                }
            )

    return indicators


async def _resolve_curriculum_context(
    req: ForgeRequest, tenant_id: uuid.UUID, db: Session
) -> list[str]:
    """Curriculum mode: retrieve grounding chunks for the learning objectives."""
    if not req.curriculum_id or not req.learning_objectives:
        return []
    from .. import curriculum_ingest
    from ..models import Curriculum

    curriculum = (
        db.query(Curriculum)
        .filter(
            Curriculum.id == req.curriculum_id,
            Curriculum.tenant_id == tenant_id,
            Curriculum.deleted_at.is_(None),
        )
        .first()
    )
    if not curriculum:
        raise HTTPException(404, "Curriculum not found.")

    chunks: list[str] = []
    seen: set[str] = set()
    for objective in req.learning_objectives[:8]:
        for hit in await curriculum_ingest.rag_search(req.curriculum_id, objective, k=4):
            key = hit["text"][:120]
            if key not in seen:
                seen.add(key)
                chunks.append(hit["text"])
    return chunks[:20]


def _create_objectives(db: Session, exercise_id: uuid.UUID, parsed: dict) -> int:
    """Persist Objective rows (with competency mapping) from generated YAML."""
    from ..models import Objective, ObjectiveType

    type_map = {
        "detection": ObjectiveType.detection,
        "containment": ObjectiveType.response,
        "eradication": ObjectiveType.response,
        "recovery": ObjectiveType.response,
        "response": ObjectiveType.response,
        "analysis": ObjectiveType.deliverable,
        "deliverable": ObjectiveType.deliverable,
    }
    created = 0
    for obj in parsed.get("objectives") or []:
        if not isinstance(obj, dict):
            continue
        ref_id = str(obj.get("id") or f"obj-{created + 1}")
        db.add(
            Objective(
                exercise_id=exercise_id,
                ref_id=ref_id[:100],
                objective_type=type_map.get(str(obj.get("type", "")).lower(), ObjectiveType.deliverable),
                description=str(obj.get("name") or ref_id),
                validator=str(obj.get("validator") or "validate.manual_ack")[:255],
                validator_params=json.dumps(obj.get("params") or {}),
                points=int(obj.get("points") or 0),
                competency_code=(str(obj.get("competency_code") or "")[:50] or None),
            )
        )
        created += 1
    return created


async def _call_forge_ai(
    indicators: list[dict], req: ForgeRequest, curriculum_context: list[str] | None = None
) -> tuple[str, str]:
    """Call AI orchestrator exercise-forge endpoint."""
    payload = {
        "threat_indicators": indicators,
        "learning_objectives": req.learning_objectives,
        "curriculum_context": curriculum_context or [],
        "difficulty": req.difficulty,
        "duration_minutes": req.duration_minutes,
        "objective_count": req.objective_count,
        "range_template": req.range_template,
        "focus_areas": req.focus_areas,
    }

    async with httpx.AsyncClient(timeout=330) as client:
        try:
            resp = await client.post(
                f"{AI_ORCHESTRATOR_URL}/ai/exercise-forge",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            raw_output = data.get("output", "")
            model_used = data.get("model_used", "unknown")

            # Strip markdown fences if present
            cleaned = _strip_yaml_fences(raw_output)
            return cleaned, model_used
        except httpx.HTTPStatusError as e:
            logger.error("AI orchestrator returned %s: %s", e.response.status_code, e.response.text)
            raise HTTPException(502, "AI orchestrator failed to generate exercise.") from e
        except httpx.ConnectError as e:
            logger.error("Cannot reach AI orchestrator: %s", e)
            raise HTTPException(503, "AI orchestrator is unavailable.") from e


def _strip_yaml_fences(text: str) -> str:
    """Remove markdown code fences from AI output."""
    text = text.strip()
    text = re.sub(r"^```(?:yaml|yml)?\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


def _extract_mitre(yaml_text: str) -> list[str]:
    """Extract MITRE ATT&CK technique IDs from scenario YAML."""
    techniques: set[str] = set()
    for match in re.finditer(r"T\d{4}(?:\.\d{3})?", yaml_text):
        techniques.add(match.group())
    return sorted(techniques)
