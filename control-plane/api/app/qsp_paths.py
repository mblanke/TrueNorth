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

def _tier_rank(po: PerformanceObjective) -> tuple:
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

    existing_mod = db.query(CourseModule).filter_by(po_id=po.id).first()
    if existing_mod is not None:
        course = db.query(Course).filter_by(id=existing_mod.course_id).one()
        course.name = display_name  # refresh title on re-run
        course.course_meta = meta
        course.duration_hours = max(1, round((po.duration_min or 0) / 60))
        db.flush()
        return course

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
            key=_tier_rank,
        )
        course_ids = [po_course[str(po.id)] for po in pos]
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
        ordered = sorted(pos, key=_tier_rank)
        course_ids = [po_course[str(po.id)] for po in ordered]
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
            key=_tier_rank,
        )
        prog_course_ids.extend(po_course[str(po.id)] for po in pos)
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
            key=_tier_rank,
        )
        course_ids = [po_course[str(po.id)] for po in pos]
        _get_or_create_path(
            db, name=f"Specialty Stream — {qual.title or qual.nqual}",
            description=f"Specialty developmental stream ({qual.qsp_code}).",
            course_ids=course_ids, prereq=_linear_prereq(course_ids), tenant_id=tenant_id,
        )
        stats["progression_paths"] += 1

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

        # wire the course's assess ModuleContent to this scenario
        assess = (
            db.query(ModuleContent)
            .filter_by(module_id=module.id, content_kind="assess")
            .first()
        )
        if assess is not None and assess.scenario_id != scenario.id:
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
