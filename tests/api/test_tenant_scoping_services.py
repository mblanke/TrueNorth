"""Second-tenant behaviour for the leaks the widened scoping guard found outside routers
(security sweep M4, 2026-10-08).

The QSP spine (qualifications, POs) is platform-global, so every lookup keyed on a PO
reached every tenant's courses: one tenant's generation renamed or reused another's
course, overwrote another's scenarios, deleted another's ranges, and a content import
superseded another tenant's placeholder. Enrollment followed a learning path's JSON
course list into any tenant (tests in test_enrollment.py). rbac.require_range_access let
any tenant's admin through.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from app import qsp_paths
from app.auth import CurrentUser
from app.course_content_ingest import _claim_po
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
    Tenant,
    UserRole,
)
from app.rbac import require_range_access
from fastapi import HTTPException

TENANT_A = "00000000-0000-0000-0000-000000000001"  # seeded by conftest
TENANT_B = uuid.UUID("00000000-0000-0000-0000-0000000000c4")


@pytest.fixture
def spine(db_session):
    db_session.add(Tenant(id=TENANT_B, name="Unit B", slug=f"unit-b-{uuid.uuid4().hex[:6]}"))
    qual = Qualification(qsp_code=f"Q{uuid.uuid4().hex[:6]}", nqual="Q", title="Analyst")
    db_session.add(qual)
    db_session.flush()
    po = PerformanceObjective(qualification_id=qual.id, po_code="PO_901", title="Triage alerts")
    db_session.add(po)
    db_session.flush()
    return po


def _course_delivering(db, po, tenant, *, authored: bool, name: str) -> Course:
    meta = {"provenance": "authored"} if authored else {"po_code": po.po_code}
    c = Course(name=name, tenant_id=tenant, course_meta=json.dumps(meta))
    db.add(c)
    db.flush()
    db.add(CourseModule(course_id=c.id, ordinal=0, title="m", content_type=ModuleContentType.scenario, po_id=po.id))
    db.flush()
    return c


def test_learning_path_generation_never_takes_another_tenants_course(db_session, spine):
    theirs = _course_delivering(db_session, spine, TENANT_B, authored=True, name="B's authored course")
    stub = _course_delivering(db_session, spine, TENANT_B, authored=False, name="B's stub")

    qsp_paths.generate_learning_paths(db_session, tenant_id=TENANT_A)

    db_session.expire_all()
    assert db_session.get(Course, stub.id).name == "B's stub"  # not renamed by A's run
    mine = (
        db_session.query(Course)
        .join(CourseModule, CourseModule.course_id == Course.id)
        .filter(CourseModule.po_id == spine.id, Course.tenant_id == uuid.UUID(TENANT_A))
        .all()
    )
    assert len(mine) == 1  # A got its own course for the PO
    for lp in db_session.query(LearningPath).filter(LearningPath.tenant_id == uuid.UUID(TENANT_A)):
        ids = json.loads(lp.course_ids or "[]")
        assert str(theirs.id) not in ids and str(stub.id) not in ids


def test_exercise_generation_stays_in_the_callers_tenant(db_session, spine):
    _course_delivering(db_session, spine, TENANT_B, authored=True, name="B's course")
    t = Template(name="legacy", yaml="name: legacy", tenant_id=TENANT_B)
    db_session.add(t)
    db_session.flush()
    old = Range(name="Legacy Assessment Range", template_id=t.id, tenant_id=TENANT_B)
    scen = Scenario(name=f"{spine.po_code}: {spine.title}", yaml="b: own", tenant_id=TENANT_B)
    db_session.add_all([old, scen])
    db_session.flush()

    stats = qsp_paths.generate_exercises(db_session, tenant_id=TENANT_A)

    db_session.expire_all()
    assert db_session.get(Range, old.id) is not None  # A's run deleted B's range
    assert db_session.get(Scenario, scen.id).yaml == "b: own"  # ... and rewrote B's scenario
    assert stats["exercises"] == 0  # A has no module for the PO; B's is not A's to scaffold
    assert db_session.query(Exercise).filter(Exercise.tenant_id == uuid.UUID(TENANT_A)).count() == 0


def test_a_content_import_never_supersedes_another_tenants_placeholder(db_session, spine):
    theirs = _course_delivering(db_session, spine, TENANT_B, authored=False, name="B's stub")
    mine = Course(name="A's course", tenant_id=uuid.UUID(TENANT_A))
    db_session.add(mine)
    db_session.flush()
    module = CourseModule(course_id=mine.id, ordinal=1, title="m", content_type=ModuleContentType.scenario)
    module.po_id = spine.id
    db_session.add(module)
    db_session.flush()

    stats: dict = {}
    _claim_po(db_session, module, "A-101", 1, stats, tenant_id=mine.tenant_id)

    db_session.flush()
    assert db_session.get(Course, theirs.id) is not None
    their_module = db_session.query(CourseModule).filter(CourseModule.course_id == theirs.id).one()
    assert their_module.po_id == spine.id
    assert stats.get("placeholders_superseded", 0) == 0


def test_require_range_access_only_lets_the_platform_admin_cross(db_session, monkeypatch):
    db_session.add(Tenant(id=TENANT_B, name="Unit B", slug=f"unit-b-{uuid.uuid4().hex[:6]}"))
    db_session.flush()
    t = Template(name="t", yaml="name: t", tenant_id=TENANT_B)
    db_session.add(t)
    db_session.flush()
    rng = Range(name="r", template_id=t.id, tenant_id=TENANT_B)
    db_session.add(rng)
    db_session.flush()
    check = require_range_access()
    admin_a = CurrentUser(id=str(uuid.uuid4()), email="a@x.test", display_name="a", role=UserRole.admin,
                          tenant_id=TENANT_A, keycloak_id="kc-a")

    monkeypatch.setenv("PLATFORM_TENANT_ID", str(TENANT_B))  # A's admin is not the operator
    with pytest.raises(HTTPException) as refused:
        asyncio.run(check(range_id=rng.id, user=admin_a, db=db_session))
    assert refused.value.status_code == 403

    monkeypatch.setenv("PLATFORM_TENANT_ID", TENANT_A)
    assert asyncio.run(check(range_id=rng.id, user=admin_a, db=db_session)) is admin_a
