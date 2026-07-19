"""CFITES / QSP qualification-spine router.

Import the crosswalk contract and browse the resulting Qualification -> PO -> EO
tree. This is the structured backbone the LMS, forge, and range curriculum panel
hang off. QSP `.docx` chapter text is never handled here (CAF on-box only) — only
the derived `crosswalk.csv` contract is ingested.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import qsp_ingest, qsp_paths
from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import (
    Competency,
    CourseModule,
    EnablingObjective,
    Exercise,
    Lesson,
    LessonObjective,
    ModuleContent,
    ObjectiveCompetencyMap,
    PerformanceObjective,
    Qualification,
    Range,
    RangeObjectiveMap,
    Template,
)

logger = logging.getLogger("truenorth.api.qsp")

router = APIRouter(prefix="/qsp", tags=["qsp"])

MAX_CSV_BYTES = 4 * 1024 * 1024


class LessonBrief(BaseModel):
    id: str
    title: str
    duration_minutes: int
    is_published: bool


class EOOut(BaseModel):
    id: str
    eo_code: str
    title: str
    maps_to_critical_event: str | None = None
    lessons: list[LessonBrief] = []


class CompTag(BaseModel):
    framework: str
    code: str
    name: str
    relation: str


class POOut(BaseModel):
    id: str
    po_code: str
    title: str
    tier: str
    environment: str
    status: str
    duration_min: int
    critical_events: list[str]
    target_role: str
    nice_dcwf_task: str
    scenario_count: int
    enabling_objectives: list[EOOut]
    competencies: list[CompTag] = []
    exercise_id: str | None = None
    scenario_id: str | None = None
    range_id: str | None = None
    course_code: str | None = None
    duration_long: bool = False


class QualificationOut(BaseModel):
    id: str
    qsp_code: str
    nqual: str
    title: str
    component_version: str
    po_count: int


def _po_exercise_refs(db: Session, po_id) -> tuple[str | None, str | None, str | None]:
    """Resolve (exercise_id, scenario_id, range_id) for a PO via its course module's assess content."""
    module = db.query(CourseModule).filter_by(po_id=po_id).first()
    if module is None:
        return (None, None, None)
    assess = (
        db.query(ModuleContent)
        .filter_by(module_id=module.id, content_kind="assess")
        .first()
    )
    if assess is None or assess.scenario_id is None:
        return (None, None, None)
    ex = db.query(Exercise).filter_by(scenario_id=assess.scenario_id).first()
    if ex is None:
        return (None, str(assess.scenario_id), None)
    return (str(ex.id), str(assess.scenario_id), str(ex.range_id))


def _po_competencies(db: Session, po_id) -> list[CompTag]:
    rows = (
        db.query(ObjectiveCompetencyMap, Competency)
        .join(Competency, Competency.id == ObjectiveCompetencyMap.competency_id)
        .filter(ObjectiveCompetencyMap.po_id == po_id)
        .all()
    )
    return [
        CompTag(framework=c.framework.value, code=c.code, name=c.name, relation=m.relation_type)
        for m, c in rows
    ]


def _po_out(
    po: PerformanceObjective,
    eos: list[EnablingObjective],
    lessons_by_eo: dict[str, list[Lesson]] | None = None,
    competencies: list[CompTag] | None = None,
) -> POOut:
    try:
        crit = json.loads(po.critical_events or "[]")
    except json.JSONDecodeError:
        crit = []
    lessons_by_eo = lessons_by_eo or {}
    return POOut(
        id=str(po.id),
        po_code=po.po_code,
        title=po.title,
        tier=po.tier.value,
        environment=po.environment.value,
        status=po.status.value,
        duration_min=po.duration_min,
        critical_events=crit,
        target_role=po.target_role,
        nice_dcwf_task=po.nice_dcwf_task,
        scenario_count=po.scenario_count,
        enabling_objectives=[
            EOOut(
                id=str(e.id),
                eo_code=e.eo_code,
                title=e.title,
                maps_to_critical_event=e.maps_to_critical_event,
                lessons=[
                    LessonBrief(
                        id=str(le.id), title=le.title,
                        duration_minutes=le.duration_minutes, is_published=le.is_published,
                    )
                    for le in lessons_by_eo.get(str(e.id), [])
                ],
            )
            for e in eos
        ],
        competencies=competencies or [],
    )


