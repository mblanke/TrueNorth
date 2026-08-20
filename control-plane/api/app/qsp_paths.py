"""QSP → curriculum + learning plans, with NICE and NIST CSF 2.0 crosswalks.

All framework mappings are a curated, deterministic seed (no LLM) so the
qualification→framework crosswalk is auditable for accreditation. The developmental
progression is DERIVED FROM the QSPs (rank ladder + specialty streams), not a fixed
DP1-5 scale. Lesson bodies are left as placeholders — GLM/Taz fills them on-box.
"""

from __future__ import annotations

import csv
import io
import json

from sqlalchemy.orm import Session

from . import range_topology

from .models import (
    CompetencyFramework,
    Competency,
    Course,
    CourseModule,
    EnablingObjective,
    Enrollment,
    Exercise,
    ExerciseState,
    Lesson,
    LessonObjective,
    LearningPath,
    ModuleContent,
    ModuleContentType,
    Objective,
    ObjectiveCompetencyMap,
    ObjectiveType,
    PerformanceObjective,
    Qualification,
    Range,
    RangeState,
    Scenario,
    Template,
)

# Developmental progression derived from the QSP rank titles (see qsp_source/INDEX.md).
# Not a fixed DP1-5 scale — a rank ladder (progression) plus parallel specialty streams.
DP_PROGRESSION: dict[str, dict] = {
    "ALJQ":   {"dp_order": 1, "track": "progression", "rank_level": "Pte",
               "title": "Cyber Operator — Cyber Defence Analyst (Pte RQ)"},
    "TEMP67": {"dp_order": 2, "track": "progression", "rank_level": "Cpl",
               "title": "Cyber Operator — Senior Cyber Defense Analyst (Cpl RQ)"},
    "TEMP64": {"dp_order": 2, "track": "specialty", "rank_level": "Cpl",
               "title": "Red Analyst — Adversary Emulation"},
    "ALRA":   {"dp_order": 2, "track": "specialty", "rank_level": "Cpl",
               "title": "Malware Analyst — Reverse Engineer"},
}

# The developmental-period ladder the career map draws. Periods with no ingested
# QSP render as "not yet defined" placeholders so the whole career arc stays visible
# instead of stopping wherever ingest happens to have reached.
#
# Rank labels are ONLY set for periods backed by a source QSP (qsp_source/INDEX.md):
# DP1 = Pte (ALJQ), DP2 = Cpl (TEMP67/TEMP64/ALRA). No CAF source on-box establishes
# the DP3-5 rank progression, so those rungs carry no rank rather than a guessed one —
# fabricated rank data must never reach CAF users. Ingesting a real DP3 QSP fills the
# rung in from `Qualification.rank_level` with no code or UI change.
DP_LADDER: list[dict] = [
    {"dp_order": 1, "rank_level": "Pte", "label": "Basic occupation"},
    {"dp_order": 2, "rank_level": "Cpl", "label": "Journeyman"},
    {"dp_order": 3, "rank_level": "", "label": ""},
    {"dp_order": 4, "rank_level": "", "label": ""},
    {"dp_order": 5, "rank_level": "", "label": ""},
]


# NICE work-role -> short catalog abbreviation for course codes.
_ROLE_ABBREV: dict[str, str] = {
    "cyber defense analyst": "CDA", "cyber defence analyst": "CDA",
    "senior cyber defense analyst": "SCDA", "senior cyber defence analyst": "SCDA",
    "cyber defense incident responder": "CIR", "cyber defence incident responder": "CIR",
    "cyber defense forensics analyst": "CFA", "cyber defence forensics analyst": "CFA",
    "malware reverse engineer": "MRE",
    "adversary emulation (dcwf)": "ADV", "adversary emulation": "ADV",
}


def course_code(po, qual) -> str:
    """Catalog code = work-role abbreviation + PO number, e.g. CDA-007, MRE-001, ALJQ-001-005."""
    role = (po.target_role or "").strip().lower()
    abbrev = _ROLE_ABBREV.get(role)
    if not abbrev:
        if role in ("", "-", "n/a", "todo"):
            abbrev = (qual.nqual.replace("-", "")[:5].upper() if qual else "GEN")
        else:
            abbrev = "".join(w[0] for w in role.split() if w)[:4].upper() or "GEN"
    num = po.po_code.replace("PO_", "")
    return f"{abbrev}-{num}"


def course_meta_of(course: Course) -> dict:
    """`course.course_meta` as a dict, tolerating the unparseable.

    `provenance` marks authored/catalogue content and `retired` marks a stub that
    authored content has superseded; both decide whether a course may be adopted,
    renamed or put on a path, so every caller has to read them the same way.
    """
    try:
        meta = json.loads(course.course_meta or "{}")
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _get_or_create_competency(
    db: Session, framework: CompetencyFramework, code: str, name: str,
    category: str = "", parent_code: str | None = None,
) -> Competency:
    comp = db.query(Competency).filter_by(framework=framework, code=code).one_or_none()
    if comp is None:
        comp = Competency(framework=framework, code=code, name=name or code,
                           category=category, parent_code=parent_code)
        db.add(comp)
        db.flush()
    else:
        if name and comp.name != name:
            comp.name = name
        if category:
            comp.category = category
        if parent_code:
            comp.parent_code = parent_code
    return comp


