"""ADR 0017 tripwire (docs/adr/0017-exercise-template-record.md).

One test per enforceable decision of the ADR: each pins the code that honours it today, so the
day the code drifts from the decision the test goes red. Behavioural tests run the real service
functions against the shared in-process SQLite session (no mocks of the unit under test, no
live services); structural tests read the source or the SQLAlchemy metadata directly.

Decisions quoted in the docstrings are the ADR's own words.
"""

from __future__ import annotations

import ast
import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.auth import CurrentUser
from app.content_port import importer, proven
from app.content_port.schemas import (
    FORMAT_V1,
    FORMAT_V2,
    ExerciseTemplateContent,
    ExerciseTemplateCore,
    ProofSummary,
)
from app.content_port.service import status_out
from app.content_review.models import ContentReviewItem
from app.content_review.staleness import ITEM_TYPES as API_ITEM_TYPES
from app.exercise_templates import service as et_service
from app.exercise_templates.models import ExerciseTemplate, ExerciseTemplateUse
from app.exercise_templates.schemas import (
    CORE_FIELDS,
    ExerciseFromTemplateIn,
    ExerciseSaveAsTemplateIn,
    ExerciseTemplateUpdate,
    limit,
)
from app.models import (
    Exercise,
    ExerciseObjective,
    ExerciseState,
    MeslEvent,
    Scenario,
    Template,
    Tenant,
    User,
    UserRole,
)
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session
from worker.content_review_runtime import staleness as worker_staleness

ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "control-plane/api"
WORKER_DIR = ROOT / "control-plane/worker"
ROUTER_SRC = (API_DIR / "app/exercise_templates/router.py").read_text(encoding="utf-8")
SERVICE_SRC = (API_DIR / "app/exercise_templates/service.py").read_text(encoding="utf-8")
SYNC_SRC = (WORKER_DIR / "worker/content_review_sync.py").read_text(encoding="utf-8")
BUNDLE_SRC = (API_DIR / "app/content_port/bundle.py").read_text(encoding="utf-8")
MODELS_SRC = (API_DIR / "app/models.py").read_text(encoding="utf-8")

NOW = datetime.now(UTC)


# ── builders (real rows in the shared session; nothing mocked) ───────────────────────
def _tenant(db: Session) -> uuid.UUID:
    tid = uuid.uuid4()
    db.add(Tenant(id=tid, name=f"Tripwire {tid.hex[:6]}", slug=f"tw{tid.hex[:10]}"))
    db.flush()
    return tid


def _admin(db: Session, tenant: uuid.UUID) -> CurrentUser:
    u = User(id=uuid.uuid4(), email=f"tw-{uuid.uuid4().hex[:8]}@example.test", display_name="Tripwire Admin",
             role=UserRole.admin, tenant_id=tenant, keycloak_id=f"kc-{uuid.uuid4().hex[:8]}")
    db.add(u)
    db.flush()
    return CurrentUser(id=str(u.id), email=u.email, display_name=u.display_name, role=u.role,
                       tenant_id=str(u.tenant_id), keycloak_id=u.keycloak_id)


def _range_template(db: Session, tenant: uuid.UUID, name: str) -> Template:
    t = Template(id=uuid.uuid4(), name=name, version="1.0",
                 yaml=f"name: {name}\nnodes:\n  - id: ws01\n    os: ubuntu-2404\n  - id: dc01\n    os: srv2022\n",
                 tenant_id=tenant)
    db.add(t)
    db.flush()
    return t


def _scenario(db: Session, tenant: uuid.UUID, tmpl: Template, *, by_name: bool = False) -> Scenario:
    ref = tmpl.name if by_name else str(tmpl.id)
    s = Scenario(id=uuid.uuid4(), name=f"Tripwire scenario {uuid.uuid4().hex[:6]}", version="1.0",
                 yaml=f"name: Tripwire scenario\nrange_template: {ref}\nobjectives: []\n", tenant_id=tenant)
    db.add(s)
    db.flush()
    return s


def _range(db: Session, tenant: uuid.UUID, tmpl: Template) -> Range:  # noqa: F821
    from app.models import Range

    rng = Range(id=uuid.uuid4(), name=f"tripwire-range-{uuid.uuid4().hex[:6]}", template_id=tmpl.id, tenant_id=tenant)
    db.add(rng)
    db.flush()
    return rng