def _lessons_for_eos(db: Session, eo_ids: list) -> dict[str, list[Lesson]]:
    """Map eo_id -> [Lesson] via the LessonObjective join (published or not)."""
    if not eo_ids:
        return {}
    joins = (
        db.query(LessonObjective, Lesson)
        .join(Lesson, Lesson.id == LessonObjective.lesson_id)
        .filter(LessonObjective.eo_id.in_(eo_ids), Lesson.deleted_at.is_(None))
        .all()
    )
    out: dict[str, list[Lesson]] = {}
    for lo, lesson in joins:
        out.setdefault(str(lo.eo_id), []).append(lesson)
    return out


def _template_curriculum(db: Session, template: Template) -> dict:
    """Build the curriculum payload for a range template: mapped POs + EOs + lessons."""
    maps = db.query(RangeObjectiveMap).filter_by(template_id=template.id).all()
    po_ids = [m.po_id for m in maps]
    objectives: list[POOut] = []
    if po_ids:
        pos = (
            db.query(PerformanceObjective)
            .filter(PerformanceObjective.id.in_(po_ids))
            .order_by(PerformanceObjective.po_code)
            .all()
        )
        for po in pos:
            eos = (
                db.query(EnablingObjective)
                .filter_by(po_id=po.id)
                .order_by(EnablingObjective.eo_code)
                .all()
            )
            lessons_by_eo = _lessons_for_eos(db, [e.id for e in eos])
            objectives.append(_po_out(po, eos, lessons_by_eo))
    return {
        "template_id": str(template.id),
        "template_name": template.name,
        "objective_count": len(objectives),
        "objectives": [o.model_dump() for o in objectives],
    }