def seed_nist_csf(db: Session, taxonomy_csv: str) -> int:
    """Upsert NIST CSF 2.0 functions + categories as Competency rows. Returns count seen."""
    reader = csv.DictReader(io.StringIO(taxonomy_csv))
    n = 0
    for row in reader:
        code = (row.get("code") or "").strip()
        if not code:
            continue
        kind = (row.get("kind") or "").strip()
        parent = None if kind == "function" else (row.get("function_code") or "").strip() or None
        _get_or_create_competency(
            db, CompetencyFramework.nist_csf, code, (row.get("name") or "").strip(),
            category=kind, parent_code=parent,
        )
        n += 1
    db.flush()
    return n


def _link(db: Session, po: PerformanceObjective, comp: Competency, relation: str) -> bool:
    exists = (
        db.query(ObjectiveCompetencyMap)
        .filter_by(po_id=po.id, competency_id=comp.id)
        .one_or_none()
    )
    if exists is None:
        db.add(ObjectiveCompetencyMap(po_id=po.id, competency_id=comp.id, relation_type=relation))
        return True
    return False


def import_competency_crosswalk(
    db: Session, taxonomy_csv: str, crosswalk_csv: str, tenant_id: str | None = None
) -> dict:
    """Seed NICE + NIST CSF competencies and link each PO to them. Idempotent."""
    stats = {"nist_csf_seeded": 0, "nice_seeded": 0, "links": 0, "pos_mapped": 0, "unmatched": []}
    stats["nist_csf_seeded"] = seed_nist_csf(db, taxonomy_csv)

    reader = csv.DictReader(io.StringIO(crosswalk_csv))
    nice_codes_seen: set[str] = set()
    for row in reader:
        qsp_code = (row.get("qsp_code") or "").strip()
        po_code = (row.get("po_code") or "").strip()
        if not qsp_code or not po_code:
            continue
        qual = db.query(Qualification).filter_by(qsp_code=qsp_code).one_or_none()
        if qual is None:
            stats["unmatched"].append(f"{qsp_code}/{po_code} (no qualification)")
            continue
        po = (
            db.query(PerformanceObjective)
            .filter_by(qualification_id=qual.id, po_code=po_code)
            .one_or_none()
        )
        if po is None:
            stats["unmatched"].append(f"{qsp_code}/{po_code} (no PO)")
            continue

        # NICE work role (primary)
        role_code = (row.get("nice_work_role") or "").strip()
        if role_code:
            role = _get_or_create_competency(
                db, CompetencyFramework.nice, role_code,
                (row.get("nice_work_role_name") or role_code).strip(), category="work_role",
            )
            nice_codes_seen.add(role_code)
            if _link(db, po, role, "primary"):
                stats["links"] += 1
            # resolve the TODO-map placeholder on the PO
            if not po.nice_dcwf_task or po.nice_dcwf_task.upper().startswith("TODO"):
                po.nice_dcwf_task = role_code

        # NICE tasks (supporting)
        for tid in [t.strip() for t in (row.get("nice_task_ids") or "").split(";") if t.strip()]:
            task = _get_or_create_competency(db, CompetencyFramework.nice, tid, tid, category="task")
            nice_codes_seen.add(tid)
            if _link(db, po, task, "supporting"):
                stats["links"] += 1

        # NIST CSF functions (primary) + categories (supporting) — already seeded by taxonomy
        for fn in [f.strip() for f in (row.get("nist_csf_functions") or "").split(";") if f.strip()]:
            comp = db.query(Competency).filter_by(framework=CompetencyFramework.nist_csf, code=fn).one_or_none()
            if comp and _link(db, po, comp, "primary"):
                stats["links"] += 1
        for cat in [c.strip() for c in (row.get("nist_csf_categories") or "").split(";") if c.strip()]:
            comp = db.query(Competency).filter_by(framework=CompetencyFramework.nist_csf, code=cat).one_or_none()
            if comp and _link(db, po, comp, "supporting"):
                stats["links"] += 1

        stats["pos_mapped"] += 1

    stats["nice_seeded"] = len(nice_codes_seen)
    # developmental progression on qualifications
    for qual in db.query(Qualification).all():
        meta = DP_PROGRESSION.get(qual.qsp_code)
        if meta:
            qual.dp_order = meta["dp_order"]
            qual.track = meta["track"]
            qual.rank_level = meta["rank_level"]
            if not qual.title:
                qual.title = meta["title"]
    db.commit()
    return stats


# ── Learning-plan generation ──────────────────────────────────────────────