def _ex_template(db: Session, tenant: uuid.UUID, sc: Scenario | None, rt: Template | None, *,
                 name: str = "Tripwire drill") -> ExerciseTemplate:
    row = ExerciseTemplate(
        id=uuid.uuid4(), name=name, description="Respond to the intrusion.", revision=1, tenant_id=tenant,
        scenario_id=sc.id if sc is not None else None, range_template_id=rt.id if rt is not None else None,
        teams=[{"name": "Blue", "side": "blue", "min_students": 2, "max_students": 6}],
        objectives=[{"ref": "O1", "text": "Detect the phish", "moe": "within 15 minutes", "competency_code": "DCWF-1"},
                    {"ref": "O2", "text": "Contain the host", "moe": "", "competency_code": ""}],
        mesl=[{"serial": i, "phase": "D1", "scenario_time": f"D1 09{i}0", "title": f"Serial {i}", "description": "",
               "objective_ref": "O1" if i == 1 else "", "attack_technique": "T1566" if i == 1 else "",
               "delivery_method": "email" if i == 1 else "white_cell", "from_cell": "OPFOR",
               "to_participant": "Blue", "expected_action": "Report it", "moe": ""} for i in (1, 2, 3)],
        roles=[{"name": "Analyst", "description": "Triage alerts", "team": "Blue", "min_participants": 2,
                "max_participants": 12, "default_count": 4, "vm_profile": [{"node_type": "ubuntu-2404", "count": 1}]}],
        scaling_rules=[{"name": "Targets", "node_type": "srv2022", "kind": "per_participants", "count": 1, "per": 4,
                        "role": "Analyst", "segment": ""}],
        instructor_notes="The notes are the answer key.", tags=["DFIR"], created_by=None)
    db.add(row)
    db.flush()
    return row


def _made_from(db: Session, et: ExerciseTemplate, tenant: uuid.UUID, *, score: int = 80,
               state: ExerciseState = ExerciseState.completed) -> Exercise:
    """A completed exercise made from ``et`` on a range built from its range template."""
    rt = db.get(Template, et.range_template_id)
    rng = _range(db, tenant, rt)
    ex = Exercise(id=uuid.uuid4(), name=f"Made {uuid.uuid4().hex[:4]}", kind="collective", range_id=rng.id,
                  scenario_id=et.scenario_id, state=state, tenant_id=tenant, total_score=score, max_score=100,
                  completed_at=NOW)
    db.add(ex)
    db.flush()
    db.add(ExerciseTemplateUse(exercise_id=ex.id, template_id=et.id, template_revision=et.revision, tenant_id=tenant))
    db.flush()
    return ex


def _review(db: Session, et: ExerciseTemplate) -> ContentReviewItem:
    r = ContentReviewItem(id=uuid.uuid4(), tenant_id=et.tenant_id, item_type="exercise_template",
                          item_id=str(et.id), title=et.name, theme_ids=[], stale=False, stale_reasons=[],
                          retired=False, due_at=NOW + timedelta(days=60), last_reviewed_at=NOW - timedelta(days=5))
    db.add(r)
    db.flush()
    return r


def _core_payload(**over):
    base: dict = {
        "name": "Tripwire core", "description": "",
        "teams": [{"name": "Blue", "side": "blue", "min_students": 1, "max_students": 4}],
        "roles": [{"name": "Analyst", "description": "", "team": "Blue", "min_participants": 1,
                   "max_participants": 4, "default_count": 2, "vm_profile": [{"node_type": "ws", "count": 1}]}],
        "scaling_rules": [], "objectives": [], "mesl": [], "instructor_notes": "", "tags": [],
    }
    base.update(over)
    return base


# ── decision 1: the record ───────────────────────────────────────────────────────────
def test_the_record_holds_exactly_the_reusable_parts(db_session):
    """ADR 0017 §1: "``ExerciseTemplate`` ... holds the reusable parts: name, description,
    objectives, the MESL (without delivery status), the scenario reference, the range-template
    reference, the teams (name, side, minimum and maximum Students), the participant roles and
    sizing rules (9 below), the instructor's notes and tags."
    """
    columns = {c.name for c in ExerciseTemplate.__table__.columns}
    assert {"name", "description", "objectives", "mesl", "scenario_id", "range_template_id", "teams", "roles",
            "scaling_rules", "instructor_notes", "tags"} <= columns
    # "the MESL (without delivery status)": the MESL entry schema has no status field at all
    import app.exercise_templates.schemas as schemas

    assert "status" not in schemas.ExerciseMeslSpec.model_fields
    assert "delivery_method" in schemas.ExerciseMeslSpec.model_fields


def test_collective_only_no_kind_field_and_no_course_link(db_session):
    """ADR 0017 §1: "It is **collective only**: there is no individual or ``kind`` field. It is
    never attached to a course module, a course does not reference it, and it does not touch a
    course's ``lab_range``."
    """
    assert "kind" not in {c.name for c in ExerciseTemplate.__table__.columns}
    fk_targets = {fk.column.table.name for t in (ExerciseTemplate.__table__, ExerciseTemplateUse.__table__)
                  for fk in t.foreign_keys}
    assert fk_targets == {"tenants", "users", "scenarios", "templates", "exercises", "exercise_templates"}
    referencing = {fk.parent.table.name for t in ExerciseTemplate.metadata.tables.values()
                   for fk in t.foreign_keys if fk.column.table.name == "exercise_templates"}
    assert referencing == {"exercise_template_uses"}


