"""The exercise template's rules: who sees what, what a template may name, and making an
exercise from one (ADR 0017).

* **Tenancy.** A template belongs to its tenant. A platform administrator may share it
  (``is_public``), as scenarios and range templates are shared: every tenant's staff then
  list and read it, and make exercises from it, but only its tenant changes or deletes it.
  Another tenant's private template is not found (404).
* **References.** A template names a scenario and a range template only of its own tenant or
  shared ones, and the two must agree (the scenario's own ``range_template`` is the one
  named). A scenario or range template that is deleted later leaves the template not proven
  (``content_port.proven``), never a dangling error.
* **An exercise made from a template** is the caller's own tenant's: its range must be theirs
  and built from the template's range template, its scenario theirs (a shared template's
  scenario may be another tenant's: then it is refused with that reason). The objectives and
  the MESL are copied; the exercise records the template and its revision and never follows
  later edits.

Nothing here commits; the router does.
"""

from __future__ import annotations

import copy
import uuid

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import scenario_objectives
from ..auth import CurrentUser
from ..content_port import proven
from ..models import AuditLog, Exercise, ExerciseObjective, ExerciseState, MeslEvent, Range, Scenario, Template
from ..rbac import is_platform_admin
from ..tenancy import get_owned, tenant_uuid
from .models import ExerciseTemplate, ExerciseTemplateUse
from .schemas import (
    CORE_FIELDS,
    ExerciseFromTemplateIn,
    ExerciseFromTemplateOut,
    ExerciseMadeFromTemplateOut,
    ExerciseSaveAsTemplateIn,
    ExerciseTemplateCore,
    ExerciseTemplateIn,
    ExerciseTemplateListOut,
    ExerciseTemplateOut,
    ExerciseTemplateUpdate,
)


def _uid(user: CurrentUser) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(user.id))
    except (TypeError, ValueError):
        return None


def audit(db: Session, user: CurrentUser, action: str, resource_type: str, resource_id: str, detail: str = "") -> None:
    db.add(AuditLog(user_id=_uid(user), tenant_id=tenant_uuid(user), action=action, resource_type=resource_type,
                    resource_id=resource_id, detail=detail))


# ── reading ───────────────────────────────────────────────────────────────────────
def visible_query(db: Session, user: CurrentUser):
    """The caller's tenant's templates and the shared ones, never a deleted one."""
    return db.query(ExerciseTemplate).filter(
        ExerciseTemplate.deleted_at.is_(None),
        or_(ExerciseTemplate.tenant_id == tenant_uuid(user), ExerciseTemplate.is_public.is_(True)))


def get_visible(db: Session, user: CurrentUser, template_id: uuid.UUID) -> ExerciseTemplate:
    row = visible_query(db, user).filter(ExerciseTemplate.id == template_id).first()
    if row is None:
        raise HTTPException(404, "Exercise template not found")
    return row


def get_own(db: Session, user: CurrentUser, template_id: uuid.UUID) -> ExerciseTemplate:
    return get_owned(db, ExerciseTemplate, template_id, user, not_found="Exercise template not found")


def lock_visible(db: Session, user: CurrentUser, template_id: uuid.UUID, *, own: bool = False) -> ExerciseTemplate:
    """The template, row-locked (SELECT ... FOR UPDATE) until the transaction ends, so a delete and
    a create-exercise (or an edit) of the same template cannot interleave."""
    q = visible_query(db, user).filter(ExerciseTemplate.id == template_id)
    if own:
        q = q.filter(ExerciseTemplate.tenant_id == tenant_uuid(user))
    row = q.with_for_update().first()
    if row is None:
        raise HTTPException(404, "Exercise template not found")
    return row


SIZING_FIELDS = ("scenario_id", "range_template_id", "roles", "scaling_rules")