def tier_rank(po: PerformanceObjective) -> tuple:
    """Order key: gate first, core next, capstones last, then by po_code."""
    tier_v = po.tier.value if po.tier else "core"
    is_capstone = "capstone" in (po.assessment_type or "").lower()
    tier_order = {"gate": 0, "core": 1}.get(tier_v, 1)
    return (tier_order, 1 if is_capstone else 0, po.po_code)


def _po_course(db: Session, po: PerformanceObjective, qual: Qualification, tenant_id: str | None) -> Course:
    """Get-or-create the reusable per-PO course (module + placeholder EO lessons + scenario link)."""
    code = course_code(po, qual)
    display_name = f"{code} — {po.title}"
    meta = json.dumps({
        "po_code": po.po_code, "qsp_code": qual.qsp_code, "course_code": code,
        "work_role": po.target_role, "environment": po.environment.value,
        "duration_min": po.duration_min,
    })

    # Whatever already delivers this objective wins. Authored content
    # (content/courses/*.yaml) binds modules to POs too, and it is the real delivery
    # vehicle — returning it untouched is what puts it on the generated paths, and it
    # avoids both hazards of building a stub alongside it: renaming the authored course
    # and overwriting its provenance, and creating a second `ordinal=0` claimant that
    # every resolver would prefer, silently shadowing the authored module.
    authored: Course | None = None
    stub_mod: CourseModule | None = None
    for candidate in (
        db.query(CourseModule).filter_by(po_id=po.id).order_by(CourseModule.ordinal).all()
    ):
        cand_course = db.query(Course).filter_by(id=candidate.course_id).one_or_none()
        if cand_course is None:
            continue
        cand_meta = course_meta_of(cand_course)
        if cand_meta.get("provenance"):  # authored content — it delivers this PO
            authored = cand_course
            break
        if stub_mod is None and not cand_meta.get("retired"):
            stub_mod = candidate
    if authored is not None:
        return authored
    if stub_mod is not None:
        course = db.query(Course).filter_by(id=stub_mod.course_id).one()
        course.name = display_name  # refresh title on re-run
        course.course_meta = meta
        course.duration_hours = max(1, round((po.duration_min or 0) / 60))
        db.flush()
        return course

    # Nothing delivers the objective. Revive a retired stub by name rather than adding a
    # second course with the same one — retirement is reversible precisely so that
    # unbinding authored content puts the spine back the way it was.
    course = db.query(Course).filter_by(tenant_id=tenant_id, name=display_name).one_or_none()
    if course is not None:
        course.course_meta = meta  # drops `retired`/`superseded_by`
        course.duration_hours = max(1, round((po.duration_min or 0) / 60))
        existing_mod = (
            db.query(CourseModule)
            .filter_by(course_id=course.id)
            .order_by(CourseModule.ordinal)
            .first()
        )
        if existing_mod is not None:
            existing_mod.po_id = po.id
            db.flush()
            return course
    else:
        course = Course(
            name=display_name,
            description=po.conditions or "",
            tenant_id=tenant_id,
            difficulty="advanced" if po.tier.value == "core" else "intermediate",
            duration_hours=max(1, round((po.duration_min or 0) / 60)),
            qualification_id=qual.id,
            course_meta=meta,
        )
        db.add(course)
    db.flush()

    module = CourseModule(
        course_id=course.id, ordinal=0, title=f"{po.po_code} — {po.title}",
        description=po.conditions or "", content_type=ModuleContentType.scenario,
        po_id=po.id, duration_minutes=po.duration_min or 0,
    )
    db.add(module)
    db.flush()

    # teach: one placeholder Lesson per EO (GLM/Taz fills the body on-box later)
    eos = db.query(EnablingObjective).filter_by(po_id=po.id).order_by(EnablingObjective.eo_code).all()
    ordinal = 0
    for eo in eos:
        lesson = Lesson(
            title=f"EO {eo.eo_code} — {eo.title or po.title}",
            body_markdown="",  # placeholder: generated on-box by GLM/Taz
            duration_minutes=30, is_published=False, tenant_id=tenant_id,
        )
        db.add(lesson)
        db.flush()
        db.add(LessonObjective(lesson_id=lesson.id, eo_id=eo.id))
        db.add(ModuleContent(module_id=module.id, ordinal=ordinal,
                             content_kind="teach", lesson_id=lesson.id))
        ordinal += 1

    # assess: link the PO's assessment scenario if one exists (by po_code in the name)
    scenario = db.query(Scenario).filter(Scenario.name.ilike(f"%{po.po_code}%")).first()
    db.add(ModuleContent(module_id=module.id, ordinal=ordinal, content_kind="assess",
                         scenario_id=scenario.id if scenario else None,
                         external_ref=json.dumps({"po_code": po.po_code})))
    db.flush()
    return course


