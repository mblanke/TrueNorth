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
POST   /exercises/{id}/resume              EXERCISE_PAUSE
POST   /exercises/{id}/complete            EXERCISE_COMPLETE
GET    /exercises/{id}/objectives           EXERCISE_READ
GET    /exercises/{id}/injects              EXERCISE_READ
POST   /exercises/{id}/objectives/{ref}/ack OBJECTIVE_ACK
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

from .. import safe_yaml, scenario_objectives
from ..aar_html import RESPONSE_HEADERS as AAR_PAGE_HEADERS
from ..aar_html import pdf_text
from ..aar_html import render_html as render_aar_html
from ..aar_report import build_report as build_aar_report
from ..ai_orchestrator_client import orchestrator_headers
from ..auth import CurrentUser
from ..db import get_db
from ..detections.redaction import redact_evidence, redact_timeline, sees_answer_key
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
from ..scenario_runs import InjectRecord
from ..scenario_runs import runs as scenario_run_leases
from ..scenario_runs.schemas import InjectRecordOut
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
    db.add(
        AuditLog(
            user_id=uuid.UUID(user.id),
            tenant_id=uuid.UUID(user.tenant_id),
            action=action,
            resource_type=rtype,
            resource_id=rid,
            detail=detail,
        )
    )


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
            parsed = safe_yaml.load(scenario.yaml) or {}
            if isinstance(parsed, dict) and isinstance(parsed.get("timeline"), list):
                definition["timeline"] = parsed["timeline"]
        except yaml.YAMLError:
            pass
    objectives = db.query(Objective).filter(Objective.exercise_id == ex.id).all()
    definition["objectives"] = [{"ref_id": o.ref_id, "validator": o.validator, "points": o.points} for o in objectives]
    # No timeline, no injects. (This used to synthesize an `inject.<ref_id>` event per
    # objective so the run "walked"; now that events are really dispatched, those fake
    # actions would only be recorded as failed injects.)
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
    db.flush()
    # The scenario's objectives are what the exercise is scored on, and out of their points
    # (they were never made here, so a scenario exercise scored 0/0).
    points = scenario_objectives.materialise(db, ex.id, sc.yaml)
    if points:
        ex.max_score = points
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
    lease = scenario_run_leases.begin(db, ex.id)
    db.commit()
    db.refresh(ex)
    _audit(db, user, "start", "exercise", str(ex.id))
    db.commit()
    # Dispatch the scenario runner (mock mode walks the timeline + auto-achieves objectives).
    definition = _scenario_definition(db, ex, user)
    _dispatch_task("run_scenario_v2", str(ex.id), definition, lease)
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
            parsed = safe_yaml.load(scenario.yaml) or {}
        except yaml.YAMLError:
            parsed = {}
    objectives = db.query(Objective).filter(Objective.exercise_id == ex.id).order_by(Objective.ref_id).all()
    key = sees_answer_key(user)  # Students get the briefing, not the answer key (ADR 0005 §5)
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
        "timeline": parsed.get("timeline", []) if key else redact_timeline(parsed.get("timeline")),
        "noise_floor": parsed.get("noise_floor", []),
        "objectives": [
            {
                "ref_id": o.ref_id,
                "type": o.objective_type.value,
                "points": o.points,
                "achieved": o.achieved,
                "evidence": (o.evidence if key else redact_evidence(o.evidence)) or "",
                "validator": o.validator,
                "competency_code": o.competency_code or "",
            }
            for o in objectives
        ],
    }


