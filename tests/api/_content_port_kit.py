"""Builders for the content port's tests: content, and each kind of evidence "proven" reads
(a certification record, a closed class, a completed exercise, a content clock row)."""

from __future__ import annotations

import base64
import json
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

from app.auth import CurrentUser, get_current_user
from app.classes.models import Class, ClassCloseout, ClassRange, ClassResult, RollCallEntry
from app.content_review.models import ContentReviewItem
from app.main import app as fastapi_app
from app.models import (
    ContentKind,
    Course,
    CourseModule,
    Exercise,
    ExerciseState,
    GoldenImage,
    Lesson,
    ModuleContent,
    ModuleContentType,
    Quiz,
    QuizQuestion,
    QuizQuestionType,
    Range,
    Scenario,
    Template,
    User,
    UserRole,
)
from app.range_certification import sections as sec
from app.range_certification.models import RangeCertificationEntry, RangeCertificationRecord
from app.range_certification.service import plan_hash
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEV = uuid.UUID("00000000-0000-0000-0000-000000000001")
NOW = datetime.now(UTC)

TEMPLATE_YAML = """name: Port lab
nodes:
  - id: ws01
    os: ubuntu-2404
  - id: dc01
    os: srv2022
accounts:
  - machine: ws01
    username: alice
    password: Winter2024!
    student_visible: true
    scenario: true
  - machine: dc01
    username: analyst
    generate: true
    student_visible: false
    notes: the lesson password is Winter2024!
"""


def scenario_yaml(template_ref: str) -> str:
    return f"name: Port scenario\nrange_template: {template_ref}\nobjectives: []\n"


