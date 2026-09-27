"""TrueNorth Range — Exercises router.

Handles exercise CRUD, lifecycle transitions (start/pause/complete),
objective tracking, and After-Action Report generation.

Permissions required per endpoint:

==========================================  ==========================
Endpoint                                    Permission(s)
==========================================  ==========================
POST   /exercises                           EXERCISE_CREATE
GET    /exercises                           EXERCISE_READ
GET    /exercises/{id}                      EXERCISE_READ
POST   /exercises/{id}/start               EXERCISE_START
POST   /exercises/{id}/pause               EXERCISE_PAUSE
POST   /exercises/{id}/complete            EXERCISE_COMPLETE
GET    /exercises/{id}/objectives           EXERCISE_READ
POST   /exercises/{id}/objectives/{ref}/ack EXERCISE_COMPLETE
POST   /exercises/{id}/aar/generate        AAR_GENERATE
GET    /exercises/{id}/aar                  AAR_READ
GET    /exercises/{id}/aar/html            AAR_READ
GET    /exercises/{id}/aar/pdf             AAR_READ
==========================================  ==========================
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import yaml
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from ..auth import CurrentUser
from ..db import get_db
from ..models import (
    AfterActionReport,
    AuditLog,
    Exercise,
    ExerciseState,
    Objective,
    Range,
    RangeState,
    Scenario,
)
from ..rbac import Permission, require_permission
from ..schemas import (
    AAROut,
    ExerciseIn,
    ExerciseListOut,
    ExerciseOut,
    ExerciseUpdate,
    ObjectiveAck,
    ObjectiveOut,
)
from ..tenancy import get_owned
from ..xapi import emit_lifecycle

logger = logging.getLogger("truenorth.api.exercises")

router = APIRouter(prefix="/exercises", tags=["exercises"])


def _audit(db: Session, user: CurrentUser, action: str, rtype: str, rid: str, detail: str = "") -> None:
    db.add(AuditLog(user_id=uuid.UUID(user.id), action=action, resource_type=rtype, resource_id=rid, detail=detail))


def _dispatch_task(task_name: str, *args: Any) -> str | None:
    """Fire a Celery worker task via the Redis broker (swallows errors if worker is down)."""
    from ..celery_client import dispatch

    return dispatch(task_name, *args)


def _scenario_definition(db: Session, ex: Exercise, user: CurrentUser) -> dict:
    """Build the scenario_definition dict for run_scenario_v2 from the exercise's scenario + objectives.

    Objectives come from the DB Objective rows (their ref_id is what the mock runner marks achieved),
    the timeline is parsed from the Scenario YAML. Robust if the YAML has no timeline.
    """
    definition: dict = {"timeline": [], "objectives": []}
    scenario = get_owned(db, Scenario, ex.scenario_id, user)
    if scenario and scenario.yaml:
        try:
            parsed = yaml.safe_load(scenario.yaml) or {}
            if isinstance(parsed, dict) and isinstance(parsed.get("timeline"), list):
                definition["timeline"] = parsed["timeline"]
        except yaml.YAMLError:
            pass
    objectives = db.query(Objective).filter(Objective.exercise_id == ex.id).all()
    definition["objectives"] = [{"ref_id": o.ref_id, "validator": o.validator, "points": o.points} for o in objectives]
    # Fallback: if the YAML carried no timeline, synthesize one step per objective so the run walks.
    if not definition["timeline"]:
        definition["timeline"] = [{"t": f"{i}:00", "action": f"inject.{o.ref_id}"} for i, o in enumerate(objectives)]
    return definition


# ── CRUD ───────────────────────────────────────────────────────────────
@router.post("", response_model=ExerciseOut, status_code=201)
def create_exercise(
    body: ExerciseIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_CREATE)),
) -> Exercise:
    """Create a new exercise.  **Permission: exercise:create**"""
    rng = get_owned(db, Range, body.range_id, user, not_found="Range not found")
    sc = get_owned(db, Scenario, body.scenario_id, user, not_found="Scenario not found")
    ex = Exercise(
        name=body.name,
        range_id=body.range_id,
        scenario_id=body.scenario_id,
        tenant_id=uuid.UUID(user.tenant_id),
        state=ExerciseState.pending,
        max_score=body.max_score or 100,
    )
    db.add(ex)
    db.commit()
    db.refresh(ex)
    _audit(db, user, "create", "exercise", str(ex.id))
    db.commit()
    return ex


@router.get("", response_model=list[ExerciseListOut])
def list_exercises(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_READ)),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> list[Exercise]:
    """List exercises for user's tenant.  **Permission: exercise:read**"""
    return (
        db.query(Exercise)
        .filter(Exercise.tenant_id == uuid.UUID(user.tenant_id))
        .order_by(Exercise.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.get("/{exercise_id}", response_model=ExerciseOut)
def get_exercise(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_READ)),
) -> Exercise:
    """Retrieve a single exercise.  **Permission: exercise:read**"""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    return ex