def test_the_use_link_table_records_template_and_revision(db_session):
    """ADR 0017 §2: "The link table ``exercise_template_uses`` records the template and its
    ``revision``" — one row per exercise made, keyed by the exercise (its primary key), so a
    hand-made exercise is simply a row that does not exist."
    """
    cols = {c.name: c for c in ExerciseTemplateUse.__table__.columns}
    assert set(cols) == {"exercise_id", "template_id", "template_revision", "tenant_id", "created_at", "updated_at"}
    assert cols["exercise_id"].primary_key  # keyed by the exercise: a hand-made one is a row that does not exist
    fk_targets = {fk.column.table.name for fk in ExerciseTemplateUse.__table__.foreign_keys}
    assert fk_targets == {"exercises", "exercise_templates", "tenants"}


# ── decision 2: creating an exercise from a template ─────────────────────────────────
def test_creating_an_exercise_copies_objectives_and_mesl_and_records_the_revision(db_session):
    """ADR 0017 §2: "makes a collective ``Exercise`` and **copies** the objectives and MESL into
    it."  The copy is real rows (ExerciseObjective with an ordinal, MeslEvent with delivery
    method, planned), and the use row carries the template's revision."""
    t = _tenant(db_session)
    rt = _range_template(db_session, t, "Copy lab")
    sc = _scenario(db_session, t, rt)
    et = _ex_template(db_session, t, sc, rt)
    rng = _range(db_session, t, rt)
    who = _admin(db_session, t)

    out = et_service.create_exercise(db_session, who, et.id,
                                     ExerciseFromTemplateIn(name="New drill", range_id=rng.id, max_score=None))
    ex = db_session.get(Exercise, out.id)
    assert ex is not None
    assert ex.kind == "collective"
    objectives = db_session.query(ExerciseObjective).filter_by(exercise_id=ex.id).order_by(ExerciseObjective.ordinal)
    assert [(o.ref, o.text, o.ordinal) for o in objectives] == [("O1", "Detect the phish", 0),
                                                                ("O2", "Contain the host", 1)]
    mesl = db_session.query(MeslEvent).filter_by(exercise_id=ex.id).order_by(MeslEvent.serial).all()
    assert [m.serial for m in mesl] == [1, 2, 3]
    assert {m.status for m in mesl} == {"planned"}
    assert {m.delivery_method for m in mesl} == {"email", "white_cell"}
    use = db_session.query(ExerciseTemplateUse).filter_by(exercise_id=ex.id).one()
    assert use.template_id == et.id
    assert use.template_revision == et.revision == 1


def test_an_exercise_made_by_hand_has_no_row(db_session):
    """ADR 0017 §2: "``exercises`` and ``app.models`` are unchanged, and an exercise made by
    hand has no row." """
    t = _tenant(db_session)
    rt = _range_template(db_session, t, "Hand lab")
    rng = _range(db_session, t, rt)
    hand = Exercise(id=uuid.uuid4(), name="Hand made", kind="collective", range_id=rng.id, tenant_id=t,
                    state=ExerciseState.pending, max_score=100)
    db_session.add(hand)
    db_session.flush()
    assert db_session.query(ExerciseTemplateUse).filter_by(exercise_id=hand.id).first() is None
    # "app.models is unchanged": the shared models module never mentions the link table
    assert "exercise_template" not in MODELS_SRC
    exercise_fks = {fk.column.table.name for fk in Exercise.__table__.foreign_keys}
    assert "exercise_templates" not in exercise_fks


def test_the_range_must_be_the_callers_and_built_from_the_templates_range_template(db_session):
    """ADR 0017 §2: "The range must be the caller's tenant's and built from the template's
    range template (or its scenario's), so proof means what it says." """
    t = _tenant(db_session)
    other = _tenant(db_session)
    rt = _range_template(db_session, t, "Proof lab")
    stranger_rt = _range_template(db_session, t, "Other lab")
    et = _ex_template(db_session, t, None, rt)
    who = _admin(db_session, t)
    stranger_range = _range(db_session, other, rt)  # another tenant's range
    wrong_build = _range(db_session, t, stranger_rt)  # ours, but built from another template

    with pytest.raises(HTTPException) as refused:
        et_service.create_exercise(db_session, who, et.id,
                                   ExerciseFromTemplateIn(name="x", range_id=stranger_range.id, max_score=None))
    assert refused.value.status_code == 422
    assert "range not found" in refused.value.detail
    with pytest.raises(HTTPException) as mismatch:
        et_service.create_exercise(db_session, who, et.id,
                                   ExerciseFromTemplateIn(name="x", range_id=wrong_build.id, max_score=None))
    assert mismatch.value.status_code == 422
    assert "was not built from range template 'Proof lab'" in mismatch.value.detail