def _get_or_create_path(db: Session, name: str, description: str, course_ids: list[str],
                        prereq: dict, tenant_id: str | None) -> tuple[LearningPath, bool]:
    lp = db.query(LearningPath).filter_by(name=name).one_or_none()
    created = lp is None
    if created:
        lp = LearningPath(name=name, tenant_id=tenant_id)
        db.add(lp)
    lp.description = description
    lp.course_ids = json.dumps(course_ids)
    lp.prerequisite_graph = json.dumps(prereq)
    lp.is_published = False
    db.flush()
    return lp, created


def _linear_prereq(course_ids: list[str]) -> dict:
    return {course_ids[i]: [course_ids[i - 1]] for i in range(1, len(course_ids))}


def _ordered_unique(course_ids: list[str]) -> list[str]:
    """Drop repeats, keep first-seen order.

    One authored course can deliver several objectives — C302 delivers three TEMP64
    POs — and a path that listed it three times would also build a prerequisite chain
    from the course to itself.
    """
    seen: set[str] = set()
    return [c for c in course_ids if not (c in seen or seen.add(c))]


def drop_course_from_paths(db: Session, course_id: str) -> None:
    """Remove a course from every learning path that lists it, prerequisites included.

    `LearningPath.course_ids` is a JSON array with no foreign key, so deleting a course
    would otherwise leave an id behind that resolves to nothing. The prerequisite graph
    is rewired around the gap rather than merely pruned: dropping a course from the
    middle of a linear chain would leave everything after it unreachable.
    """
    for lp in db.query(LearningPath).all():
        try:
            ids = json.loads(lp.course_ids or "[]")
            prereq = json.loads(lp.prerequisite_graph or "{}")
        except (TypeError, ValueError):
            continue
        if course_id not in ids:
            continue

        inherited = prereq.get(course_id, [])
        ids = [c for c in ids if c != course_id]
        rewired = {}
        for node, deps in prereq.items():
            if node == course_id:
                continue
            new_deps = []
            for d in deps:
                new_deps.extend(inherited if d == course_id else [d])
            rewired[node] = [d for d in dict.fromkeys(new_deps) if d != node]
        lp.course_ids = json.dumps(ids)
        lp.prerequisite_graph = json.dumps(rewired)
    db.flush()


def purge_orphaned_stubs(db: Session, tenant_id: str | None = None) -> int:
    """Delete spine-generated stubs that no longer deliver anything.

    A stub exists only while no real content delivers its objective. Once a course does,
    the stub is a second catalogue entry for one thing. Superseding at import time
    handles the stub that is *holding* the objective; this catches the ones already
    released by an earlier run, which are otherwise invisible dead rows.

    Never touches authored content, the stub still holding an objective (PO_TODO has no
    real deliverer), or anything with enrolments — learner history is not ours to discard
    to tidy a catalogue. Scenarios, exercises and ranges survive: they are provisioned
    infrastructure referenced by id.
    """
    course_q = db.query(Course)
    if tenant_id is not None:
        course_q = course_q.filter(Course.tenant_id == tenant_id)

    removed = 0
    for course in course_q.all():
        meta = course_meta_of(course)
        if meta.get("provenance"):
            continue  # authored or catalogue content
        modules = db.query(CourseModule).filter_by(course_id=course.id).all()
        if any(m.po_id for m in modules):
            continue  # still the deliverer of an objective
        if db.query(Enrollment).filter_by(course_id=course.id).count():
            continue  # someone's record depends on it

        drop_course_from_paths(db, str(course.id))
        for mod in modules:
            db.query(ModuleContent).filter_by(module_id=mod.id).delete()
            db.delete(mod)
        db.flush()
        db.delete(course)
        removed += 1
    db.flush()
    return removed