@router.put("/{exercise_id}", response_model=ExerciseOut)
def update_exercise(
    body: ExerciseUpdate,
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_CREATE)),
) -> Exercise:
    """Update an exercise.  **Permission: exercise:create**"""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    if ex.state not in (ExerciseState.pending,):
        raise HTTPException(409, f"Cannot edit exercise in state {ex.state.value}")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(ex, field, value)
    db.commit()
    db.refresh(ex)
    _audit(db, user, "update", "exercise", str(ex.id))
    db.commit()
    return ex


# ── Lifecycle ──────────────────────────────────────────────────────────
@router.post("/{exercise_id}/start", response_model=ExerciseOut)
async def start_exercise(
    exercise_id: uuid.UUID = Path(...),
    background_tasks: BackgroundTasks = None,  # type: ignore[assignment]
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_START)),
) -> Exercise:
    """Start a pending exercise.  **Permission: exercise:start**"""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    if ex.state != ExerciseState.pending:
        raise HTTPException(409, f"Exercise is {ex.state.value}, expected pending")
    ex.state = ExerciseState.running
    ex.started_at = datetime.now(UTC)
    db.commit()
    db.refresh(ex)
    _audit(db, user, "start", "exercise", str(ex.id))
    db.commit()
    # Dispatch the scenario runner (mock mode walks the timeline + auto-achieves objectives).
    definition = _scenario_definition(db, ex, user)
    _dispatch_task("run_scenario_v2", str(ex.id), definition)
    if background_tasks is not None:
        emit_lifecycle(
            background_tasks,
            verb_key="attempted",
            user_email=user.email or f"{user.id}@truenorth.local",
            user_name=user.display_name,
            activity_type="exercise",
            activity_id=str(ex.id),
            activity_name=ex.name,
        )
    return ex


@router.get("/{exercise_id}/scenario-detail")
def scenario_detail(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_READ)),
) -> dict:
    """Parsed scenario for the detail view: metadata + timeline + noise floor + objectives."""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    scenario = get_owned(db, Scenario, ex.scenario_id, user)
    parsed: dict = {}
    if scenario and scenario.yaml:
        try:
            parsed = yaml.safe_load(scenario.yaml) or {}
        except yaml.YAMLError:
            parsed = {}
    objectives = db.query(Objective).filter(Objective.exercise_id == ex.id).order_by(Objective.ref_id).all()
    return {
        "exercise_id": str(ex.id),
        "exercise_name": ex.name,
        "state": ex.state.value,
        "total_score": ex.total_score,
        "max_score": ex.max_score,
        "range_id": str(ex.range_id),
        "scenario_id": str(ex.scenario_id) if ex.scenario_id else None,
        "scenario_name": scenario.name if scenario else "",
        "po_id": parsed.get("po_id", ""),
        "environment": parsed.get("environment", ""),
        "duration_min": parsed.get("duration_min", 0),
        "timeline": parsed.get("timeline", []),
        "noise_floor": parsed.get("noise_floor", []),
        "objectives": [
            {
                "ref_id": o.ref_id,
                "type": o.objective_type.value,
                "points": o.points,
                "achieved": o.achieved,
                "evidence": o.evidence or "",
                "validator": o.validator,
                "competency_code": o.competency_code or "",
            }
            for o in objectives
        ],
    }