def flag_changed(db: Session, row: ExerciseTemplate, why: str) -> None:
    """A template whose scenario, range template, roles or scaling rules changed is not reviewed
    any more: its content-clock row is flagged stale until someone reviews it again (the flag
    clears on review). A template the clock does not track yet has nothing to flag."""
    from ..content_review import service as clock
    from ..content_review.models import ContentReviewItem

    review = (db.query(ContentReviewItem).filter(ContentReviewItem.item_type == "exercise_template",
                                                 ContentReviewItem.item_id == str(row.id)).first())
    if review is not None and not review.missing and not review.retired:
        clock.flag_stale(db, review, f"Changed since it was last reviewed ({why})",
                         f"exercise-template-edit:{row.id}:{row.revision}")


def is_shared_with(row: ExerciseTemplate, user: CurrentUser) -> bool:
    return row.tenant_id != tenant_uuid(user)


def out(row: ExerciseTemplate, user: CurrentUser) -> ExerciseTemplateOut:
    body = ExerciseTemplateOut.model_validate(row)
    body.shared = is_shared_with(row, user)
    return body


def list_out(row: ExerciseTemplate, user: CurrentUser) -> ExerciseTemplateListOut:
    return ExerciseTemplateListOut(
        id=row.id, name=row.name, description=row.description, revision=row.revision,
        is_public=row.is_public, shared=is_shared_with(row, user), scenario_id=row.scenario_id,
        range_template_id=row.range_template_id, team_names=[t["name"] for t in row.teams or []],
        role_names=[r["name"] for r in row.roles or []],
        objectives=len(row.objectives or []), mesl_events=len(row.mesl or []), tags=list(row.tags or []),
        updated_at=row.updated_at)


# ── what a template may name ──────────────────────────────────────────────────────
def _usable(row, tenant: uuid.UUID) -> bool:
    return row is not None and row.deleted_at is None and (row.tenant_id == tenant or bool(row.is_public))


def check_refs(db: Session, user: CurrentUser, scenario_id: uuid.UUID | None,
               range_template_id: uuid.UUID | None) -> None:
    """422 unless the scenario and the range template are the caller's tenant's or shared, and
    agree with each other."""
    tenant = tenant_uuid(user)
    sc = tmpl = None
    if scenario_id is not None:
        sc = db.get(Scenario, scenario_id)  # tenant-safe: _usable checks tenant or shared
        if not _usable(sc, tenant):
            raise HTTPException(422, "scenario not found")
    if range_template_id is not None:
        tmpl = db.get(Template, range_template_id)  # tenant-safe: _usable checks tenant or shared
        if not _usable(tmpl, tenant):
            raise HTTPException(422, "range template not found")
    if sc is not None and tmpl is not None:
        runs_on = proven.scenario_template(db, sc)
        if runs_on is not None and runs_on.id != tmpl.id:
            raise HTTPException(422, f"The scenario runs on range template '{runs_on.name}', not '{tmpl.name}'; "
                                     f"name the one the scenario uses")


def _core(data: dict) -> ExerciseTemplateCore:
    try:
        return ExerciseTemplateCore.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise HTTPException(422, f"{where + ': ' if where else ''}{first.get('msg', 'invalid')}") from None


def _require_share(db: Session, user: CurrentUser) -> None:
    if not is_platform_admin(user, db):
        raise HTTPException(403, "Only the platform administrator shares an exercise template with every tenant")


# ── writing ───────────────────────────────────────────────────────────────────────
def create(db: Session, user: CurrentUser, body: ExerciseTemplateIn) -> ExerciseTemplate:
    if body.is_public:
        _require_share(db, user)
    check_refs(db, user, body.scenario_id, body.range_template_id)
    core = body.model_dump(include=set(CORE_FIELDS), mode="json")
    row = ExerciseTemplate(id=uuid.uuid4(), tenant_id=tenant_uuid(user), is_public=body.is_public, revision=1,
                           scenario_id=body.scenario_id, range_template_id=body.range_template_id,
                           created_by=_uid(user), **core)
    db.add(row)
    db.flush()
    audit(db, user, "create", "exercise_template", str(row.id))
    return row