def generate_learning_paths(db: Session, tenant_id: str | None = None) -> dict:
    """Build per-PO courses + qualification paths + role paths + a developmental progression."""
    stats = {"po_courses": 0, "qualification_paths": 0, "role_paths": 0, "progression_paths": 0}

    quals = db.query(Qualification).order_by(Qualification.dp_order, Qualification.qsp_code).all()

    # 1) one reusable course per PO
    po_course: dict[str, str] = {}  # po.id -> course.id
    for qual in quals:
        pos = db.query(PerformanceObjective).filter_by(qualification_id=qual.id).all()
        for po in pos:
            course = _po_course(db, po, qual, tenant_id)
            po_course[str(po.id)] = str(course.id)
            stats["po_courses"] += 1

    # 2) qualification paths (one per NQual), ordered gate→core→capstone
    for qual in quals:
        pos = sorted(
            db.query(PerformanceObjective).filter_by(qualification_id=qual.id).all(),
            key=tier_rank,
        )
        course_ids = _ordered_unique([po_course[str(po.id)] for po in pos])
        title = qual.title or f"{qual.nqual} qualification"
        _get_or_create_path(
            db, name=f"{qual.qsp_code} — {title}",
            description=f"Qualification learning path for {qual.nqual} ({qual.qsp_code}).",
            course_ids=course_ids, prereq=_linear_prereq(course_ids), tenant_id=tenant_id,
        )
        stats["qualification_paths"] += 1

    # 3) role paths (one per distinct target_role, across NQuals)
    roles: dict[str, list[PerformanceObjective]] = {}
    for po in db.query(PerformanceObjective).all():
        role = (po.target_role or "").strip()
        if role and role not in {"-", "TODO", "n/a"}:
            roles.setdefault(role, []).append(po)
    for role, pos in sorted(roles.items()):
        ordered = sorted(pos, key=tier_rank)
        course_ids = _ordered_unique([po_course[str(po.id)] for po in ordered])
        _get_or_create_path(
            db, name=f"Role: {role}",
            description=f"Career-progression path toward the {role} role across qualifications.",
            course_ids=course_ids, prereq=_linear_prereq(course_ids), tenant_id=tenant_id,
        )
        stats["role_paths"] += 1

    # 4) developmental progression (rank ladder) + specialty streams
    progression_quals = [q for q in quals if q.track == "progression"]
    prog_course_ids: list[str] = []
    for qual in sorted(progression_quals, key=lambda q: q.dp_order):
        pos = sorted(
            db.query(PerformanceObjective).filter_by(qualification_id=qual.id).all(),
            key=tier_rank,
        )
        prog_course_ids.extend(po_course[str(po.id)] for po in pos)
    prog_course_ids = _ordered_unique(prog_course_ids)
    if prog_course_ids:
        _get_or_create_path(
            db, name="Developmental Progression — Cyber Operator",
            description="Rank-ladder progression derived from the QSPs (foundational Pte → senior Cpl).",
            course_ids=prog_course_ids, prereq=_linear_prereq(prog_course_ids), tenant_id=tenant_id,
        )
        stats["progression_paths"] += 1
    for qual in [q for q in quals if q.track == "specialty"]:
        pos = sorted(
            db.query(PerformanceObjective).filter_by(qualification_id=qual.id).all(),
            key=tier_rank,
        )
        course_ids = _ordered_unique([po_course[str(po.id)] for po in pos])
        _get_or_create_path(
            db, name=f"Specialty Stream — {qual.title or qual.nqual}",
            description=f"Specialty developmental stream ({qual.qsp_code}).",
            course_ids=course_ids, prereq=_linear_prereq(course_ids), tenant_id=tenant_id,
        )
        stats["progression_paths"] += 1

    # Stubs released by an earlier import are dead rows until something removes them.
    stats["stubs_purged"] = purge_orphaned_stubs(db, tenant_id)

    db.commit()
    return stats


# ── Exercise scaffolding (one assessment exercise per PO-course) ───────────

def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


# Critical event -> (MITRE technique id, technique name). Curated, deterministic.
CRITICAL_EVENT_TECHNIQUE: dict[str, tuple[str, str]] = {
    "scanning": ("T1046", "Network Service Discovery"),
    "reconnaissance": ("T1595", "Active Scanning"),
    "recon completeness": ("T1595", "Active Scanning"),
    "lateral_movement": ("T1021", "Remote Services"),
    "lateral movement": ("T1021", "Remote Services"),
    "exfiltration": ("T1048", "Exfiltration Over Alternative Protocol"),
    "persistence": ("T1547", "Boot or Logon Autostart Execution"),
    "c2": ("T1071", "Application Layer Protocol"),
    "exploitation objectives": ("T1190", "Exploit Public-Facing Application"),
    "static+dynamic findings": ("T1204", "User Execution"),
    "static analysis findings": ("T1027", "Obfuscated Files or Information"),
    "dynamic analysis findings": ("T1055", "Process Injection"),
    "memory forensics findings": ("T1620", "Reflective Code Loading"),
    "network architecture identification": ("T1046", "Network Service Discovery"),
    "complete reconstructable timeline": ("T1070", "Indicator Removal"),
    "assembly translation + analysis": ("T1027", "Obfuscated Files or Information"),
}


def _technique(ev: str) -> tuple[str, str]:
    key = ev.strip().lower()
    if key in CRITICAL_EVENT_TECHNIQUE:
        return CRITICAL_EVENT_TECHNIQUE[key]
    for k, v in CRITICAL_EVENT_TECHNIQUE.items():
        if k in key or key in k:
            return v
    return ("T1059", "Command and Scripting Interpreter")


def _points(crit: list[str]) -> tuple[int, int, int]:
    """(deliverable_pts, per_crit, remainder) summing to 100."""
    deliverable_pts = 20 if crit else 100
    per = (100 - deliverable_pts) // len(crit) if crit else 0
    remainder = (100 - deliverable_pts) - per * len(crit) if crit else 0
    return deliverable_pts, per, remainder