@router.post("/{exercise_id}/run", response_model=ExerciseOut)
async def run_exercise(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_START)),
) -> Exercise:
    """One-click: provision the range (mock, if needed) then start the run. **Permission: exercise:start**"""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    # Replay: reset a finished/cancelled exercise back to pending before re-running.
    if ex.state in (ExerciseState.completed, ExerciseState.cancelled):
        for obj in db.query(Objective).filter(Objective.exercise_id == ex.id).all():
            obj.achieved = False
            obj.achieved_at = None
        ex.state = ExerciseState.pending
        ex.total_score = 0
        ex.completed_at = None
        db.commit()
    if ex.state != ExerciseState.pending:
        raise HTTPException(409, f"Exercise is {ex.state.value}, expected pending")
    # provision the range if it hasn't been (mock provisioner flips it to ready via the worker)
    rng = get_owned(db, Range, ex.range_id, user)
    if rng and rng.state.can_transition_to(RangeState.provisioning):
        rng.state = RangeState.provisioning
        db.commit()
        _dispatch_task("provision_range", str(rng.id))
    # start the exercise + dispatch the scenario runner
    ex.state = ExerciseState.running
    ex.started_at = datetime.now(UTC)
    db.commit()
    db.refresh(ex)
    _audit(db, user, "run", "exercise", str(ex.id))
    db.commit()
    definition = _scenario_definition(db, ex, user)
    _dispatch_task("run_scenario_v2", str(ex.id), definition)
    return ex


@router.post("/{exercise_id}/pause", response_model=ExerciseOut)
async def pause_exercise(
    exercise_id: uuid.UUID = Path(...),
    background_tasks: BackgroundTasks = None,  # type: ignore[assignment]
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_PAUSE)),
) -> Exercise:
    """Pause a running exercise.  **Permission: exercise:pause**"""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    if ex.state != ExerciseState.running:
        raise HTTPException(409, f"Exercise is {ex.state.value}, expected running")
    ex.state = ExerciseState.paused
    db.commit()
    db.refresh(ex)
    _audit(db, user, "pause", "exercise", str(ex.id))
    db.commit()
    if background_tasks is not None:
        emit_lifecycle(
            background_tasks,
            verb_key="terminated",
            user_email=user.email or f"{user.id}@truenorth.local",
            user_name=user.display_name,
            activity_type="exercise",
            activity_id=str(ex.id),
            activity_name=ex.name,
            context_extensions={"pause": True},
        )
    return ex


@router.post("/{exercise_id}/complete", response_model=ExerciseOut)
async def complete_exercise(
    exercise_id: uuid.UUID = Path(...),
    background_tasks: BackgroundTasks = None,  # type: ignore[assignment]
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_COMPLETE)),
) -> Exercise:
    """Complete an exercise and tally scores.  **Permission: exercise:complete**"""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    if ex.state not in (ExerciseState.running, ExerciseState.paused):
        raise HTTPException(409, f"Exercise is {ex.state.value}, cannot complete")
    ex.state = ExerciseState.completed
    ex.completed_at = datetime.now(UTC)
    objectives = db.query(Objective).filter(Objective.exercise_id == ex.id).all()
    ex.total_score = sum(o.points for o in objectives if o.achieved)
    db.commit()
    db.refresh(ex)
    _audit(db, user, "complete", "exercise", str(ex.id))
    db.commit()
    # Trigger competency auto-assessment (EPIC 3)
    try:
        from worker.celery_app import app as celery_app

        celery_app.send_task(
            "worker.tasks.auto_assess_competency",
            args=[str(ex.id), user.id],
            queue="default",
            ignore_result=True,
        )
    except Exception:
        logger.warning("Failed to dispatch auto-assess task for exercise %s", ex.id)
    if background_tasks is not None:
        # Moodle/LTI grade pass-back (no-op unless launched via LTI)
        background_tasks.add_task(
            _push_exercise_lti_grade, uuid.UUID(user.id), ex.id, ex.total_score or 0, ex.max_score or 100
        )
        max_score = max(ex.max_score or 1, 1)
        emit_lifecycle(
            background_tasks,
            verb_key="completed",
            user_email=user.email or f"{user.id}@truenorth.local",
            user_name=user.display_name,
            activity_type="exercise",
            activity_id=str(ex.id),
            activity_name=ex.name,
            result={
                "score": {
                    "raw": ex.total_score or 0,
                    "max": ex.max_score or 0,
                    "scaled": (ex.total_score or 0) / max_score,
                },
                "completion": True,
                "success": (ex.total_score or 0) >= max_score * 0.7,
            },
        )
    return ex


