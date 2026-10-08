"""TrueNorth Range — scenario execution: run a scenario's timeline against a range, no exercise.

The worker (``run_scenario_execution``) fires each timeline event through the same inject
dispatch an exercise uses and records every outcome in ``inject_records``. Objectives are
reported, never scored: an execution has no Students and no evidence review, so they stay
``unassessed`` (docs/scenario-inject-execution.md).

=================================================  ==========================
Endpoint                                           Permission(s)
=================================================  ==========================
POST   /scenarios/execute                          SCENARIO_UPDATE + EXERCISE_START
GET    /scenarios/executions/{id}/results          EXERCISE_READ
GET    /scenarios/executions/{id}/timeline         EXERCISE_READ (answer key: staff)
=================================================  ==========================

Executing fires host-affecting injects, so it is staff-only (``scenario:update``; Students
hold ``exercise:start`` for their own exercises) and refuses a lab session's range, which
only its session drives. The timeline's ``action`` / ``detail`` / ``execution_mode`` /
``mitre_technique`` are the answer key (ADR 0005 §5): anyone else sees ``seq``, ``t`` and
``status`` of events already recorded, and nothing of pending ones.
"""

from __future__ import annotations

import logging
import uuid

import yaml
from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.orm import Session

from .. import range_lifecycle
from ..auth import CurrentUser
from ..db import get_db
from ..detections.redaction import sees_answer_key
from ..models import AuditLog, Range, RangeState, Scenario
from ..rbac import Permission, require_permission
from ..scenario_runs import InjectRecord, ScenarioExecution
from ..scenario_runs.schemas import (
    InjectCounts,
    ObjectiveResultOut,
    ScenarioExecuteIn,
    ScenarioExecutionOut,
    ScenarioExecutionResultsOut,
    TimelineEntryOut,
)
from ..tenancy import get_owned

logger = logging.getLogger("truenorth.api.scenario_executions")

router = APIRouter(prefix="/scenarios", tags=["scenarios"])

RUNNABLE_RANGE_STATES = (RangeState.ready, RangeState.running)


def _dispatch(task_name: str, *args) -> str | None:
    from ..celery_client import dispatch

    return dispatch(task_name, *args)


def _definition(sc: Scenario) -> dict:
    """{"timeline": [...], "objectives": [...]} from the scenario YAML; 422 if it is not a mapping."""
    try:
        parsed = yaml.safe_load(sc.yaml or "") or {}
    except yaml.YAMLError as exc:
        raise HTTPException(422, f"Scenario YAML does not parse: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(422, "Scenario YAML must be a mapping")
    timeline = parsed.get("timeline") or []
    if not isinstance(timeline, list):
        raise HTTPException(422, "Scenario timeline must be a list")
    objectives = []
    for o in parsed.get("objectives") or []:
        if isinstance(o, dict) and (o.get("ref_id") or o.get("id")):
            objectives.append(
                {"ref_id": str(o.get("ref_id") or o.get("id")), "description": str(o.get("description", ""))}
            )
    return {"timeline": timeline, "objectives": objectives}


@router.post("/execute", response_model=ScenarioExecutionOut, status_code=202)
def execute_scenario(
    body: ScenarioExecuteIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.SCENARIO_UPDATE, Permission.EXERCISE_START)),
) -> ScenarioExecution:
    """Run a scenario's timeline against a ready range.  **Permission: scenario:update + exercise:start**

    202: queued. 403: not staff. 404: scenario or range not in your tenant. 409: the range
    is not ready, or it belongs to a Student's lab session. 422: the YAML is not a mapping
    or its timeline is not a list. 503: the worker broker is down (the execution is
    recorded ``failed``).
    """
    sc = get_owned(db, Scenario, body.scenario_id, user, not_found="Scenario not found")
    rng = get_owned(db, Range, body.range_id, user, not_found="Range not found")
    if rng.state not in RUNNABLE_RANGE_STATES:
        raise HTTPException(409, f"Range is {rng.state.value}; a scenario runs on a ready or running range")
    if range_lifecycle.is_lab_range(db, rng.id):
        raise HTTPException(409, "This range belongs to a student's lab session; run the scenario on another range")
    definition = _definition(sc)
    x = ScenarioExecution(
        tenant_id=uuid.UUID(user.tenant_id),
        scenario_id=sc.id,
        scenario_name=sc.name,
        range_id=rng.id,
        state="pending",
        definition=definition,
        requested_by=uuid.UUID(user.id),
    )
    db.add(x)
    db.add(
        AuditLog(
            user_id=uuid.UUID(user.id),
            tenant_id=uuid.UUID(user.tenant_id),
            action="execute",
            resource_type="scenario",
            resource_id=str(sc.id),
        )
    )
    db.commit()
    task_id = _dispatch("run_scenario_execution", str(x.id), definition)
    if task_id is None:
        x.state = "failed"
        x.error = "not queued: the worker broker is unavailable"
        db.commit()
        raise HTTPException(503, "Scenario execution not queued: the worker broker is unavailable")
    x.task_id = task_id
    db.commit()
    db.refresh(x)
    return x