def _scenario_yaml(po: PerformanceObjective, crit: list[str]) -> str:
    """Deterministic scenario from the PO — MITRE-tagged timeline + objectives + noise floor.

    Objectives use ref_id crit-1..N + deliverable-report (matching the Exercise's Objective rows),
    so the mock runner (run_scenario_v2) walks the timeline and auto-achieves them. Points sum 100.
    """
    deliverable_pts, per, remainder = _points(crit)
    lines = [
        f'name: "{po.po_code}: {po.title}"',
        'version: "1.0"',
        f"po_id: {po.po_code}",
        f"environment: {po.environment.value}",
        f"duration_min: {po.duration_min}",
        "objectives:",
    ]
    for i, ev in enumerate(crit):
        tech, tname = _technique(ev)
        pts = per + (remainder if i == 0 else 0)
        lines += [
            f"  - ref_id: crit-{i + 1}",
            "    type: detection",
            f'    critical_event: "{ev}"',
            f"    attack_technique: {tech}   # {tname}",
            "    validator: validate.opensearch_query",
            "    must_pass: true",
            f"    points: {pts}",
        ]
    lines += [
        "  - ref_id: deliverable-report",
        "    type: deliverable",
        f'    deliverable: "{po.deliverable or "technical report"}"',
        "    validator: validate.deliverable_check",
        f"    points: {deliverable_pts}",
    ]
    # Timeline: one inject per critical event, timed; drives the mock run + the detail view.
    lines.append("timeline:")
    for i, ev in enumerate(crit):
        tech, tname = _technique(ev)
        lines += [
            f'  - t: "{i}:00"',
            f"    action: inject.{_slug(ev)[:24] or 'activity'}",
            f"    attack_technique: {tech}",
            f'    critical_event: "{ev}"',
            f'    description: "Adversary activity: {tname} ({ev})."',
        ]
    if not crit:
        lines += ['  - t: "0:00"', "    action: inject.activity",
                  '    description: "Foundational skills assessment activity."']
    # Noise floor — benign lookalikes the analyst must not mis-attribute.
    lines += [
        "noise_floor:",
        '  - id: nf-01',
        '    description: "Scheduled backup producing large outbound transfer (benign)."',
        '  - id: nf-02',
        '    description: "Administrator remote-management session across hosts (benign)."',
    ]
    return "\n".join(lines) + "\n"