async def _push_exercise_lti_grade(user_id: uuid.UUID, exercise_id: uuid.UUID, score: int, max_score: int) -> None:
    from .. import lti13
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        await lti13.push_score_for_resource(db, user_id, "exercise", str(exercise_id), score, max_score)
    except Exception as exc:  # advisory — never fail the completion path
        logger.debug("LTI grade push skipped: %s", exc)
    finally:
        db.close()


# ── Objectives ─────────────────────────────────────────────────────────
@router.get("/{exercise_id}/objectives", response_model=list[ObjectiveOut])
def list_objectives(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_READ)),
) -> list[Objective]:
    """List objectives for an exercise.  **Permission: exercise:read**"""
    return db.query(Objective).filter(Objective.exercise_id == exercise_id).all()


@router.post("/{exercise_id}/objectives/{ref_id}/ack", response_model=ObjectiveOut)
async def acknowledge_objective(
    exercise_id: uuid.UUID = Path(...),
    ref_id: str = Path(...),
    body: ObjectiveAck = Depends(),
    background_tasks: BackgroundTasks = None,  # type: ignore[assignment]
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_COMPLETE)),
) -> Objective:
    """Acknowledge (achieve) an objective.  **Permission: exercise:complete**"""
    obj = (
        db.query(Objective)
        .filter(
            Objective.exercise_id == exercise_id,
            Objective.ref_id == ref_id,
        )
        .first()
    )
    if not obj:
        raise HTTPException(404, "Objective not found")
    if obj.achieved:
        raise HTTPException(409, "Objective already achieved")
    obj.achieved = True
    obj.evidence = body.evidence or f"Acknowledged by {user.display_name}"
    obj.achieved_at = datetime.now(UTC)
    db.commit()
    db.refresh(obj)
    if background_tasks is not None:
        emit_lifecycle(
            background_tasks,
            verb_key="passed",
            user_email=user.email or f"{user.id}@truenorth.local",
            user_name=user.display_name,
            activity_type="objective",
            activity_id=str(obj.id),
            activity_name=obj.description or obj.ref_id,
            result={"score": {"raw": obj.points}, "success": True},
            context_extensions={"exercise_id": str(exercise_id), "ref_id": obj.ref_id},
        )
    return obj


# ── AAR ────────────────────────────────────────────────────────────────
@router.post("/{exercise_id}/aar/generate", response_model=AAROut, status_code=201)
def generate_aar(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.AAR_GENERATE)),
) -> AfterActionReport:
    """Generate After-Action Report.  **Permission: aar:generate**"""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    sc = get_owned(db, Scenario, ex.scenario_id, user)
    objectives = db.query(Objective).filter(Objective.exercise_id == ex.id).all()

    report = {
        "exercise": {
            "id": str(ex.id),
            "name": ex.name,
            "state": ex.state.value,
            "started_at": str(ex.started_at) if ex.started_at else None,
            "completed_at": str(ex.completed_at) if ex.completed_at else None,
        },
        "scenario": {"id": str(sc.id), "name": sc.name} if sc else None,
        "scores": {
            "total": ex.total_score,
            "max": ex.max_score,
            "pct": round(ex.total_score / max(ex.max_score, 1) * 100, 1),
        },
        "objectives": [
            {
                "ref_id": o.ref_id,
                "type": o.objective_type.value,
                "description": o.description,
                "points": o.points,
                "achieved": o.achieved,
                "evidence": o.evidence,
            }
            for o in objectives
        ],
        "generated_at": datetime.now(UTC).isoformat(),
        "generated_by": user.display_name,
    }
    report_json = json.dumps(report, indent=2)
    pct = report["scores"]["pct"]
    html = f"<html><body><h1>AAR: {ex.name}</h1><p>Score: {pct}%</p></body></html>"

    existing = db.query(AfterActionReport).filter(AfterActionReport.exercise_id == ex.id).first()
    if existing:
        existing.report_json = report_json
        existing.report_html = html
        aar = existing
    else:
        aar = AfterActionReport(exercise_id=ex.id, report_json=report_json, report_html=html)
        db.add(aar)
    db.commit()
    db.refresh(aar)
    return aar