# ── decision 3: editing never changes exercises already made ─────────────────────────
def test_editing_rises_the_revision_and_leaves_made_exercises_alone(db_session):
    """ADR 0017 §3: "Editing never changes exercises already made. ``revision`` rises on every
    edit; the exercises keep their own copy and the revision they came from." """
    t = _tenant(db_session)
    rt = _range_template(db_session, t, "Edit lab")
    sc = _scenario(db_session, t, rt)
    et = _ex_template(db_session, t, sc, rt)
    who = _admin(db_session, t)
    rng = _range(db_session, t, rt)
    out = et_service.create_exercise(db_session, who, et.id,
                                     ExerciseFromTemplateIn(name="Drill", range_id=rng.id, max_score=None))
    before_objectives = [(o.ref, o.text) for o in db_session.query(ExerciseObjective)
                         .filter_by(exercise_id=out.id).order_by(ExerciseObjective.ordinal)]

    et_service.update(db_session, who, et.id, ExerciseTemplateUpdate(name="Renamed drill"))

    assert et.revision == 2
    assert [(o.ref, o.text) for o in db_session.query(ExerciseObjective).filter_by(exercise_id=out.id)
            .order_by(ExerciseObjective.ordinal)] == before_objectives
    use = db_session.query(ExerciseTemplateUse).filter_by(exercise_id=out.id).one()
    assert use.template_revision == 1  # the revision the exercise came from


def test_deleting_a_template_exercises_were_made_from_is_refused(db_session):
    """ADR 0017 §3: "Deleting a template that exercises were made from is refused (409, ADR
    0010): they are training record; retire it on the content clock instead." """
    t = _tenant(db_session)
    rt = _range_template(db_session, t, "Delete lab")
    et = _ex_template(db_session, t, None, rt)
    unused = _ex_template(db_session, t, None, rt, name="Unused drill")
    who = _admin(db_session, t)
    _made_from(db_session, et, t)

    with pytest.raises(HTTPException) as refused:
        et_service.delete(db_session, who, et.id)
    assert refused.value.status_code == 409
    assert "kept as part of the training record" in refused.value.detail
    et_service.delete(db_session, who, unused.id)  # nothing made from it: the delete goes through
    assert db_session.get(ExerciseTemplate, unused.id).deleted_at is not None


# ── decision 4: tenancy ──────────────────────────────────────────────────────────────
def test_another_tenants_private_template_is_a_404_and_never_changed_by_them(db_session):
    """ADR 0017 §4: "``tenant_id`` plus ``is_public``. Another tenant's private template is a
    404. A shared one is listed and readable by staff of other tenants ... never changed by
    them." """
    t = _tenant(db_session)
    other = _tenant(db_session)
    rt = _range_template(db_session, t, "Tenancy lab")
    et = _ex_template(db_session, t, None, rt)
    shared = _ex_template(db_session, t, None, rt, name="Shared drill")
    shared.is_public = True
    db_session.flush()
    stranger = _admin(db_session, other)

    for call in (lambda: et_service.get_visible(db_session, stranger, et.id),
                 lambda: et_service.update(db_session, stranger, et.id, ExerciseTemplateUpdate(name="hijacked")),
                 lambda: et_service.delete(db_session, stranger, et.id)):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 404
    assert et_service.get_visible(db_session, stranger, shared.id).id == shared.id  # shared is readable


def test_only_the_platform_administrator_shares_one(db_session):
    """ADR 0017 §4: "Only the platform administrator shares one." """
    from app.exercise_templates.service import _require_share

    t = _tenant(db_session)
    other = _tenant(db_session)
    who = _admin(db_session, t)
    with pytest.raises(HTTPException) as refused:  # two tenants, caller is not the platform admin
        _require_share(db_session, who)
    assert refused.value.status_code == 403
    assert "Only the platform administrator" in refused.value.detail
    with pytest.raises(HTTPException):
        _require_share(db_session, _admin(db_session, other))  # nor is any other tenant's admin
    who2 = _admin(db_session, t)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PLATFORM_TENANT_ID", str(t))
        _require_share(db_session, who2)  # the platform administrator shares


def test_every_endpoint_needs_exercise_create(db_session):
    """ADR 0017 §4: "All endpoints need ``exercise:create`` (admins, instructors), reads
    included, because the MESL and the notes are the answer key ... No new permission." """
    tree = ast.parse(ROUTER_SRC)
    endpoints = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                         and isinstance(d.func.value, ast.Name) and d.func.value.id == "router"
                         for d in node.decorator_list)]
    assert len(endpoints) >= 8, "the exercise-template router lost endpoints"
    for node in endpoints:
        defaults = node.args.defaults + [d for d in node.args.kw_defaults if d is not None]
        guarded = [d for d in defaults if isinstance(d, ast.Call) and isinstance(d.func, ast.Name)
                   and d.func.id == "Depends" and d.args and isinstance(d.args[0], ast.Name)
                   and d.args[0].id == "_STAFF"]
        assert guarded, f"route {node.name} does not depend on _STAFF (exercise:create)"
    assert ROUTER_SRC.count("require_permission(") == 1, "a new permission appeared"
    assert "_STAFF = require_permission(Permission.EXERCISE_CREATE)" in ROUTER_SRC