def generate_exercises(db: Session, tenant_id: str | None = None) -> dict:
    """Scaffold one pending Exercise per PO-course: Scenario (from PO) + placeholder Range.

    Idempotent. Ranges are unprovisioned mock placeholders (state=created) for a human to
    provision later; scenarios are structural stubs. No LLM.
    """
    stats = {"scenarios": 0, "ranges": 0, "exercises": 0, "objectives": 0, "wired_modules": 0}

    # 1) retire the earlier shared env-ranges + their exercises (superseded by per-PO ranges)
    for old in db.query(Range).filter(Range.name.like("% Assessment Range")).all():
        for ex in db.query(Exercise).filter_by(range_id=old.id).all():
            db.query(Objective).filter_by(exercise_id=ex.id).delete()
            db.delete(ex)
        db.flush()
        db.delete(old)
    db.flush()

    def _range_for_po(po: PerformanceObjective) -> Range:
        """One placeholder Range per PO, carrying a generated topology diagram."""
        qual = db.query(Qualification).filter_by(id=po.qualification_id).one()
        tmpl_name = f"{qual.qsp_code} {po.po_code} Range"
        template = db.query(Template).filter_by(name=tmpl_name).one_or_none()
        if template is None:
            template = Template(
                name=tmpl_name, tenant_id=tenant_id, is_public=False,
                yaml=f"name: {tmpl_name}\nenvironment: {po.environment.value}\n",
            )
            db.add(template)
            db.flush()
        diagram = range_topology.build_diagram(po)
        rng = db.query(Range).filter_by(name=tmpl_name).one_or_none()
        if rng is None:
            rng = Range(
                name=tmpl_name, template_id=template.id, tenant_id=tenant_id,
                state=RangeState.created, provisioner_backend="mock", diagram_json=diagram,
            )
            db.add(rng)
            db.flush()
            stats["ranges"] += 1
        else:
            rng.diagram_json = diagram  # refresh topology on re-run
        return rng

    # 2) per PO-course (module carries po_id), create scenario + exercise + objectives
    modules = db.query(CourseModule).filter(CourseModule.po_id.isnot(None)).all()
    for module in modules:
        po = db.query(PerformanceObjective).filter_by(id=module.po_id).one_or_none()
        if po is None:
            continue
        try:
            crit = json.loads(po.critical_events or "[]")
        except json.JSONDecodeError:
            crit = []
        # skip stub POs with no real content (e.g. PO_TODO)
        if po.status.value == "needs_spec":
            continue

        scen_name = f"{po.po_code}: {po.title}"
        scenario = db.query(Scenario).filter_by(name=scen_name).one_or_none()
        if scenario is None:
            scenario = Scenario(name=scen_name, yaml=_scenario_yaml(po, crit), tenant_id=tenant_id)
            db.add(scenario)
            db.flush()
            stats["scenarios"] += 1
        else:
            scenario.yaml = _scenario_yaml(po, crit)  # refresh with enriched timeline

        # wire the delivering module's assess ModuleContent to this scenario
        assess = (
            db.query(ModuleContent)
            .filter_by(module_id=module.id, content_kind="assess")
            .first()
        )
        if assess is None:
            # Authored modules arrive with no ModuleContent at all, so there is nothing
            # to rewire. Without this the exercise/scenario/range deep links vanish from
            # the career map the moment authored content supersedes a stub: the stub kept
            # the assess row, but the stub no longer claims the objective.
            next_ordinal = (
                db.query(ModuleContent).filter_by(module_id=module.id).count()
            )
            assess = ModuleContent(
                module_id=module.id, ordinal=next_ordinal, content_kind="assess",
                scenario_id=scenario.id,
                external_ref=json.dumps({"po_code": po.po_code}),
            )
            db.add(assess)
            db.flush()
            stats["wired_modules"] += 1
        elif assess.scenario_id != scenario.id:
            assess.scenario_id = scenario.id
            stats["wired_modules"] += 1

        rng = _range_for_po(po)
        qual = db.query(Qualification).filter_by(id=po.qualification_id).one()
        ex_name = f"{course_code(po, qual)} — {po.title}"
        exercise = db.query(Exercise).filter_by(scenario_id=scenario.id).one_or_none()
        if exercise is None:
            exercise = Exercise(
                name=ex_name, range_id=rng.id, scenario_id=scenario.id,
                state=ExerciseState.pending, tenant_id=tenant_id,
            )
            db.add(exercise)
            db.flush()
            stats["exercises"] += 1

            # objectives mirroring the scenario (one per critical event + deliverable; sum to 100)
            deliverable_pts = 20 if crit else 100
            per = (100 - deliverable_pts) // len(crit) if crit else 0
            remainder = (100 - deliverable_pts) - per * len(crit) if crit else 0
            for i, ev in enumerate(crit):
                db.add(Objective(
                    exercise_id=exercise.id, ref_id=f"crit-{i + 1}",
                    objective_type=ObjectiveType.detection,
                    validator="validate.opensearch_query",
                    points=per + (remainder if i == 0 else 0), achieved=False,
                    evidence=ev, competency_code=po.nice_dcwf_task or "",
                ))
                stats["objectives"] += 1
            db.add(Objective(
                exercise_id=exercise.id, ref_id="deliverable-report",
                objective_type=ObjectiveType.deliverable,
                validator="validate.deliverable_check",
                points=deliverable_pts, achieved=False,
                evidence=po.deliverable or "technical report",
                competency_code=po.nice_dcwf_task or "",
            ))
            stats["objectives"] += 1
        elif exercise.name != ex_name:
            exercise.name = ex_name  # refresh title on re-run

    db.commit()
    return stats


def po_coverage(db: Session, tenant_id: str | None = None) -> dict:
    """Which performance objectives have a course module delivering them.

    ``CourseModule.po_id`` is the only link ``qsp_progress`` walks, so a PO with no
    module is a PO no learner can make progress against — it renders on the career
    map as permanently not-started regardless of what courseware exists. This report
    is what Standards needs in order to decide which authored modules should be
    mapped to which objectives.
    """
    qual_q = db.query(Qualification)
    course_q = db.query(Course)
    if tenant_id is not None:
        qual_q = qual_q.filter(Qualification.tenant_id == tenant_id)
        course_q = course_q.filter(Course.tenant_id == tenant_id)
    quals = qual_q.order_by(Qualification.dp_order, Qualification.qsp_code).all()
    qual_by_id = {str(q.id): q for q in quals}

    if not quals:
        return {"objective_count": 0, "covered": 0, "uncovered": 0,
                "unbound_modules": 0, "objectives": []}

    pos = (
        db.query(PerformanceObjective)
        .filter(PerformanceObjective.qualification_id.in_([q.id for q in quals]))
        .all()
    )
    # Retired stubs no longer deliver anything — they released their `po_id` when
    # authored content superseded them — so counting them would overstate coverage.
    courses = [c for c in course_q.all() if not course_meta_of(c).get("retired")]
    course_names = {str(c.id): c.name for c in courses}
    course_ids = [c.id for c in courses]

    modules_by_po: dict[str, list[CourseModule]] = {}
    module_q = db.query(CourseModule).filter(CourseModule.course_id.in_(course_ids))
    for module in module_q.filter(CourseModule.po_id.isnot(None)).all():
        modules_by_po.setdefault(str(module.po_id), []).append(module)

    objectives = []
    for po in sorted(pos, key=lambda p: (qual_by_id.get(str(p.qualification_id)).dp_order
                                         if qual_by_id.get(str(p.qualification_id)) else 0,
                                         p.po_code)):
        qual = qual_by_id.get(str(po.qualification_id))
        delivering = modules_by_po.get(str(po.id), [])
        objectives.append({
            "qsp_code": qual.qsp_code if qual else "",
            "dp_order": qual.dp_order if qual else 0,
            "po_code": po.po_code,
            "title": po.title,
            "tier": po.tier.value if po.tier else "",
            "status": po.status.value if po.status else "",
            "module_count": len(delivering),
            "delivered_by": [
                {"course": course_names.get(str(m.course_id), ""), "module": m.title}
                for m in delivering
            ],
        })

    covered = sum(1 for o in objectives if o["module_count"] > 0)
    unbound_modules = (
        db.query(CourseModule)
        .filter(CourseModule.course_id.in_(course_ids), CourseModule.po_id.is_(None))
        .count()
    )
    return {
        "objective_count": len(objectives),
        "covered": covered,
        "uncovered": len(objectives) - covered,
        "unbound_modules": unbound_modules,
        "objectives": objectives,
    }