def _owned_execution(db: Session, execution_id: uuid.UUID, user: CurrentUser) -> ScenarioExecution:
    return get_owned(db, ScenarioExecution, execution_id, user, not_found="Scenario execution not found")


def _timeline(db: Session, x: ScenarioExecution) -> list[TimelineEntryOut]:
    recorded: dict[int, InjectRecord] = {}
    for rec in (
        db.query(InjectRecord)
        .filter(InjectRecord.execution_id == x.id)
        .order_by(InjectRecord.created_at, InjectRecord.id)
        .all()
    ):
        if rec.seq is not None:
            recorded[rec.seq] = rec  # the latest outcome for each position
    entries = []
    for seq, event in enumerate((x.definition or {}).get("timeline") or []):
        ev = event if isinstance(event, dict) else {}
        rec = recorded.get(seq)
        entries.append(
            TimelineEntryOut(
                seq=seq,
                t=str(ev.get("t")) if ev.get("t") is not None else (rec.t if rec else None),
                action=rec.action if rec else str(ev.get("action") or "invalid"),
                status=rec.status if rec else "pending",
                detail=rec.detail if rec else "",
                execution_mode=rec.execution_mode if rec else None,
                mitre_technique=rec.mitre_technique if rec else None,
                telemetry_count=rec.telemetry_count if rec else 0,
                recorded_at=rec.created_at if rec else None,
            )
        )
    return entries


@router.get(
    "/executions/{execution_id}/timeline",
    response_model=list[TimelineEntryOut],
)
def get_execution_timeline(
    execution_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_READ)),
) -> list[TimelineEntryOut]:
    """Each timeline event with its recorded outcome (``pending`` until the worker reaches it).

    Staff (``scenario:update``) see the whole entry. Anyone else sees only ``seq``, ``t``
    and ``status`` of recorded events: the action, detail and technique are the answer
    key (ADR 0005 §5), and a pending event's ``action`` comes straight from the playbook.
    """
    entries = _timeline(db, _owned_execution(db, execution_id, user))
    if sees_answer_key(user):
        return entries
    return [
        TimelineEntryOut(seq=e.seq, t=e.t, action="", status=e.status) for e in entries if e.status != "pending"
    ]


@router.get(
    "/executions/{execution_id}/results",
    response_model=ScenarioExecutionResultsOut,
)
def get_execution_results(
    execution_id: uuid.UUID = Path(...),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.EXERCISE_READ)),
) -> ScenarioExecutionResultsOut:
    """State, inject counts and objectives (always ``unassessed``) of an execution."""
    x = _owned_execution(db, execution_id, user)
    timeline = _timeline(db, x)
    counts = {s: sum(1 for e in timeline if e.status == s) for s in ("fired", "skipped", "failed", "pending")}
    return ScenarioExecutionResultsOut(
        **ScenarioExecutionOut.model_validate(x).model_dump(),
        injects=InjectCounts(total=len(timeline), **counts),
        objectives=[
            ObjectiveResultOut(ref_id=o["ref_id"], description=o.get("description", ""))
            for o in (x.definition or {}).get("objectives") or []
            if isinstance(o, dict) and o.get("ref_id")
        ],
    )