# ── decision 5: save as template ─────────────────────────────────────────────────────
def test_save_as_template_copies_a_collective_exercise(db_session):
    """ADR 0017 §5: "Optional \"Save as template\" ... copies a collective exercise's
    objectives, MESL, scenario and range template; an individual exercise is refused." """
    t = _tenant(db_session)
    rt = _range_template(db_session, t, "Save lab")
    sc = _scenario(db_session, t, rt)
    who = _admin(db_session, t)
    rng = _range(db_session, t, rt)
    ex = Exercise(id=uuid.uuid4(), name="Collective drill", kind="collective", range_id=rng.id, scenario_id=sc.id,
                  tenant_id=t, state=ExerciseState.completed, max_score=100)
    db_session.add(ex)
    db_session.flush()
    db_session.add(ExerciseObjective(exercise_id=ex.id, ref="O1", text="Detect the phish", ordinal=0))
    db_session.add(MeslEvent(exercise_id=ex.id, serial=1, phase="D1", title="Serial 1", status="planned",
                             delivery_method="email"))
    db_session.flush()

    saved = et_service.save_exercise(db_session, who, ex.id, ExerciseSaveAsTemplateIn(name="Saved drill"))
    assert saved.revision == 1
    assert saved.scenario_id == sc.id
    assert saved.range_template_id == rt.id
    assert [o["ref"] for o in saved.objectives] == ["O1"]
    assert [m["serial"] for m in saved.mesl] == [1]
    assert db_session.query(ExerciseTemplateUse).filter_by(template_id=saved.id).first() is None

    lone = Exercise(id=uuid.uuid4(), name="Individual practical", kind="assessment", range_id=rng.id, tenant_id=t,
                    state=ExerciseState.completed, max_score=100)
    db_session.add(lone)
    db_session.flush()
    with pytest.raises(HTTPException) as refused:
        et_service.save_exercise(db_session, who, lone.id, ExerciseSaveAsTemplateIn(name="No"))
    assert refused.value.status_code == 422
    assert "Only a collective exercise" in refused.value.detail


# ── decision 6: the content clock ────────────────────────────────────────────────────
def test_the_content_clock_knows_exercise_templates(db_session):
    """ADR 0017 §6: "``exercise_template`` is a content-clock type ... It depends on its
    scenario and its range template, so their staleness reaches it. The worker's sync reads
    ``exercise_templates``." """
    assert "exercise_template" in API_ITEM_TYPES
    assert "exercise_template" in worker_staleness.ITEM_TYPES
    # the worker's sync selects exercise_templates and wires both dependencies
    assert "exercise_templates.c.scenario_id" in SYNC_SRC
    assert "exercise_templates.c.range_template_id" in SYNC_SRC
    assert 'c.dep(key, ("scenario", str(e.scenario_id)))' in SYNC_SRC
    assert 'c.dep(key, ("range_template", str(e.range_template_id)))' in SYNC_SRC


# ── decision 7: proven ───────────────────────────────────────────────────────────────
def test_proven_is_computed_never_stored_and_passes_at_70(db_session):
    """ADR 0017 §7: "**Proven** ..., computed, never stored." and "Used: an exercise **made from
    the template** completed with a passing score (70%)"."""
    assert proven.PASS_PERCENT == 70
    stored = [c.name for c in ExerciseTemplate.__table__.columns if "proven" in c.name]
    assert stored == [], f"provenance was stored on the record: {stored}"


def test_used_means_made_from_the_current_revision_on_a_matching_range(db_session):
    """ADR 0017 §7: "Used: ... **made from the template's current revision and on a range built
    from the range template it runs on now**; an exercise merely on the same range proves
    nothing, and any edit needs a new exercise to prove it again." """
    t = _tenant(db_session)
    rt = _range_template(db_session, t, "Proven lab")
    et = _ex_template(db_session, t, None, rt)
    scope = {t}

    # an exercise merely on the same range proves nothing: no use row, no evidence
    rt2 = db_session.get(Template, et.range_template_id)
    rng = _range(db_session, t, rt2)
    bystander = Exercise(id=uuid.uuid4(), name="Bystander", kind="collective", range_id=rng.id, tenant_id=t,
                         state=ExerciseState.completed, total_score=80, max_score=100, completed_at=NOW)
    db_session.add(bystander)
    db_session.flush()
    assert proven.exercise_template_use(db_session, et, scope).met is False

    made = _made_from(db_session, et, t, score=80)
    assert proven.exercise_template_use(db_session, et, scope).met is True

    use = db_session.query(ExerciseTemplateUse).filter_by(exercise_id=made.id).one()
    use.template_revision = et.revision - 1  # made from an older revision: proves nothing
    db_session.flush()
    assert proven.exercise_template_use(db_session, et, scope).met is False
    use.template_revision = et.revision
    made.total_score = 50  # below the 70% passing score
    db_session.flush()
    assert proven.exercise_template_use(db_session, et, scope).met is False
    made.total_score = 70  # exactly 70%: a passing score
    db_session.flush()
    assert proven.exercise_template_use(db_session, et, scope).met is True
    made.state = ExerciseState.running  # not completed: no evidence yet
    db_session.flush()
    assert proven.exercise_template_use(db_session, et, scope).met is False


