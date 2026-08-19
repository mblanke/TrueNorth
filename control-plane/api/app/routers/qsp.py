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

from .. import qsp_ingest, qsp_paths, qsp_progress
from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..models import (
    Competency,
    ContentKind,
    CourseModule,
    EnablingObjective,
    Exercise,
    Lesson,
    LessonObjective,
    ModuleContent,
    ObjectiveCompetencyMap,
    PerformanceObjective,
    POTier,
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
    # The assessment brief — what the candidate is given, how they are tested,
    # what counts as a pass, and what they hand in. Ingested from the crosswalk
    # since the first import but never surfaced, which left long assessments
    # looking unexplained.
    conditions: str = ""
    assessment_type: str = ""
    pass_standard: str = ""
    deliverable: str = ""
    build_hours: int = 0
    enabling_objectives: list[EOOut]
    competencies: list[CompTag] = []
    exercise_id: str | None = None
    scenario_id: str | None = None
    range_id: str | None = None
    course_code: str | None = None
    duration_long: bool = False
    # Learner state for the calling user; only populated by the career map, which
    # is the one view that resolves enrolments. Elsewhere it stays "not_started".
    progress_state: str = qsp_progress.NOT_STARTED


class QualificationOut(BaseModel):
    id: str
    qsp_code: str
    nqual: str
    title: str
    component_version: str
    po_count: int


# ── Batched lookups ───────────────────────────────────────────────────────
# Every PO-adjacent lookup takes the whole list of PO ids at once and runs a
# constant number of queries, so serialising a qualification (or the entire
# spine) never scales its query count with its objective count.


def _eos_for_pos(db: Session, po_ids: list) -> dict[str, list[EnablingObjective]]:
    """Map po_id -> [EnablingObjective] in one query, ordered by eo_code."""
    if not po_ids:
        return {}
    rows = (
        db.query(EnablingObjective)
        .filter(EnablingObjective.po_id.in_(po_ids))
        .order_by(EnablingObjective.eo_code)
        .all()
    )
    out: dict[str, list[EnablingObjective]] = {}
    for eo in rows:
        out.setdefault(str(eo.po_id), []).append(eo)
    return out


def _competencies_for_pos(db: Session, po_ids: list) -> dict[str, list[CompTag]]:
    """Map po_id -> [CompTag] in one join query."""
    if not po_ids:
        return {}
    rows = (
        db.query(ObjectiveCompetencyMap, Competency)
        .join(Competency, Competency.id == ObjectiveCompetencyMap.competency_id)
        .filter(ObjectiveCompetencyMap.po_id.in_(po_ids))
        .all()
    )
    out: dict[str, list[CompTag]] = {}
    for m, c in rows:
        out.setdefault(str(m.po_id), []).append(
            CompTag(framework=c.framework.value, code=c.code, name=c.name, relation=m.relation_type)
        )
    return out


def _modules_for_pos(db: Session, po_ids: list) -> dict[str, CourseModule]:
    """Map po_id -> its course module (first by ordinal). One query."""
    if not po_ids:
        return {}
    rows = (
        db.query(CourseModule)
        .filter(CourseModule.po_id.in_(po_ids))
        .order_by(CourseModule.ordinal)
        .all()
    )
    out: dict[str, CourseModule] = {}
    for module in rows:
        out.setdefault(str(module.po_id), module)
    return out


def _exercise_refs_for_pos(
    db: Session, po_ids: list, modules_by_po: dict[str, CourseModule] | None = None
) -> dict[str, tuple[str | None, str | None, str | None]]:
    """Map po_id -> (exercise_id, scenario_id, range_id). Three queries regardless of PO count.

    Pass `modules_by_po` when the caller already loaded the modules (see
    `_modules_for_pos`) to save the repeat query.
    """
    if not po_ids:
        return {}
    modules_by_po = _modules_for_pos(db, po_ids) if modules_by_po is None else modules_by_po
    if not modules_by_po:
        return {}

    module_ids = [m.id for m in modules_by_po.values()]
    assess_by_module: dict[str, ModuleContent] = {}
    for content in (
        db.query(ModuleContent)
        .filter(
            ModuleContent.module_id.in_(module_ids),
            ModuleContent.content_kind == ContentKind.assess,
        )
        .order_by(ModuleContent.ordinal)
        .all()
    ):
        assess_by_module.setdefault(str(content.module_id), content)

    scenario_ids = [c.scenario_id for c in assess_by_module.values() if c.scenario_id is not None]
    ex_by_scenario: dict[str, Exercise] = {}
    if scenario_ids:
        for ex in db.query(Exercise).filter(Exercise.scenario_id.in_(scenario_ids)).all():
            ex_by_scenario.setdefault(str(ex.scenario_id), ex)

    out: dict[str, tuple[str | None, str | None, str | None]] = {}
    for po_id, module in modules_by_po.items():
        assess = assess_by_module.get(str(module.id))
        if assess is None or assess.scenario_id is None:
            out[po_id] = (None, None, None)
            continue
        ex = ex_by_scenario.get(str(assess.scenario_id))
        if ex is None:
            out[po_id] = (None, str(assess.scenario_id), None)
        else:
            out[po_id] = (str(ex.id), str(assess.scenario_id), str(ex.range_id))
    return out


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
        conditions=po.conditions,
        assessment_type=po.assessment_type,
        pass_standard=po.pass_standard,
        deliverable=po.deliverable,
        build_hours=po.build_hours,
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
        eos_by_po = _eos_for_pos(db, [po.id for po in pos])
        lessons_by_eo = _lessons_for_eos(db, [e.id for eos in eos_by_po.values() for e in eos])
        objectives = [_po_out(po, eos_by_po.get(str(po.id), []), lessons_by_eo) for po in pos]
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
    return _POContext(db, [po.id for po in pos]).serialise(pos, qual)


class _POContext:
    """Every batched lookup a set of POs needs, resolved once up front.

    Build this over *all* the POs a response will mention — not per qualification —
    otherwise the N+1 simply moves up a level. `modules_by_po` is exposed because
    the learner overlay needs the same rows and should not re-query them.
    """

    def __init__(self, db: Session, po_ids: list):
        self.eos_by_po = _eos_for_pos(db, po_ids)
        self.lessons_by_eo = _lessons_for_eos(
            db, [e.id for eos in self.eos_by_po.values() for e in eos]
        )
        self.comps_by_po = _competencies_for_pos(db, po_ids)
        self.modules_by_po = _modules_for_pos(db, po_ids)
        self.refs_by_po = _exercise_refs_for_pos(db, po_ids, self.modules_by_po)

    def serialise(self, pos: list[PerformanceObjective], qual: Qualification) -> list[POOut]:
        result = []
        for po in pos:
            key = str(po.id)
            out = _po_out(
                po, self.eos_by_po.get(key, []), self.lessons_by_eo, self.comps_by_po.get(key, [])
            )
            out.exercise_id, out.scenario_id, out.range_id = self.refs_by_po.get(
                key, (None, None, None)
            )
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


# ── Career map ────────────────────────────────────────────────────────────


def _stages(quals: list[Qualification]) -> list[dict]:
    """The DP columns: every ladder rung, with un-ingested ones flagged `planned`.

    Rank labels come from the ingested qualifications where they exist and fall back
    to `DP_LADDER` otherwise, so real data always overrides the provisional config.
    """
    ranks_by_dp: dict[int, str] = {}
    for q in quals:
        if q.rank_level and q.dp_order:
            ranks_by_dp.setdefault(q.dp_order, q.rank_level)
    populated = {q.dp_order for q in quals}

    stages = []
    for rung in qsp_paths.DP_LADDER:
        dp = rung["dp_order"]
        stages.append({
            "dp_order": dp,
            "rank_level": ranks_by_dp.get(dp, rung["rank_level"]),
            "label": rung["label"],
            "planned": dp not in populated,
        })
    # A qualification ingested beyond the configured ladder still gets a column.
    for dp in sorted(populated - {r["dp_order"] for r in qsp_paths.DP_LADDER}):
        if dp:
            stages.append({
                "dp_order": dp, "rank_level": ranks_by_dp.get(dp, ""),
                "label": "", "planned": False,
            })
    return sorted(stages, key=lambda s: s["dp_order"])


def _tracks(quals: list[Qualification]) -> list[dict]:
    """The swimlanes: one shared rank-ladder lane on top, then one per specialty stream.

    Specialty lanes are ordered by the DP they fork at and then by their displayed
    label, so the legend reads in the order someone scanning the map sees it.
    """
    specialties = [q for q in quals if q.track == "specialty"]
    return [{"key": "progression", "label": "Core progression", "kind": "progression"}] + [
        {
            "key": q.nqual or q.qsp_code,
            "label": q.title or q.nqual or q.qsp_code,
            "kind": "specialty",
        }
        for q in sorted(specialties, key=lambda q: (q.dp_order, q.title or q.nqual or q.qsp_code))
    ]


def _edges(quals: list[Qualification], stages: list[dict]) -> list[dict]:
    """Derive the path edges. Nothing is stored — the ladder implies its own topology.

    - `progression`: consecutive DPs along the rank ladder.
    - `branch`: from the last rank-ladder qualification below a specialty's DP into it,
      i.e. you earn the core qualification before forking into Red or Malware.
    - `planned`: from the end of the real ladder into the first placeholder column.
    """
    ladder = sorted(
        (q for q in quals if q.track != "specialty"), key=lambda q: (q.dp_order, q.qsp_code)
    )
    edges = [
        {"from": a.qsp_code, "to": b.qsp_code, "kind": "progression"}
        for a, b in zip(ladder, ladder[1:])
        if a.dp_order != b.dp_order
    ]

    for spec in sorted(
        (q for q in quals if q.track == "specialty"), key=lambda q: (q.dp_order, q.qsp_code)
    ):
        parents = [q for q in ladder if q.dp_order < spec.dp_order]
        if parents:
            edges.append({"from": parents[-1].qsp_code, "to": spec.qsp_code, "kind": "branch"})

    planned = [s for s in stages if s["planned"] and (not ladder or s["dp_order"] > ladder[-1].dp_order)]
    if ladder and planned:
        edges.append({
            "from": ladder[-1].qsp_code,
            "to": f"planned:{planned[0]['dp_order']}",
            "kind": "planned",
        })
    return edges


@router.get("/po-coverage")
def po_coverage(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Which performance objectives have a module delivering them, and which do not.

    A PO with no module can never show progress on the career map, because
    `CourseModule.po_id` is the only link the progress resolver walks. This is the
    working list for deciding which authored modules Standards should map.
    """
    return qsp_paths.po_coverage(db, tenant_id=user.tenant_id or None)


@router.get("/curriculum-map")
def curriculum_map(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """The whole developmental career map in one payload.

    Stages (DP columns), tracks (swimlanes), qualification nodes with their objectives
    inline, derived prerequisite edges, and the caller's own position on the path.
    Deliberately a single aggregated response — the page it backs used to fan out one
    request per qualification and re-query per objective.
    """
    quals = db.query(Qualification).order_by(Qualification.dp_order, Qualification.qsp_code).all()
    stages = _stages(quals)
    tracks = _tracks(quals)
    edges = _edges(quals, stages)

    pos = (
        db.query(PerformanceObjective)
        .filter(PerformanceObjective.qualification_id.in_([q.id for q in quals]))
        .all()
        if quals
        else []
    )
    pos_by_qual: dict[str, list[PerformanceObjective]] = {}
    for po in pos:
        pos_by_qual.setdefault(str(po.qualification_id), []).append(po)

    # One lookup pass over every objective on the map, shared by all qualifications.
    ctx = _POContext(db, [po.id for po in pos])
    progress_by_po = qsp_progress.po_progress(db, user.id, [po.id for po in pos], ctx.modules_by_po)

    nodes = []
    node_tuples: list[qsp_progress.Node] = []
    for qual in quals:
        # Path order, not code order: gate first, then core, capstones last —
        # the same ordering the generated learning paths use.
        qual_pos = sorted(pos_by_qual.get(str(qual.id), []), key=qsp_paths.tier_rank)
        objectives = ctx.serialise(qual_pos, qual)

        po_states: list[tuple[str, str]] = []
        for po, out in zip(qual_pos, objectives):
            state = progress_by_po.get(str(po.id), qsp_progress.NOT_STARTED)
            out.progress_state = state
            po_states.append((po.po_code, state))

        node_tuples.append((qual.qsp_code, qual.dp_order, qual.track, po_states))
        nodes.append({
            "qsp_code": qual.qsp_code,
            "nqual": qual.nqual,
            "title": qual.title or qual.nqual,
            "dp_order": qual.dp_order,
            "track": qual.track,
            "track_key": "progression" if qual.track != "specialty" else (qual.nqual or qual.qsp_code),
            "rank_level": qual.rank_level,
            "po_count": len(qual_pos),
            "gate_count": sum(1 for po in qual_pos if po.tier == POTier.gate),
            "total_minutes": sum(po.duration_min or 0 for po in qual_pos),
            # How many objectives actually carry a duration. Much of the spine is
            # still unscoped, so a bare total reads as complete when it is not —
            # the UI needs to know the sum is a floor, not a figure.
            "timed_po_count": sum(1 for po in qual_pos if (po.duration_min or 0) > 0),
            "progress": qsp_progress.aggregate(po_states).as_dict(),
            "objectives": [o.model_dump() for o in objectives],
        })

    states = qsp_progress.node_states(node_tuples, [(e["from"], e["to"]) for e in edges])
    for node in nodes:
        node["state"] = states.get(node["qsp_code"], qsp_progress.AVAILABLE)

    return {
        "stages": stages,
        "tracks": tracks,
        "nodes": nodes,
        "edges": edges,
        "learner": qsp_progress.current_position(node_tuples, states).as_dict(),
    }