@router.post("/{exercise_id}/run", response_model=ExerciseOut)
def run_exercise(
    exercise_id: uuid.UUID = Path(...),
    reset: bool = Query(False, description="replay a completed or cancelled exercise: clears every objective and the score"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_START)),
) -> Exercise:
    """One-click: provision the range (mock, if needed) then start the run. **Permission: exercise:start**

    A completed or cancelled exercise is replayed only with ``reset=true``: replay wipes
    everyone's objectives and score, so it is never a side effect of pressing Run (409)."""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    if ex.state in (ExerciseState.completed, ExerciseState.cancelled) and not reset:
        raise HTTPException(409, f"Exercise is {ex.state.value}; replaying it clears every objective (reset=true)")
    # Replay: reset a finished/cancelled exercise back to pending before re-running.
    if ex.state in (ExerciseState.completed, ExerciseState.cancelled):
        for obj in db.query(Objective).filter(Objective.exercise_id == ex.id).all():
            obj.achieved = False
            obj.achieved_at = None
            obj.evidence = None  # the last run's credit; its submissions stay on record
        ex.state = ExerciseState.pending
        ex.total_score = 0
        ex.completed_at = None
        db.commit()
    if ex.state != ExerciseState.pending:
        raise HTTPException(409, f"Exercise is {ex.state.value}, expected pending")
    # provision the range if it hasn't been (mock provisioner flips it to ready via the worker),
    # as a recorded range operation (app/range_ops): durable, and re-sent if the broker is down.
    rng = get_owned(db, Range, ex.range_id, user)
    if rng and rng.state.can_transition_to(RangeState.provisioning):
        from ..range_ops import service as range_ops

        op, _, _ = range_ops.accept(db, rng.id, user, "provision")
        db.commit()
        range_ops.dispatch(db, op)
    # start the exercise + dispatch the scenario runner
    ex.state = ExerciseState.running
    ex.started_at = datetime.now(UTC)
    lease = scenario_run_leases.begin(db, ex.id)
    db.commit()
    db.refresh(ex)
    _audit(db, user, "run", "exercise", str(ex.id))
    db.commit()
    definition = _scenario_definition(db, ex, user)
    _dispatch_task("run_scenario_v2", str(ex.id), definition, lease)
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


@router.post("/{exercise_id}/resume", response_model=ExerciseOut)
def resume_exercise(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_PAUSE)),
) -> Exercise:
    """Resume a paused exercise.  **Permission: exercise:pause**

    The scenario run continues from the next event it has not fired: a new worker task
    takes over the run (app/scenario_runs/runs.py), so no inject fires twice. Only a
    paused exercise can be resumed (409 otherwise, e.g. once completed or cancelled).
    """
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    # Conditional, so two resumes (or a resume racing a complete) cannot both win.
    moved = (
        db.query(Exercise)
        .filter(Exercise.id == ex.id, Exercise.state == ExerciseState.paused)
        .update({Exercise.state: ExerciseState.running}, synchronize_session=False)
    )
    if not moved:  # nothing was written
        db.refresh(ex)
        raise HTTPException(409, f"Exercise is {ex.state.value}, expected paused")
    lease = scenario_run_leases.resume(db, ex.id, ex.started_at)
    db.commit()
    db.refresh(ex)
    _audit(db, user, "resume", "exercise", str(ex.id))
    db.commit()
    _dispatch_task("run_scenario_v2", str(ex.id), _scenario_definition(db, ex, user), lease)
    return ex


@router.post("/{exercise_id}/complete", response_model=ExerciseOut)
async def complete_exercise(
    exercise_id: uuid.UUID = Path(...),
    background_tasks: BackgroundTasks = None,  # type: ignore[assignment]
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_COMPLETE)),
) -> Exercise:
    """Complete an exercise and tally scores.  **Permission: exercise:complete**

    Competency assessment, the LTI grade and the xAPI statement go to whoever completed it
    and to every Student who submitted a detection in this run (app/exercise_completion).
    """
    from ..exercise_completion import Participant, close

    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    closer = Participant(uuid.UUID(user.id), user.email or "", user.display_name)
    closed = close(db, ex.id, background_tasks, closer)
    if closed is None:
        db.refresh(ex)
        raise HTTPException(409, f"Exercise is {ex.state.value}, cannot complete")
    _audit(db, user, "complete", "exercise", str(closed.id))
    db.commit()
    return closed


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
    """List objectives for an exercise.  **Permission: exercise:read**

    Own-tenant exercises only; a foreign exercise id is 404 (until 2026-10-07 this
    listed any tenant's objectives, validators and evidence by exercise id). Students
    get evidence with the answer key redacted (ADR 0005)."""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    rows = db.query(Objective).filter(Objective.exercise_id == ex.id).all()
    if sees_answer_key(user):
        return rows
    return [ObjectiveOut.model_validate(o).model_copy(update={"evidence": redact_evidence(o.evidence)}) for o in rows]