# ── decision 8: the port ─────────────────────────────────────────────────────────────
def test_a_bundle_that_carries_an_exercise_template_is_v2(db_session):
    """ADR 0017 §8: "A bundle that carries an exercise template is format **v2**; a bundle
    without one stays v1, so older sites still read it. A v1 bundle carrying an exercise
    template is refused, and a format this site does not know is refused with a message to
    upgrade." """
    assert 'fmt = FORMAT_V2 if any(t == "exercise_template" for t, _ in items) else FORMAT_V1' in BUNDLE_SRC
    assert "A v1 bundle carries no exercise templates" in BUNDLE_SRC
    assert "Upgrade this site" in BUNDLE_SRC
    assert FORMAT_V2 != FORMAT_V1


def test_the_import_content_is_the_strict_model_plus_the_two_keys(db_session):
    """ADR 0017 §8: "The content is the same strict model the API validates, plus the keys of
    the scenario and range template; no tenant, owner, sharing or revision." """
    assert issubclass(ExerciseTemplateContent, ExerciseTemplateCore)
    content_fields = set(ExerciseTemplateContent.model_fields)
    assert content_fields == set(CORE_FIELDS) | {"scenario", "range_template"}
    assert not content_fields & {"tenant_id", "is_public", "revision", "created_by"}
    with pytest.raises(ValidationError):
        ExerciseTemplateContent.model_validate(_core_payload(name="Too big", roles=[{
            "name": "Crowd", "description": "", "team": "Blue", "min_participants": 0, "max_participants": 0,
            "default_count": 400, "vm_profile": [{"node_type": "ws", "count": 3}]}]))  # 1200 VMs > the 1000 cap


# ── decision 9: roles and sizing rules ───────────────────────────────────────────────
def test_roles_and_rules_are_consistent(db_session):
    """ADR 0017 §9: "The record only guarantees they are consistent (min <= default <= max,
    unique names, teams and roles referenced exist, a step size where one is needed)." """
    with pytest.raises(ValidationError):
        ExerciseTemplateCore.model_validate(_core_payload(roles=[_core_payload()["roles"][0]
                                                 | {"min_participants": 3, "default_count": 2}]))
    with pytest.raises(ValidationError):
        ExerciseTemplateCore.model_validate(_core_payload(roles=[_core_payload()["roles"][0]
                                                 | {"default_count": 5}]))  # default above max 4
    with pytest.raises(ValidationError, match="which the template lacks"):
        ExerciseTemplateCore.model_validate(_core_payload(roles=[_core_payload()["roles"][0] | {"team": "Red"}]))
    with pytest.raises(ValidationError, match="which the template lacks"):
        ExerciseTemplateCore.model_validate(_core_payload(scaling_rules=[
            {"name": "Targets", "node_type": "ws", "kind": "per_participants", "count": 1, "per": 2,
             "role": "Nobody"}]))
    with pytest.raises(ValidationError, match="a role is listed twice"):
        analyst = _core_payload()["roles"][0]
        ExerciseTemplateCore.model_validate(_core_payload(roles=[analyst, analyst | {"team": ""}]))
    with pytest.raises(ValidationError, match="needs per"):
        ExerciseTemplateCore.model_validate(_core_payload(scaling_rules=[
            {"name": "Targets", "node_type": "ws", "kind": "per_participants", "count": 1, "per": 0, "role": ""}]))
    with pytest.raises(ValidationError, match="which the template lacks"):
        ExerciseTemplateCore.model_validate(_core_payload(mesl=[
            {"serial": 1, "title": "Serial 1", "objective_ref": "O9"}]))


def test_node_types_must_exist_in_the_range_template(db_session):
    """ADR 0017 §9: "**Proven** also requires every node type a role or rule names to exist in
    the range template ...: a profile that points at nothing sizes nothing." """
    t = _tenant(db_session)
    rt = _range_template(db_session, t, "Node lab")  # builds ubuntu-2404 and srv2022
    good = _ex_template(db_session, t, None, rt)
    bad = _ex_template(db_session, t, None, rt, name="Broken drill")
    bad.roles = [{"name": "Analyst", "description": "", "team": "Blue", "min_participants": 1, "max_participants": 4,
                  "default_count": 2, "vm_profile": [{"node_type": "gpu-box", "count": 1}]}]

    assert proven.sizing_problems(good, [rt]) == []
    problems = proven.sizing_problems(bad, [rt])
    assert len(problems) == 1
    assert "gpu-box" in problems[0] and rt.name in problems[0]


