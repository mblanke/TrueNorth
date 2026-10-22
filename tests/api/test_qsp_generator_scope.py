"""Issue #257: the QSP generators read only qualifications the caller's tenant may read.

``POST /qsp/generate-learning-paths`` built courses and paths for the caller's tenant
from every tenant's qualifications, and ``POST /qsp/generate-exercises`` resolved a
module's objective's qualification with no tenant check. An author could thereby copy
another tenant's curriculum into their own courses. Visible = own tenant + shared
(``tenant_id IS NULL``), the rule ``app.qsp_scope`` holds for every QSP read.
"""

from __future__ import annotations

import json
import uuid

import pytest
from _shared import act_as, real_tenant, real_user
from app import qsp_scope
from app.models import (
    Course,
    CourseModule,
    Exercise,
    LearningPath,
    ModuleContentType,
    PerformanceObjective,
    Qualification,
    Range,
    Scenario,
    Template,
    UserRole,
)

AUTHOR_ROLES = [UserRole.instructor, UserRole.admin]  # admin: no bypass across tenants


def _qual(db, tenant_id, label: str, track: str = "specialty") -> tuple[Qualification, PerformanceObjective]:
    code = f"{label.upper()}{uuid.uuid4().hex[:5]}"
    q = Qualification(qsp_code=code, nqual=f"N-{code}", title=f"{label} qualification",
                      tenant_id=tenant_id, track=track, dp_order=2)
    db.add(q)
    db.flush()
    po = PerformanceObjective(
        qualification_id=q.id, po_code=f"PO_{label.upper()}", title=f"{label} objective text",
        target_role=f"{label} role", critical_events=json.dumps(["scanning"]),
    )
    db.add(po)
    db.flush()
    return q, po


@pytest.fixture
def world(db_session):
    a, b = real_tenant(db_session), real_tenant(db_session)
    own = _qual(db_session, a.id, "own")
    shared = _qual(db_session, None, "shared", track="progression")
    foreign = _qual(db_session, b.id, "foreign")
    return {"a": a, "b": b, "own": own, "shared": shared, "foreign": foreign}


def _tenant_text(db, tenant_id) -> str:
    """Everything the generators write for a tenant, as one searchable blob."""
    parts: list[str] = []
    for c in db.query(Course).filter(Course.tenant_id == tenant_id):
        parts += [c.name, c.course_meta or ""]
    for lp in db.query(LearningPath).filter(LearningPath.tenant_id == tenant_id):
        parts += [lp.name, lp.description or ""]
    for s in db.query(Scenario).filter(Scenario.tenant_id == tenant_id):
        parts += [s.name, s.yaml]
    for e in db.query(Exercise).filter(Exercise.tenant_id == tenant_id):
        parts.append(e.name)
    for t in db.query(Template).filter(Template.tenant_id == tenant_id):
        parts += [t.name, t.yaml]
    for r in db.query(Range).filter(Range.tenant_id == tenant_id):
        parts.append(r.name)
    return "\n".join(parts)


def _modules_for(db, tenant_id, po) -> list[CourseModule]:
    return (
        db.query(CourseModule)
        .join(Course, Course.id == CourseModule.course_id)
        .filter(Course.tenant_id == tenant_id, CourseModule.po_id == po.id)
        .all()
    )


@pytest.mark.parametrize("role", AUTHOR_ROLES)
def test_learning_paths_use_own_and_shared_qualifications_only(client, db_session, world, role):
    act_as(real_user(db_session, role, world["a"].id))

    r = client.post("/qsp/generate-learning-paths")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["generated"] is True and body["po_courses"] == 2  # own + shared, not foreign
    db_session.expire_all()
    tid = world["a"].id
    assert _modules_for(db_session, tid, world["own"][1]) and _modules_for(db_session, tid, world["shared"][1])
    assert _modules_for(db_session, tid, world["foreign"][1]) == []
    fq, fpo = world["foreign"]
    text = _tenant_text(db_session, tid)
    for leaked in (fq.qsp_code, fq.title, fpo.title, fpo.target_role):
        assert leaked not in text
    names = {lp.name for lp in db_session.query(LearningPath).filter(LearningPath.tenant_id == tid)}
    assert f"Role: {world['own'][1].target_role}" in names
    assert f"Role: {world['shared'][1].target_role}" in names
    assert f"Role: {fpo.target_role}" not in names


def test_the_other_tenant_sees_its_own_and_shared_but_not_the_first(client, db_session, world):
    act_as(real_user(db_session, UserRole.instructor, world["b"].id))

    assert client.post("/qsp/generate-learning-paths").status_code == 200

    db_session.expire_all()
    tid = world["b"].id
    assert _modules_for(db_session, tid, world["foreign"][1])  # B's own qualification
    assert _modules_for(db_session, tid, world["shared"][1])
    assert _modules_for(db_session, tid, world["own"][1]) == []  # A's is not B's to copy
    assert world["own"][0].qsp_code not in _tenant_text(db_session, tid)


@pytest.mark.parametrize("role", AUTHOR_ROLES)
def test_exercises_skip_a_module_bound_to_another_tenants_objective(client, db_session, world, role):
    act_as(real_user(db_session, role, world["a"].id))
    assert client.post("/qsp/generate-learning-paths").status_code == 200
    # A module in A's tenant bound to B's objective, as a run before the fix left behind.
    fq, fpo = world["foreign"]
    poisoned = Course(name="pre-fix leftover", tenant_id=world["a"].id)
    db_session.add(poisoned)
    db_session.flush()
    db_session.add(CourseModule(course_id=poisoned.id, ordinal=0, title="m",
                                content_type=ModuleContentType.scenario, po_id=fpo.id))
    db_session.flush()

    r = client.post("/qsp/generate-exercises")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped_foreign_objectives"] == 1
    assert body["exercises"] == 2  # own + shared still scaffold
    db_session.expire_all()
    text = _tenant_text(db_session, world["a"].id)
    for leaked in (fq.qsp_code, fpo.po_code, fpo.title):
        assert leaked not in text
    assert world["own"][1].po_code in text and world["shared"][1].po_code in text


def test_no_tenant_sees_shared_qualifications_only(db_session, world):
    codes = {q.qsp_code for q in qsp_scope.visible_qualifications(db_session, None)}
    assert world["shared"][0].qsp_code in codes
    assert not codes & {world["own"][0].qsp_code, world["foreign"][0].qsp_code}


def test_visible_objectives_follow_visible_qualifications(db_session, world):
    po_ids = {po.id for po in qsp_scope.visible_objectives(db_session, str(world["a"].id))}
    assert {world["own"][1].id, world["shared"][1].id} <= po_ids
    assert world["foreign"][1].id not in po_ids


@pytest.mark.parametrize("path", ["/qsp/generate-learning-paths", "/qsp/generate-exercises"])
def test_students_still_cannot_generate(client, db_session, world, path):
    act_as(real_user(db_session, UserRole.student, world["a"].id))
    assert client.post(path).status_code == 403