def update(db: Session, user: CurrentUser, template_id: uuid.UUID, body: ExerciseTemplateUpdate) -> ExerciseTemplate:
    row = lock_visible(db, user, template_id, own=True)
    before = {f: copy.deepcopy(getattr(row, f)) for f in SIZING_FIELDS}
    changes = body.model_dump(exclude_unset=True, mode="json")
    if "is_public" in changes:
        if changes["is_public"] is None:
            raise HTTPException(422, "is_public: must be true or false")
        if changes["is_public"] != row.is_public:
            _require_share(db, user)
    refs = {k: changes[k] if k in changes else getattr(row, k) for k in ("scenario_id", "range_template_id")}
    if "scenario_id" in changes or "range_template_id" in changes:
        check_refs(db, user, refs["scenario_id"], refs["range_template_id"])
    merged = {f: changes[f] if f in changes else getattr(row, f) for f in CORE_FIELDS}
    core = _core(merged).model_dump(mode="json")
    for field in CORE_FIELDS:
        setattr(row, field, core[field])
    row.scenario_id, row.range_template_id = refs["scenario_id"], refs["range_template_id"]
    if "is_public" in changes:
        row.is_public = changes["is_public"]
    row.revision = (row.revision or 1) + 1
    db.flush()
    moved = [f for f in SIZING_FIELDS if getattr(row, f) != before[f]]
    if moved:
        flag_changed(db, row, ", ".join(f.replace("_", " ") for f in moved))
    audit(db, user, "update", "exercise_template", str(row.id), f"revision {row.revision}")
    return row


def delete(db: Session, user: CurrentUser, template_id: uuid.UUID) -> None:
    """Soft-delete. Refused (409, ADR 0010) while an exercise was made from it: those exercises
    and their evidence stay attributable to it. Retire it on the content clock instead."""
    row = lock_visible(db, user, template_id, own=True)
    made = (db.query(ExerciseTemplateUse.exercise_id).join(Exercise, Exercise.id == ExerciseTemplateUse.exercise_id)
            .filter(ExerciseTemplateUse.template_id == row.id, Exercise.deleted_at.is_(None)).count())
    if made:
        raise HTTPException(409, f"{made} exercise(s) were made from this template and are kept as part of the "
                                 f"training record; retire the template on the content clock instead")
    row.soft_delete()
    audit(db, user, "delete", "exercise_template", str(row.id))


# ── making an exercise from a template ────────────────────────────────────────────
def create_exercise(db: Session, user: CurrentUser, template_id: uuid.UUID,
                    body: ExerciseFromTemplateIn) -> ExerciseFromTemplateOut:
    tpl = lock_visible(db, user, template_id)
    try:
        rng = get_owned(db, Range, body.range_id, user, not_found="range not found")
    except HTTPException:
        raise HTTPException(422, "range not found") from None
    sc = None
    if tpl.scenario_id is not None:
        try:
            sc = get_owned(db, Scenario, tpl.scenario_id, user, not_found="scenario not found")
        except HTTPException:
            raise HTTPException(422, "The template's scenario belongs to another tenant, so an exercise here cannot "
                                     "run it; copy the scenario into your tenant and name it in a template of "
                                     "your own") from None
    want = proven.effective_range_template(db, tpl)
    if want is not None and rng.template_id != want.id:
        raise HTTPException(422, f"The range was not built from range template '{want.name}', which this "
                                 f"exercise template runs on")
    ex = Exercise(name=body.name, kind="collective", range_id=rng.id, scenario_id=sc.id if sc else None,
                  tenant_id=tenant_uuid(user), state=ExerciseState.pending, max_score=body.max_score or 100)
    db.add(ex)
    db.flush()
    if sc is not None:
        points = scenario_objectives.materialise(db, ex.id, sc.yaml)
        if points:
            ex.max_score = points
    for i, o in enumerate(tpl.objectives or []):
        db.add(ExerciseObjective(exercise_id=ex.id, ref=o["ref"], text=o.get("text", ""), moe=o.get("moe", ""),
                                 competency_code=o.get("competency_code", ""), ordinal=i))
    for m in sorted(tpl.mesl or [], key=lambda m: m["serial"]):
        db.add(MeslEvent(exercise_id=ex.id, status="planned", generated_by_model="",
                         **{k: m.get(k, "") for k in ("serial", "phase", "scenario_time", "title", "description",
                                                      "objective_ref", "attack_technique", "delivery_method",
                                                      "from_cell", "to_participant", "expected_action", "moe")}))
    db.add(ExerciseTemplateUse(exercise_id=ex.id, template_id=tpl.id, template_revision=tpl.revision,
                               tenant_id=tenant_uuid(user)))
    db.flush()
    audit(db, user, "create_from_template", "exercise", str(ex.id), f"template {tpl.id} revision {tpl.revision}")
    return ExerciseFromTemplateOut(
        id=ex.id, name=ex.name, state=ex.state.value, range_id=ex.range_id, scenario_id=ex.scenario_id,
        max_score=ex.max_score, template_id=tpl.id, template_revision=tpl.revision,
        objectives=len(tpl.objectives or []), mesl_events=len(tpl.mesl or []))