# ── decision 10: edits reset the review ──────────────────────────────────────────────
def test_sizing_edits_flag_the_clock_stale(db_session):
    """ADR 0017 §10: "Changing a template's scenario, range template, roles or scaling rules (in
    the API, or by an import update) flags its content-clock row stale until it is reviewed
    again, so a changed template is \"not yet reviewed\"." """
    assert et_service.SIZING_FIELDS == ("scenario_id", "range_template_id", "roles", "scaling_rules")
    t = _tenant(db_session)
    rt = _range_template(db_session, t, "Stale lab")
    et = _ex_template(db_session, t, None, rt)
    who = _admin(db_session, t)
    review = _review(db_session, et)

    et_service.update(db_session, who, et.id, ExerciseTemplateUpdate(name="Only a rename"))
    db_session.flush()
    assert review.stale is False  # a rename is not a sizing edit

    et_service.update(db_session, who, et.id, ExerciseTemplateUpdate(roles=[_core_payload()["roles"][0]]))
    db_session.flush()
    assert review.stale is True
    texts = " ".join(str(r.get("text") or "") for r in review.stale_reasons)
    assert "roles" in texts
    assert review.stale_reasons and "Changed since it was last reviewed (roles)" in texts


# ── decision 11: no leaks across tenants ─────────────────────────────────────────────
def test_no_other_tenants_private_names_reach_a_shared_reader(db_session):
    """ADR 0017 §11: "A shared template (or scenario) never names another tenant's private
    scenario or range template: not in the dependency list, nor in any reason, and the
    dependency's status is not shown." """
    t = _tenant(db_session)
    other = _tenant(db_session)
    hidden = _range_template(db_session, t, "Hidden Lab")
    hidden.is_public = False
    sc = _scenario(db_session, t, hidden, by_name=True)
    site = {t}

    st = proven.status(db_session, "scenario", sc, site, viewer=other)
    assert len(st.dependencies) == 1
    assert st.dependencies[0].title == "a range template that is not shared with you"
    rendered = json.dumps(asdict(st), default=str)
    assert "hidden lab" not in rendered.lower()
    assert any("a private item" in r for c in st.conditions for r in c.reasons)

    out = status_out(db_session, "scenario", sc, site, viewer=other)
    assert out.dependencies[0].title == "a range template that is not shared with you"
    assert out.dependencies[0].proven is None  # the dependency's status is not shown


# ── decision 12: concurrency and caps ────────────────────────────────────────────────
def test_writers_lock_the_template_row(db_session):
    """ADR 0017 §12: "Delete, edit, create-exercise and an import update lock the template row
    (``SELECT ... FOR UPDATE``, read as it is now), so two writers never share a revision
    number." """
    tree = ast.parse(SERVICE_SRC)
    functions = {node.name: ast.get_source_segment(SERVICE_SRC, node) for node in tree.body
                 if isinstance(node, ast.FunctionDef)}
    lock = functions["lock_visible"]
    assert "with_for_update()" in lock and "populate_existing()" in lock
    for name in ("update", "delete", "create_exercise"):
        assert "lock_visible(" in functions[name], f"{name} does not lock the template row"
    importer_tree = ast.parse((API_DIR / "app/content_port/importer.py").read_text(encoding="utf-8"))
    importer_functions = {node.name: ast.get_source_segment((API_DIR / "app/content_port/importer.py")
                          .read_text(encoding="utf-8"), node) for node in importer_tree.body
                          if isinstance(node, ast.FunctionDef)}
    assert ".with_for_update()" in importer_functions["_write_exercise_template"]