# ── Programme courses on the developmental path ───────────────────────────


def programme_courses(db: Session, tenant_id: str | None = None) -> dict[tuple, list[dict]]:
    """Catalogue courses grouped by the node they belong to on the career map.

    Placement and delivery are two independent axes, and conflating them in
    `Course.qualification_id` is what made the imported catalogue invisible:

    * **Placement** — which DP column and term a course is *taught* in — comes from the
      programme catalogue (`course_meta.dp_order` / `institution` / `term_code`), which
      `programme_ingest` already stores. DP1 is the Algonquin College programme, DP2 the
      Royal Military College one.
    * **Delivery** — which objective a module *satisfies* — stays `CourseModule.po_id`,
      the single link `qsp_progress` walks.

    A course therefore belongs to the DP column its catalogue row names, and within it to
    the lane its qualification names. When the two disagree — three Algonquin Year-3
    courses (C302, C305, C306) deliver DP2 specialty objectives — the course stays in its
    catalogue column's `progression` lane and carries the delivery as a cross-DP note,
    rather than being pulled forward into a period it is not taught in.

    Keyed by `(dp_order, track_key)` to match the map's node grid. Three queries total.
    """
    course_q = db.query(Course)
    if tenant_id is not None:
        course_q = course_q.filter(Course.tenant_id == tenant_id)

    courses = []
    for course in course_q.all():
        meta = course_meta_of(course)
        # Stubs carry no `programme`; retired ones are superseded. Neither is catalogue
        # content, and neither belongs on a developmental path.
        if meta.get("retired") or not meta.get("programme"):
            continue
        courses.append((course, meta))
    if not courses:
        return {}

    quals = {str(q.id): q for q in db.query(Qualification).all()}

    # po_id -> (qualification, po), for the delivery notes.
    delivers: dict[str, list[dict]] = {}
    rows = (
        db.query(CourseModule, PerformanceObjective)
        .join(PerformanceObjective, CourseModule.po_id == PerformanceObjective.id)
        .filter(CourseModule.course_id.in_([c.id for c, _ in courses]))
        .all()
    )
    for module, po in rows:
        qual = quals.get(str(po.qualification_id))
        delivers.setdefault(str(module.course_id), []).append({
            "qsp_code": qual.qsp_code if qual else "",
            "dp_order": qual.dp_order if qual else 0,
            "po_code": po.po_code,
            "po_title": po.title,
            "module_id": str(module.id),
            "module_title": module.title,
        })

    grouped: dict[tuple, list[dict]] = {}
    for course, meta in courses:
        dp_order = meta.get("dp_order") or 0
        qual = quals.get(str(course.qualification_id)) if course.qualification_id else None
        # The lane is the course's own qualification, but only when that qualification
        # sits in the same DP column. Otherwise the course stays on the core progression
        # of the period that actually teaches it.
        if qual is not None and qual.dp_order == dp_order and qual.track == "specialty":
            track_key = qual.nqual or qual.qsp_code
        else:
            track_key = "progression"

        entries = sorted(
            delivers.get(str(course.id), []), key=lambda d: (d["dp_order"], d["po_code"])
        )
        grouped.setdefault((dp_order, track_key), []).append({
            "course_id": str(course.id),
            "course_code": meta.get("course_code") or "",
            "name": course.name,
            "institution": meta.get("institution") or "",
            "term_code": meta.get("term_code") or "",
            "term_label": meta.get("term_label") or "",
            "term_start": meta.get("term_start") or "",
            "duration_hours": course.duration_hours or 0,
            "difficulty": course.difficulty or "",
            "is_published": bool(course.is_published),
            "delivers": entries,
            # True when the course is taught in one DP but delivers into another.
            "delivers_cross_dp": any(d["dp_order"] != dp_order for d in entries),
        })

    for key in grouped:
        grouped[key].sort(key=lambda c: (c["term_start"], c["term_code"], c["course_code"]))
    return grouped