def exercises_of(db: Session, user: CurrentUser, template_id: uuid.UUID) -> list[ExerciseMadeFromTemplateOut]:
    """The caller's tenant's exercises made from a template they may read."""
    tpl = get_visible(db, user, template_id)
    rows = (db.query(Exercise, ExerciseTemplateUse.template_revision)
            .join(ExerciseTemplateUse, ExerciseTemplateUse.exercise_id == Exercise.id)
            .filter(ExerciseTemplateUse.template_id == tpl.id, Exercise.tenant_id == tenant_uuid(user),
                    Exercise.deleted_at.is_(None))
            .order_by(Exercise.created_at.desc(), Exercise.id).limit(200).all())
    return [ExerciseMadeFromTemplateOut(id=ex.id, name=ex.name, state=ex.state.value, total_score=ex.total_score,
                                max_score=ex.max_score, template_revision=rev, created_at=ex.created_at)
            for ex, rev in rows]


def save_exercise(db: Session, user: CurrentUser, exercise_id: uuid.UUID,
                  body: ExerciseSaveAsTemplateIn) -> ExerciseTemplate:
    """A template from an existing exercise of the caller's tenant: its objectives, its MESL
    (as planned), its scenario and the range template its range was built from. The exercise
    is not changed and is not 'made from' the new template."""
    ex = get_owned(db, Exercise, exercise_id, user, not_found="Exercise not found")
    rng = db.get(Range, ex.range_id)  # tenant-safe: the caller's own exercise's range
    tenant = tenant_uuid(user)
    scenario_id = ex.scenario_id if _usable(db.get(Scenario, ex.scenario_id) if ex.scenario_id else None, tenant) \
        else None  # tenant-safe: _usable checks tenant or shared
    template_id = rng.template_id if rng is not None and _usable(db.get(Template, rng.template_id), tenant) \
        else None  # tenant-safe: _usable checks tenant or shared
    if ex.kind != "collective":
        raise HTTPException(422, "Only a collective exercise can be saved as a template: courses, not exercise "
                                 "templates, are individual training")
    objectives = [{"ref": o.ref, "text": o.text or "", "moe": o.moe or "", "competency_code": o.competency_code or ""}
                  for o in db.query(ExerciseObjective).filter_by(exercise_id=ex.id)
                  .order_by(ExerciseObjective.ordinal, ExerciseObjective.id).all()]
    mesl = [{k: getattr(m, k) or ("cyber" if k == "delivery_method" else "")
             for k in ("phase", "scenario_time", "title", "description", "objective_ref", "attack_technique",
                       "delivery_method", "from_cell", "to_participant", "expected_action", "moe")}
            | {"serial": m.serial}
            for m in db.query(MeslEvent).filter_by(exercise_id=ex.id).order_by(MeslEvent.serial, MeslEvent.id).all()]
    core = _core({"name": body.name, "description": body.description, "teams": [], "roles": [], "scaling_rules": [],
                  "objectives": objectives, "mesl": mesl, "instructor_notes": "", "tags": []})
    check_refs(db, user, scenario_id, template_id)
    row = ExerciseTemplate(id=uuid.uuid4(), tenant_id=tenant, is_public=False, revision=1, scenario_id=scenario_id,
                           range_template_id=template_id, created_by=_uid(user), **core.model_dump(mode="json"))
    db.add(row)
    db.flush()
    audit(db, user, "create", "exercise_template", str(row.id), f"saved from exercise {ex.id}")
    return row