def b64_public(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def user(db, role: UserRole = UserRole.admin, tenant: uuid.UUID = DEV) -> User:
    u = User(id=uuid.uuid4(), email=f"{role.value}-{uuid.uuid4().hex[:6]}@example.test", display_name=role.value,
             role=role, tenant_id=tenant, keycloak_id=f"kc-{uuid.uuid4().hex[:8]}")
    db.add(u)
    db.flush()
    return u


@contextmanager
def acting_as(db, role: UserRole = UserRole.admin, tenant: uuid.UUID = DEV):
    u = user(db, role, tenant)
    who = CurrentUser(id=str(u.id), email=u.email, display_name=u.display_name, role=u.role,
                      tenant_id=str(u.tenant_id), keycloak_id=u.keycloak_id)
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield u
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


# ── content ─────────────────────────────────────────────────────────────────────
def template(db, *, name: str | None = None, yaml: str = TEMPLATE_YAML, tenant=DEV, public=False) -> Template:
    t = Template(id=uuid.uuid4(), name=name or f"Port lab {uuid.uuid4().hex[:6]}", version="1.0", yaml=yaml,
                 tenant_id=tenant, is_public=public)
    db.add(t)
    db.flush()
    return t


def scenario(db, tmpl: Template, *, name: str | None = None, tenant=DEV, by_name=False) -> Scenario:
    s = Scenario(id=uuid.uuid4(), name=name or f"Port scenario {uuid.uuid4().hex[:6]}", version="1.0",
                 yaml=scenario_yaml(tmpl.name if by_name else str(tmpl.id)), tenant_id=tenant, is_public=False)
    db.add(s)
    db.flush()
    return s


def course(db, sc: Scenario | None = None, *, name: str | None = None, tenant=DEV) -> Course:
    c = Course(id=uuid.uuid4(), name=name or f"C9{uuid.uuid4().hex[:2]} Port course", description="Ported.",
               version="1.0", tenant_id=tenant, is_published=True, duration_hours=10)
    db.add(c)
    db.flush()
    m = CourseModule(id=uuid.uuid4(), course_id=c.id, ordinal=1, title="Lab module",
                     content_type=ModuleContentType.scenario, duration_minutes=60)
    db.add(m)
    db.flush()
    lesson = Lesson(id=uuid.uuid4(), title="Lab brief", body_markdown="## Do the lab", duration_minutes=10,
                    tenant_id=tenant)
    quiz = Quiz(id=uuid.uuid4(), title="Check", module_id=m.id, tenant_id=tenant)
    db.add_all([lesson, quiz])
    db.flush()
    db.add(QuizQuestion(id=uuid.uuid4(), quiz_id=quiz.id, ordinal=0, question_type=QuizQuestionType.mcq,
                        stem="Which?", options=json.dumps(["a", "b"]), correct=json.dumps([1])))
    db.add(ModuleContent(id=uuid.uuid4(), module_id=m.id, ordinal=0, content_kind=ContentKind.teach,
                         lesson_id=lesson.id))
    db.add(ModuleContent(id=uuid.uuid4(), module_id=m.id, ordinal=1, content_kind=ContentKind.check,
                         quiz_id=quiz.id))
    if sc is not None:
        db.add(ModuleContent(id=uuid.uuid4(), module_id=m.id, ordinal=2, content_kind=ContentKind.assess,
                             scenario_id=sc.id))
    db.flush()
    return c


# ── evidence ────────────────────────────────────────────────────────────────────
REQUIRED = [s.name for s in sec.SECTIONS if s.required]


def certify(db, tmpl: Template, *, tenant=DEV, status: str = sec.PASS, hash_of: str | None = None,
            builds=(("ubuntu-2404", "b-2026-10-01", "a" * 64),), opened: datetime | None = None
            ) -> RangeCertificationRecord:
    """A certification record of a range built from ``tmpl``: every required section
    ``status`` (pass: Certified for Students). ``hash_of``: the YAML the build was planned
    from (default: the template as it is now)."""
    rng = Range(id=uuid.uuid4(), name=f"range-{uuid.uuid4().hex[:6]}", template_id=tmpl.id, tenant_id=tenant)
    db.add(rng)
    db.flush()
    rec = RangeCertificationRecord(
        id=uuid.uuid4(), deployment_id=uuid.uuid4(), generation=1, tenant_id=tenant, range_id=rng.id,
        range_name=rng.name, template_id=tmpl.id, template_name=tmpl.name, template_version=tmpl.version,
        plan_hash=plan_hash(Template(yaml=hash_of)) if hash_of is not None else plan_hash(tmpl),
        plan_hash_basis="template_yaml_sha256", opened_by="system", opened_at=opened or NOW)
    db.add(rec)
    db.flush()
    for seq, name in enumerate(REQUIRED, start=1):
        detail = {}
        if name == "template_provenance":
            detail = {"vms": [{"vm": f"vm{i}", "catalogue_id": c, "build_id": b, "checksum": k, "promoted": True}
                              for i, (c, b, k) in enumerate(builds)]}
        db.add(RangeCertificationEntry(id=uuid.uuid4(), record_id=rec.id, tenant_id=tenant, seq=seq, section=name,
                                       status=status, summary=f"{name} {status}", detail=detail, source="test",
                                       actor="system"))
    db.flush()
    return rec


def closed_class(db, crs: Course, *, tmpl: Template | None = None, outcome: str = "pass", tenant=DEV,
                 closed: bool = True) -> Class:
    lead = user(db, UserRole.instructor, tenant)
    cls = Class(id=uuid.uuid4(), tenant_id=tenant, name=f"C9-{uuid.uuid4().hex[:4]}", course_id=crs.id,
                start_date=date.today() - timedelta(days=10), end_date=date.today() - timedelta(days=1),
                lead_instructor_id=lead.id, status="closed" if closed else "open")
    db.add(cls)
    db.flush()
    entry = RollCallEntry(id=uuid.uuid4(), class_id=cls.id, tenant_id=tenant, email=f"s{uuid.uuid4().hex[:6]}@x.test")
    db.add(entry)
    db.flush()
    db.add(ClassResult(id=uuid.uuid4(), tenant_id=tenant, class_id=cls.id, entry_id=entry.id, outcome=outcome))
    if closed:
        db.add(ClassCloseout(class_id=cls.id, tenant_id=tenant, closed_at=NOW))
    if tmpl is not None:
        db.add(ClassRange(id=uuid.uuid4(), tenant_id=tenant, class_id=cls.id, template_id=tmpl.id, kind="shared",
                          seat_key="shared"))
    db.flush()
    return cls


def exercise(db, tmpl: Template, sc: Scenario | None = None, *, score=(80, 100), tenant=DEV,
             state=ExerciseState.completed) -> Exercise:
    rng = Range(id=uuid.uuid4(), name=f"ex-range-{uuid.uuid4().hex[:6]}", template_id=tmpl.id, tenant_id=tenant)
    db.add(rng)
    db.flush()
    ex = Exercise(id=uuid.uuid4(), name=f"Exercise {uuid.uuid4().hex[:4]}", range_id=rng.id,
                  scenario_id=sc.id if sc else None, state=state, tenant_id=tenant, total_score=score[0],
                  max_score=score[1], completed_at=NOW)
    db.add(ex)
    db.flush()
    return ex


def review(db, item_type: str, row, *, stale=False, retired=False, reasons=None, tenant="same") -> ContentReviewItem:
    r = ContentReviewItem(id=uuid.uuid4(), tenant_id=row.tenant_id if tenant == "same" else tenant,
                          item_type=item_type, item_id=str(row.id), title=row.name, theme_ids=[],
                          stale=stale, stale_reasons=reasons or ([{"kind": "time", "text": "Its review is overdue",
                                                                   "origin_id": "x", "source": "clock"}]
                                                                 if stale else []),
                          retired=retired, due_at=NOW + timedelta(days=60), last_reviewed_at=NOW - timedelta(days=5))
    db.add(r)
    db.flush()
    return r


def prove_template(db, tmpl: Template) -> Template:
    certify(db, tmpl)
    exercise(db, tmpl)
    review(db, "range_template", tmpl)
    return tmpl


def prove_scenario(db, sc: Scenario, tmpl: Template) -> Scenario:
    exercise(db, tmpl, sc)
    review(db, "scenario", sc)
    return sc


def prove_course(db, crs: Course, tmpl: Template | None = None) -> Course:
    closed_class(db, crs, tmpl=tmpl)
    review(db, "course", crs)
    return crs


def proven_set(db):
    """A proven template, scenario and course that uses them."""
    t = prove_template(db, template(db))
    s = prove_scenario(db, scenario(db, t), t)
    c = prove_course(db, course(db, s), t)
    return t, s, c


def golden_build(db, catalogue_id="ubuntu-2404", build_id="b-2026-10-01", checksum="a" * 64):
    from app.golden_image_builds.models import GoldenImageBuild

    g = db.query(GoldenImage).filter_by(catalogue_id=catalogue_id, hypervisor="vsphere").first()
    if g is None:
        g = GoldenImage(id=uuid.uuid4(), catalogue_id=catalogue_id, hypervisor="vsphere")
        db.add(g)
        db.flush()
    db.add(GoldenImageBuild(id=uuid.uuid4(), golden_image_id=g.id, build_id=build_id, checksum=checksum,
                            copies="{}", promoted=True))
    db.flush()
