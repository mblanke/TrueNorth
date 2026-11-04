"""Behavior ratchet for the Unclassified practical-exercise completion path."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.exercise_completion import Participant, close
from app.models import (
    Exercise,
    ExerciseState,
    Objective,
    ObjectiveType,
    Range,
    RangeState,
    Scenario,
    Template,
    Tenant,
    User,
    UserRole,
)
from app.schemas import ExerciseOut

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "docs/security/unclassified-exercise-behavior-baseline.json"
TENANT = uuid.UUID("00000000-0000-0000-0000-00000000e7e0")


class CapturedTasks:
    def __init__(self) -> None:
        self.items: list[tuple[object, tuple, dict]] = []

    def add_task(self, fn, *args, **kwargs) -> None:
        self.items.append((fn, args, kwargs))


def _world(db):
    tenant = Tenant(id=TENANT, name="Exercise DLP tenant", slug="exercise-dlp")
    db.add(tenant)
    db.flush()
    learner = User(
        id=uuid.uuid4(),
        keycloak_id=str(uuid.uuid4()),
        email=f"subject-{uuid.uuid4().hex}@subjects.invalid",
        display_name="Learner E7E00001",
        role=UserRole.student,
        tenant_id=TENANT,
        source="subject",
    )
    template = Template(name="Exercise DLP template", version="1", yaml="name: dlp\nnodes: []\n", tenant_id=TENANT)
    db.add_all((learner, template))
    db.flush()
    range_ = Range(name="Exercise DLP range", template_id=template.id, tenant_id=TENANT, state=RangeState.ready)
    scenario = Scenario(name="Exercise DLP scenario", yaml="name: dlp\n", tenant_id=TENANT)
    db.add_all((range_, scenario))
    db.flush()
    exercise = Exercise(
        name="Exercise DLP run",
        range_id=range_.id,
        scenario_id=scenario.id,
        tenant_id=TENANT,
        state=ExerciseState.running,
        started_at=datetime.now(UTC),
        max_score=10,
    )
    db.add(exercise)
    db.flush()
    objective = Objective(
        exercise_id=exercise.id,
        ref_id="OBJ-1",
        objective_type=ObjectiveType.detection,
        description="Detect the test signal",
        validator="test-only",
        points=8,
        achieved=True,
        evidence=json.dumps({"source": "synthetic-validator", "matched": True}),
        achieved_at=datetime.now(UTC),
    )
    db.add(objective)
    db.flush()
    return learner, exercise, objective


def test_unclassified_exercise_adds_no_unreviewed_readable_result_path(db_session, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")
    learner, exercise, objective = _world(db_session)
    dispatched: list[tuple[str, tuple]] = []
    from app import celery_client

    monkeypatch.setattr(
        celery_client,
        "dispatch",
        lambda name, *args: dispatched.append((name, args)) or "test-task",
    )
    tasks = CapturedTasks()
    closed = close(
        db_session,
        exercise.id,
        tasks,
        Participant(learner.id, learner.email, learner.display_name),
    )
    assert closed is not None
    db_session.refresh(objective)

    findings: set[str] = set()
    body = ExerciseOut.model_validate(closed).model_dump(mode="json")
    for field in ("total_score", "max_score"):
        if body.get(field) is not None:
            findings.add(f"api:exercise-complete.{field}")
        if getattr(closed, field) is not None:
            findings.add(f"db:exercises.{field}")
    if objective.achieved:
        findings.add("db:objectives.achieved")
    if objective.evidence:
        findings.add("db:objectives.evidence")

    for fn, args, _kwargs in tasks.items:
        if getattr(fn, "__name__", "") == "_push_exercise_lti_grade":
            findings.update(("lti:exercise-grade.score", "lti:exercise-grade.max_score"))
        if getattr(fn, "__name__", "") == "emit_statement_sync":
            result = args[0].get("result") or {}
            if "score" in result:
                findings.add("xapi:completed.result.score")
    if any(name == "auto_assess_competency" for name, _args in dispatched):
        findings.add("worker:auto-assess-competency.numeric-persistence")

    accepted = set(json.loads(BASELINE.read_text())["findings"])
    assert dispatched == [("auto_assess_competency", (str(exercise.id), str(learner.id)))]
    assert findings - accepted == set()