# ── Injects ────────────────────────────────────────────────────────────
@router.get("/{exercise_id}/injects", response_model=list[InjectRecordOut])
def list_injects(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_READ)),
) -> list[InjectRecord] | list[InjectRecordOut]:
    """What each inject did (timeline and instructor), oldest first, every run kept.
    **Permission: exercise:read**

    Users without scenario:update (Students) get only when and whether each inject ran:
    action, detail, MITRE technique, mode and telemetry counts are the answer key (ADR 0005)."""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    rows = (
        db.query(InjectRecord)
        .filter(InjectRecord.exercise_id == ex.id)
        .order_by(InjectRecord.created_at, InjectRecord.seq)
        .all()
    )
    if sees_answer_key(user):
        return rows
    return [
        InjectRecordOut(
            id=r.id, source=r.source, run_id=r.run_id, seq=r.seq, t=r.t, action="", status=r.status,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/{exercise_id}/objectives/{ref_id}/ack", response_model=ObjectiveOut)
async def acknowledge_objective(
    exercise_id: uuid.UUID = Path(...),
    ref_id: str = Path(...),
    body: ObjectiveAck = Depends(),
    background_tasks: BackgroundTasks = None,  # type: ignore[assignment]
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.OBJECTIVE_ACK)),
) -> Objective:
    """Acknowledge (achieve) an objective on a running or paused exercise in the caller's
    tenant, recording who acknowledged it, and re-total the exercise score.

    **Permission: objective:ack** (instructors and admins; never Students).
    404 if the exercise is not in the caller's tenant or the objective does not exist;
    409 if the exercise is not running/paused or the objective is already achieved.
    """
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    if ex.state not in (ExerciseState.running, ExerciseState.paused):
        raise HTTPException(409, f"Exercise is {ex.state.value}, expected running or paused")
    obj = db.query(Objective).filter(Objective.exercise_id == ex.id, Objective.ref_id == ref_id).first()
    if not obj:
        raise HTTPException(404, "Objective not found")
    if obj.achieved:
        raise HTTPException(409, "Objective already achieved")
    who = user.display_name or user.email or user.id
    obj.achieved = True
    obj.evidence = f"Acknowledged by {who}: {body.evidence}" if body.evidence else f"Acknowledged by {who}"
    obj.achieved_at = datetime.now(UTC)
    db.flush()
    # Same tally as complete_exercise, so the live score is right before completion.
    objectives = db.query(Objective).filter(Objective.exercise_id == ex.id).all()
    ex.total_score = sum(o.points for o in objectives if o.achieved)
    _audit(db, user, "ack_objective", "exercise", str(ex.id), detail=obj.ref_id)
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
# Every AAR route resolves the exercise through get_owned first: the report row has no
# tenant of its own, so a lookup by exercise id alone served any tenant's report.
def _owned_aar(db: Session, exercise_id: uuid.UUID, user: CurrentUser) -> AfterActionReport:
    """The stored AAR of the caller's exercise, or 404 (unknown, foreign or not generated)."""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    aar = db.query(AfterActionReport).filter(AfterActionReport.exercise_id == ex.id).first()
    if not aar:
        raise HTTPException(404, "AAR not found — generate it first")
    return aar


def _report_data(aar: AfterActionReport) -> dict:
    try:
        data = json.loads(aar.report_json) if aar.report_json else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}