def test_caps_are_counted_exactly_as_the_adr_says(db_session, monkeypatch):
    """ADR 0017 §12: "a role's participants are its ``max_participants``, or, **when it has no
    maximum (0), its ``default_count``**; the template's total participants is their sum ...
    each participant's VMs are the sum of its VM profile counts ... **total VMs** is the worst
    case of the roles plus the scaling rules ... ``per_segment`` rule: ``count x
    MAX_SEGMENTS`` ... ``per_participants`` rule: ``count x ceil(people / per)``."""
    crowd = {"name": "Crowd", "description": "", "team": "Blue", "min_participants": 0, "max_participants": 0,
             "default_count": 6, "vm_profile": [{"node_type": "ws", "count": 1}]}  # unlimited: counts at 6
    few = {"name": "Few", "description": "", "team": "Blue", "min_participants": 0, "max_participants": 2,
           "default_count": 1, "vm_profile": [{"node_type": "srv", "count": 2}]}
    sensor = {"name": "Sensor", "node_type": "srv", "kind": "per_segment", "count": 1, "per": 0, "role": "",
              "segment": ""}
    targets = {"name": "Targets", "node_type": "ws", "kind": "per_participants", "count": 1, "per": 4,
               "role": "Crowd"}

    # people 6 + 2 = 8; per-participant VMs 1 and 2; roles 6*1 + 2*2 = 10; sensor 1*10; targets ceil(6/4)=2 → 22
    ExerciseTemplateCore.model_validate(_core_payload(roles=[crowd, few], scaling_rules=[sensor, targets]))
    with pytest.raises(ValidationError, match="the roles add up to 8 participants; the limit is 7"):
        # an unlimited role counts at its default: 8 > 7 fails, counting it as 0 would pass
        monkeypatch.setenv("TN_EXERCISE_TEMPLATE_MAX_PARTICIPANTS", "7")
        ExerciseTemplateCore.model_validate(_core_payload(roles=[crowd, few], scaling_rules=[sensor, targets]))
    monkeypatch.delenv("TN_EXERCISE_TEMPLATE_MAX_PARTICIPANTS")
    with pytest.raises(ValidationError, match="Few gives each participant 2 VMs; the limit is 1"):
        monkeypatch.setenv("TN_EXERCISE_TEMPLATE_MAX_VMS_PER_PARTICIPANT", "1")
        ExerciseTemplateCore.model_validate(_core_payload(roles=[crowd, few], scaling_rules=[sensor, targets]))
    monkeypatch.delenv("TN_EXERCISE_TEMPLATE_MAX_VMS_PER_PARTICIPANT")
    # per_segment: count x MAX_SEGMENTS, conservatively — 3 segments make the sensor 3, total 15
    monkeypatch.setenv("TN_EXERCISE_TEMPLATE_MAX_SEGMENTS", "3")
    monkeypatch.setenv("TN_EXERCISE_TEMPLATE_MAX_VMS", "15")
    ExerciseTemplateCore.model_validate(_core_payload(roles=[crowd, few], scaling_rules=[sensor, targets]))
    monkeypatch.setenv("TN_EXERCISE_TEMPLATE_MAX_VMS", "14")
    with pytest.raises(ValidationError, match="add up to 15 VMs; the limit is 14"):
        ExerciseTemplateCore.model_validate(_core_payload(roles=[crowd, few], scaling_rules=[sensor, targets]))
    monkeypatch.delenv("TN_EXERCISE_TEMPLATE_MAX_SEGMENTS")
    monkeypatch.delenv("TN_EXERCISE_TEMPLATE_MAX_VMS")
    # per_participants: whole steps of ``per`` — ceil(2/4) = 1 step, not 0
    small = {"name": "Crowd", "description": "", "team": "Blue", "min_participants": 0, "max_participants": 0,
             "default_count": 2, "vm_profile": [{"node_type": "ws", "count": 1}]}
    ExerciseTemplateCore.model_validate(_core_payload(roles=[small], scaling_rules=[targets]))
    monkeypatch.setenv("TN_EXERCISE_TEMPLATE_MAX_VMS", "2")  # roles 2 + targets 1 = 3
    with pytest.raises(ValidationError, match="add up to 3 VMs; the limit is 2"):
        ExerciseTemplateCore.model_validate(_core_payload(roles=[small], scaling_rules=[targets]))
    monkeypatch.delenv("TN_EXERCISE_TEMPLATE_MAX_VMS")
    # the default total-VM cap is 1000, and 1000 is allowed
    full = {"name": "Full", "description": "", "team": "Blue", "min_participants": 0, "max_participants": 500,
            "default_count": 500, "vm_profile": [{"node_type": "ws", "count": 2}]}
    ExerciseTemplateCore.model_validate(_core_payload(roles=[full]))  # 500 * 2 = 1000 = the default cap
    assert limit("MAX_VMS", 1000) == 1000


def test_an_import_rechecks_that_the_relinks_still_agree(db_session):
    """ADR 0017 §12: "An import re-checks that the scenario and range template it relinks to
    still agree, and skips the template with a reason if not." """
    t = _tenant(db_session)
    alpha = _range_template(db_session, t, "Alpha lab")
    beta = _range_template(db_session, t, "Beta lab")
    sc = _scenario(db_session, t, alpha, by_name=True)
    content = {
        "name": "Imported drill", "description": "", "teams": [], "roles": [], "scaling_rules": [],
        "objectives": [], "mesl": [], "instructor_notes": "", "tags": [],
        "scenario": f"scenario:{sc.id}", "range_template": f"range_template:{alpha.id}",
    }
    item = importer.BundleItem(item_type="exercise_template", key=f"exercise_template:{uuid.uuid4()}",
                               origin_id=uuid.uuid4().hex, content_sha256="a" * 64, content=content,
                               proof=ProofSummary(proven=False, conditions=[]))
    plan = importer.PlanItem(item=item, action="create", conflict=None, local=None,
                             link=None, reasons=[], notes=[])
    keymap = {f"scenario:{sc.id}": str(sc.id), f"range_template:{alpha.id}": str(beta.id)}
    assert importer._write_exercise_template(db_session, plan, t, False, keymap, None) is None
    assert plan.reasons and "runs on range template 'Alpha lab', not 'Beta lab'" in plan.reasons[0]


# ── the migration consequence, as far as it is a decision about the schema ────────────
def test_the_two_tables_come_from_one_additive_migration(db_session):
    """ADR 0017 §Consequences: "One migration (``9d4e7a2b5c18`` ...) adds two tables and writes
    no rows."  Both tables exist in the metadata the API runs on, and no other table was added
    by the decision (exercises and app.models are unchanged, §2)."""
    migration = (API_DIR / "alembic/versions/9d4e7a2b5c18_exercise_templates.py").read_text(encoding="utf-8")
    assert "exercise_templates" in migration and "exercise_template_uses" in migration
    assert "create_table" in migration
    assert "insert" not in migration.lower()  # writes no rows