@router.get("/{exercise_id}/aar", response_model=AAROut)
def get_aar(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.AAR_READ)),
) -> AfterActionReport:
    """Retrieve AAR JSON.  **Permission: aar:read**"""
    aar = db.query(AfterActionReport).filter(AfterActionReport.exercise_id == exercise_id).first()
    if not aar:
        raise HTTPException(404, "AAR not found — generate it first")
    return aar


@router.get("/{exercise_id}/aar/html", response_class=HTMLResponse)
def get_aar_html(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.AAR_READ)),
) -> HTMLResponse:
    """Retrieve AAR as rendered HTML.  **Permission: aar:read**"""
    aar = db.query(AfterActionReport).filter(AfterActionReport.exercise_id == exercise_id).first()
    if not aar:
        raise HTTPException(404, "AAR not found")
    return HTMLResponse(content=aar.report_html)


@router.get("/{exercise_id}/aar/pdf")
def get_aar_pdf(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.AAR_READ)),
) -> StreamingResponse:
    """Retrieve AAR as downloadable PDF.  **Permission: aar:read**

    Renders the stored AAR JSON to a PDF using fpdf2 (pure Python, no C deps).
    """
    from io import BytesIO

    aar = db.query(AfterActionReport).filter(AfterActionReport.exercise_id == exercise_id).first()
    if not aar:
        raise HTTPException(404, "AAR not found — generate it first")

    try:
        from fpdf import FPDF
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(500, "PDF rendering dependency unavailable") from exc

    try:
        data = json.loads(aar.report_json) if aar.report_json else {}
    except json.JSONDecodeError:
        data = {}

    def _s(val: object) -> str:
        """Coerce to str and strip characters outside fpdf2's core-font range (latin-1)."""
        return str(val if val is not None else "").encode("latin-1", "replace").decode("latin-1")

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    # Header
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, "After-Action Report", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)

    ex = data.get("exercise", {})
    pdf.cell(0, 7, _s(f"Exercise: {ex.get('name', 'Unknown')}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, _s(f"State: {ex.get('state', '-')}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, _s(f"Started: {ex.get('started_at') or '-'}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, _s(f"Completed: {ex.get('completed_at') or '-'}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, _s(f"Generated: {data.get('generated_at', '-')}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Scores
    scores = data.get("scores", {})
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Score", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(
        0,
        7,
        _s(f"{scores.get('total', 0)} / {scores.get('max', 0)}  ({scores.get('pct', 0)}%)"),
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(4)

    # Objectives
    objectives = data.get("objectives", [])
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, _s(f"Objectives ({len(objectives)})"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    for obj in objectives:
        status = "[x]" if obj.get("achieved") else "[ ]"
        line = f"{status} [{obj.get('ref_id', '?')}] ({obj.get('points', 0)} pts) {obj.get('description', '')}"
        pdf.multi_cell(0, 6, _s(line))
        if obj.get("evidence"):
            pdf.set_font("Helvetica", "I", 9)
            pdf.multi_cell(0, 5, _s(f"     Evidence: {obj['evidence']}"))
            pdf.set_font("Helvetica", "", 10)
    pdf.ln(4)

    # AI analysis if present
    ai = data.get("ai_analysis")
    if isinstance(ai, dict) and ai.get("summary"):
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, "AI Analysis", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 5, _s(str(ai["summary"])[:4000]))

    pdf.set_y(-20)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, _s(f"Generated by TrueNorth Range - user: {user.display_name}"), align="C")

    buf = BytesIO()
    pdf.output(buf)
    buf.seek(0)
    filename = f"aar-{exercise_id}.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{exercise_id}/aar/ai-enhance", response_model=AAROut)
async def ai_enhance_aar(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.AAR_GENERATE)),
) -> AfterActionReport:
    """Enhance an existing AAR with AI-generated analysis.

    Calls the AI orchestrator's aar-analysis task to produce:
    - Executive summary
    - Strengths and weaknesses analysis
    - Recommendations for improvement
    - MITRE ATT&CK mapping insights
    - Competency gap identification
    """
    aar = db.query(AfterActionReport).filter(AfterActionReport.exercise_id == exercise_id).first()
    if not aar:
        raise HTTPException(404, "AAR not found — generate the base report first")

    report_data = json.loads(aar.report_json)
    # Was pointed at :8000 /generate with a task_type/temperature body and read
    # ai_result["text"] — four mismatches against the orchestrator, so this could
    # never succeed. The real route is :6000 /ai/aar-analysis, takes
    # {report_data, context}, and returns its text in "output".
    ai_orchestrator_url = os.getenv("AI_ORCHESTRATOR_URL", "http://ai-orchestrator:6000")

    try:
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{ai_orchestrator_url}/ai/aar-analysis",
                json={
                    "report_data": json.dumps(report_data)[:50000],
                    "context": {"exercise_id": str(exercise_id)},
                },
            )
            resp.raise_for_status()
            ai_result = resp.json()

        ai_analysis = ai_result.get("output", "")
        ai_model = ai_result.get("model_used", "unknown")

        # Merge AI analysis into the report
        report_data["ai_analysis"] = {
            "summary": ai_analysis,
            "model": ai_model,
            "generated_at": datetime.now(UTC).isoformat(),
        }
        aar.report_json = json.dumps(report_data, indent=2)

        # Enhance HTML with AI section
        ai_html = (
            f'<div class="ai-analysis">'
            f"<h2>AI-Powered Analysis</h2>"
            f'<div class="analysis-content">{_md_to_html(ai_analysis)}</div>'
            f'<p class="ai-meta">Generated by {ai_model}</p>'
            f"</div>"
        )
        if aar.report_html:
            aar.report_html = aar.report_html.replace("</body>", f"{ai_html}</body>")

        db.commit()
        db.refresh(aar)
        logger.info("AI-enhanced AAR for exercise %s", exercise_id)
        return aar

    except httpx.HTTPError as exc:
        logger.warning("AI orchestrator unavailable: %s", exc)
        raise HTTPException(503, "AI orchestrator is unavailable. Try again later.") from exc
    except Exception as exc:
        logger.error("AI AAR enhancement failed: %s", exc, exc_info=True)
        raise HTTPException(500, "AI analysis failed") from exc