@router.post("/import-crosswalk")
async def import_crosswalk(
    file: UploadFile,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Upload crosswalk.csv and upsert the Qualification/PO/EO spine (idempotent)."""
    raw = await file.read()
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="crosswalk too large")
    try:
        csv_text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"crosswalk must be UTF-8 CSV: {exc}") from exc
    try:
        stats = qsp_ingest.import_crosswalk(db, csv_text, tenant_id=user.tenant_id or None)
    except Exception as exc:  # noqa: BLE001 — surface parse/DB errors to the caller
        db.rollback()
        logger.exception("crosswalk import failed")
        raise HTTPException(status_code=422, detail=f"crosswalk import failed: {exc}") from exc
    return {"imported": True, **stats}


@router.get("/qualifications", response_model=list[QualificationOut])
def list_qualifications(
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
) -> list[QualificationOut]:
    quals = db.query(Qualification).order_by(Qualification.qsp_code).all()
    out = []
    for q in quals:
        po_count = db.query(PerformanceObjective).filter_by(qualification_id=q.id).count()
        out.append(
            QualificationOut(
                id=str(q.id), qsp_code=q.qsp_code, nqual=q.nqual, title=q.title,
                component_version=q.component_version, po_count=po_count,
            )
        )
    return out


@router.get("/qualifications/{qsp_code}/objectives", response_model=list[POOut])
def qualification_objectives(
    qsp_code: str,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
) -> list[POOut]:
    qual = db.query(Qualification).filter_by(qsp_code=qsp_code).one_or_none()
    if qual is None:
        raise HTTPException(status_code=404, detail=f"qualification {qsp_code} not found")
    pos = (
        db.query(PerformanceObjective)
        .filter_by(qualification_id=qual.id)
        .order_by(PerformanceObjective.po_code)
        .all()
    )
    result = []
    for po in pos:
        eos = (
            db.query(EnablingObjective)
            .filter_by(po_id=po.id)
            .order_by(EnablingObjective.eo_code)
            .all()
        )
        lessons_by_eo = _lessons_for_eos(db, [e.id for e in eos])
        out = _po_out(po, eos, lessons_by_eo, _po_competencies(db, po.id))
        out.exercise_id, out.scenario_id, out.range_id = _po_exercise_refs(db, po.id)
        out.course_code = qsp_paths.course_code(po, qual)
        out.duration_long = (po.duration_min or 0) >= 480  # 8h+ -> flag for verification
        result.append(out)
    return result


# ── Range ↔ PO linkage — "curriculum in the range section" ────────────────


class AttachObjectives(BaseModel):
    po_ids: list[str]
    source: str = "manual"


@router.post("/templates/{template_id}/objectives")
def attach_template_objectives(
    template_id: str,
    body: AttachObjectives,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Link a range template to one or more POs (idempotent upsert)."""
    template = db.query(Template).filter_by(id=template_id).one_or_none()
    if template is None:
        raise HTTPException(status_code=404, detail="template not found")
    added = 0
    for po_id in body.po_ids:
        po = db.query(PerformanceObjective).filter_by(id=po_id).one_or_none()
        if po is None:
            raise HTTPException(status_code=422, detail=f"unknown po_id {po_id}")
        exists = (
            db.query(RangeObjectiveMap)
            .filter_by(template_id=template.id, po_id=po.id)
            .one_or_none()
        )
        if exists is None:
            db.add(
                RangeObjectiveMap(
                    template_id=template.id, po_id=po.id,
                    source=body.source, tenant_id=user.tenant_id or None,
                )
            )
            added += 1
    db.commit()
    return {"template_id": template_id, "linked": added}


@router.delete("/templates/{template_id}/objectives/{po_id}")
def detach_template_objective(
    template_id: str,
    po_id: str,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
) -> dict:
    row = (
        db.query(RangeObjectiveMap)
        .filter_by(template_id=template_id, po_id=po_id)
        .one_or_none()
    )
    detached = row is not None
    if detached:
        db.delete(row)
        db.commit()
    return {"detached": detached}


@router.get("/templates/{template_id}/curriculum")
def template_curriculum(
    template_id: str,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
) -> dict:
    """The QSP POs/EOs (and their lessons) a range template supports."""
    template = db.query(Template).filter_by(id=template_id).one_or_none()
    if template is None:
        raise HTTPException(status_code=404, detail="template not found")
    return _template_curriculum(db, template)


@router.get("/ranges/{range_id}/curriculum")
def range_curriculum(
    range_id: str,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
) -> dict:
    """The curriculum a provisioned range supports (resolved via its template)."""
    rng = db.query(Range).filter_by(id=range_id).one_or_none()
    if rng is None:
        raise HTTPException(status_code=404, detail="range not found")
    template = db.query(Template).filter_by(id=rng.template_id).one_or_none()
    if template is None:
        raise HTTPException(status_code=404, detail="range template not found")
    payload = _template_curriculum(db, template)
    payload["range_id"] = str(rng.id)
    payload["range_name"] = rng.name
    return payload


# ── NICE / NIST CSF crosswalk + learning-path generation ──────────────────


@router.post("/import-competency-crosswalk")
async def import_competency_crosswalk(
    taxonomy: UploadFile,
    crosswalk: UploadFile,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Seed NIST CSF 2.0 + NICE competencies and link each PO to them (curated, idempotent).

    Upload `taxonomy` = nist_csf_2_0_taxonomy.csv and `crosswalk` = qsp_competency_crosswalk.csv.
    """
    try:
        tax_text = (await taxonomy.read()).decode("utf-8-sig")
        xwalk_text = (await crosswalk.read()).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"seed files must be UTF-8 CSV: {exc}") from exc
    try:
        stats = qsp_paths.import_competency_crosswalk(
            db, tax_text, xwalk_text, tenant_id=user.tenant_id or None
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("competency crosswalk import failed")
        raise HTTPException(status_code=422, detail=f"crosswalk import failed: {exc}") from exc
    return {"imported": True, **stats}


@router.post("/generate-learning-paths")
def generate_learning_paths(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Build per-PO courses, qualification paths, role paths, and the developmental progression."""
    try:
        stats = qsp_paths.generate_learning_paths(db, tenant_id=user.tenant_id or None)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("learning-path generation failed")
        raise HTTPException(status_code=422, detail=f"generation failed: {exc}") from exc
    return {"generated": True, **stats}


@router.post("/generate-exercises")
def generate_exercises(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Scaffold one pending Exercise per PO-course (Scenario from PO + placeholder Range)."""
    try:
        stats = qsp_paths.generate_exercises(db, tenant_id=user.tenant_id or None)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("exercise generation failed")
        raise HTTPException(status_code=422, detail=f"generation failed: {exc}") from exc
    return {"generated": True, **stats}


@router.get("/developmental-progression")
def developmental_progression(
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Qualifications arranged by the QSP-derived developmental progression + specialty streams."""
    quals = db.query(Qualification).order_by(Qualification.dp_order, Qualification.qsp_code).all()
    progression, specialties = [], []
    for q in quals:
        entry = {
            "qsp_code": q.qsp_code, "nqual": q.nqual, "title": q.title,
            "dp_order": q.dp_order, "rank_level": q.rank_level, "track": q.track,
            "po_count": db.query(PerformanceObjective).filter_by(qualification_id=q.id).count(),
        }
        (specialties if q.track == "specialty" else progression).append(entry)
    return {"progression": progression, "specialty_streams": specialties}