@router.post("/{exercise_id}/aar/generate", response_model=AAROut, status_code=201)
def generate_aar(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.AAR_GENERATE)),
) -> AfterActionReport:
    """Generate After-Action Report.  **Permission: aar:generate**"""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    sc = db.query(Scenario).filter(Scenario.id == ex.scenario_id, Scenario.tenant_id == ex.tenant_id).first()
    report = build_aar_report(db, ex, sc, user.display_name)
    report_json = json.dumps(report, indent=2)
    html = render_aar_html(report)

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
    return _owned_aar(db, exercise_id, user)


@router.get("/{exercise_id}/aar/html", response_class=HTMLResponse)
def get_aar_html(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.AAR_READ)),
) -> HTMLResponse:
    """Retrieve AAR as rendered HTML.  **Permission: aar:read**

    Rendered from the stored report JSON on every read, so reports written by the worker
    or before this renderer existed get the full page and are escaped the same way.
    """
    aar = _owned_aar(db, exercise_id, user)
    return HTMLResponse(content=render_aar_html(_report_data(aar)), headers=AAR_PAGE_HEADERS)


@router.get("/{exercise_id}/aar/pdf")
def get_aar_pdf(
    exercise_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.AAR_READ)),
) -> StreamingResponse:
    """Retrieve AAR as downloadable PDF.  **Permission: aar:read**

    Renders the stored AAR JSON to a PDF using fpdf2 (pure Python, no C deps). Its core
    fonts are latin-1 only; text outside it is transliterated (``aar_html.pdf_text``).
    """
    from io import BytesIO

    aar = _owned_aar(db, exercise_id, user)

    try:
        from fpdf import FPDF
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(500, "PDF rendering dependency unavailable") from exc

    data = _report_data(aar)
    _s = pdf_text

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    # Header
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, "After-Action Report", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)

    ex = data.get("exercise") if isinstance(data.get("exercise"), dict) else {}
    pdf.cell(0, 7, _s(f"Exercise: {ex.get('name', 'Unknown')}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, _s(f"State: {ex.get('state', '-')}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, _s(f"Started: {ex.get('started_at') or '-'}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, _s(f"Completed: {ex.get('completed_at') or '-'}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, _s(f"Generated: {data.get('generated_at', '-')}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Scores
    scores = data.get("scores") if isinstance(data.get("scores"), dict) else {}
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
    def _rows(key: str) -> list[dict]:
        value = data.get(key)
        return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []

    def _section(title: str, lines: list[str]) -> None:
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, _s(title), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        for line in lines or ["(none recorded)"]:
            pdf.multi_cell(0, 6, _s(line), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

    objectives = _rows("objectives")
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, _s(f"Objectives ({len(objectives)})"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    for obj in objectives:
        status = "[x]" if obj.get("achieved") else "[ ]"
        line = f"{status} [{obj.get('ref_id', '?')}] ({obj.get('points', 0)} pts) {obj.get('description', '')}"
        pdf.multi_cell(0, 6, _s(line), new_x="LMARGIN", new_y="NEXT")
        if obj.get("evidence"):
            pdf.set_font("Helvetica", "I", 9)
            pdf.multi_cell(0, 5, _s(f"     Evidence: {obj['evidence']}"), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 10)
    pdf.ln(4)

    _section(
        "Injects", [f"{i.get('at') or ''}  {i.get('title') or ''}  {i.get('status') or ''}" for i in _rows("injects")]
    )
    _section(
        "Timeline", [f"{t.get('at') or ''}  {t.get('title') or ''}  {t.get('detail') or ''}" for t in _rows("timeline")]
    )
    _section(
        "Participants",
        [" - ".join(str(p.get(k)) for k in ("name", "team", "role") if p.get(k)) for p in _rows("participants")],
    )

    # AI analysis if present
    ai = data.get("ai_analysis")
    if isinstance(ai, dict) and ai.get("summary"):
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, "AI Analysis", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 5, _s(str(ai["summary"])[:4000]), new_x="LMARGIN", new_y="NEXT")

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
    aar = _owned_aar(db, exercise_id, user)
    report_data = _report_data(aar)
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
                headers=orchestrator_headers(),
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

        # The renderer escapes the model's output like everything else in the report.
        aar.report_html = render_aar_html(report_data)

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