def _build_aar_prompt(report: dict) -> str:
    """Build a structured prompt for AAR analysis."""
    ex = report.get("exercise", {})
    scores = report.get("scores", {})
    objectives = report.get("objectives", [])

    achieved = [o for o in objectives if o.get("achieved")]
    missed = [o for o in objectives if not o.get("achieved")]

    return (
        f"You are a cybersecurity training analyst reviewing an After-Action Report.\n\n"
        f"Exercise: {ex.get('name', 'Unknown')}\n"
        f"Score: {scores.get('pct', 0)}% ({scores.get('total', 0)}/{scores.get('max', 0)} points)\n\n"
        f"Achieved Objectives ({len(achieved)}):\n"
        + "\n".join(f"- [{o.get('type', '')}] {o.get('description', '')}" for o in achieved)
        + f"\n\nMissed Objectives ({len(missed)}):\n"
        + "\n".join(f"- [{o.get('type', '')}] {o.get('description', '')} ({o.get('points', 0)} pts)" for o in missed)
        + "\n\nProvide:\n"
        "1. Executive summary (2-3 sentences)\n"
        "2. Key strengths demonstrated\n"
        "3. Areas for improvement with specific recommendations\n"
        "4. MITRE ATT&CK technique coverage analysis\n"
        "5. Suggested follow-up training exercises\n"
    )


def _md_to_html(text: str) -> str:
    """Minimal Markdown-to-HTML for AI output."""
    import re

    text = re.sub(r"^### (.+)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)
    text = re.sub(r"^## (.+)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)
    text = re.sub(r"^# (.+)$", r"<h2>\1</h2>", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"^- (.+)$", r"<li>\1</li>", text, flags=re.MULTILINE)
    text = re.sub(r"(<li>.*</li>)", r"<ul>\1</ul>", text, flags=re.DOTALL)
    text = text.replace("\n\n", "</p><p>")
    return f"<p>{text}</p>"
