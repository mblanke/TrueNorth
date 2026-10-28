"""Whether a course, range template or scenario is **proven** (owner decision 2026-10-27).

Computed from what the site holds now; nothing here is stored. An item is proven when all
three conditions hold:

``certified``
    A range built from it reached **Certified for Students** (ADR 0013 §6: its certification
    record's verdict) **from the template as it is now**: the record's plan hash is the
    SHA-256 of the template's current YAML, so editing a template unproves it until a range
    built from the new version is certified. A scenario needs its range template's
    certification; a course needs the certification of every range template it uses (through
    its bookings and its scenarios). A course that uses no range has nothing to certify: the
    condition holds and says so (``not_applicable``).
``used``
    A class or exercise using it finished with passing evidence: a **closed class**
    (ADR 0014 §13, #303) with at least one Student whose result is ``pass``, or a
    **completed exercise** whose score reached ``PASS_PERCENT`` of its maximum. A template is
    used by the classes whose ranges or sessions were built from it and by exercises on
    ranges built from it; a scenario by the classes whose sessions ran it and by exercises
    that ran it; a course by its own closed classes.
``fresh``
    The content clock (ADR 0014 §22-25, #295, #312) tracks it, and it is neither stale (a
    review overdue, a release or vulnerability feed flag, or either upstream) nor retired.

An **exercise template** (ADR 0017) is proven the same way: its range template (or, failing
that, its scenario's) must be certified, and the exercises that count as use are those *made
from the template* (``exercise_template_uses``) that completed with a passing score; a
completed exercise on the same range that was not made from it proves nothing about it.

The evidence counted is the caller's tenant's, or every tenant's for the platform
administrator (``scope``): a tenant never learns of another tenant's classes or ranges.

Dependencies are read from the content itself (not the content clock's hourly edges, so a
bundle never misses one): a scenario's ``range_template`` (its id, else its name: the
scenario's tenant first, then a public or shared template, as the clock resolves it); a
course's module scenarios (``module_content.scenario_id`` and the legacy ``content_ref``)
and the range templates its bookings (not cancelled or rejected) and scenarios use. Only
content in the item's own scope counts: another tenant's private template a booking named
is not this course's content.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from .. import safe_yaml
from ..classes.models import Class, ClassCloseout, ClassRange, ClassResult, ClassSession, ClassStatus, ResultOutcome
from ..content_review.models import ContentReviewItem
from ..exercise_templates.models import ExerciseTemplate, ExerciseTemplateUse
from ..models import Course, CourseModule, Exercise, ExerciseState, ModuleContent, Range, Scenario, Template
from ..range_certification import sections as sec
from ..range_certification.models import RangeCertificationEntry, RangeCertificationRecord
from ..range_certification.service import plan_hash
from ..scheduler.models import EventState, ScheduledEvent

PORTABLE = ("course", "range_template", "scenario", "exercise_template")
LABELS = {"course": "course", "range_template": "range template", "scenario": "scenario",
          "exercise_template": "exercise template"}
PASS_PERCENT = 70  # a completed exercise passes at 70% of its maximum, CourseModule.pass_threshold's default
MAX_RECORDS = 50  # certification records read per template, newest first
CERTIFIED, USED, FRESH = "certified", "used", "fresh"
CONDITION_TITLES = {CERTIFIED: "Range certified", USED: "Used with passing evidence", FRESH: "Not stale"}

Scope = set[uuid.UUID] | None  # the tenants whose evidence counts; None: every tenant


@dataclass
class Evidence:
    kind: str  # certification | class | exercise | review
    id: str
    label: str
    at: datetime | None = None


@dataclass
class Condition:
    name: str
    met: bool
    reasons: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    not_applicable: bool = False


@dataclass
class Dependency:
    item_type: str
    item_id: str
    title: str | None  # None: missing, deleted or outside the item's scope


@dataclass
class Status:
    item_type: str
    item_id: str
    title: str
    conditions: list[Condition]
    dependencies: list[Dependency]
    golden_builds: list[dict]

    @property
    def proven(self) -> bool:
        return all(c.met for c in self.conditions)

    @property
    def reasons(self) -> list[str]:
        return [r for c in self.conditions if not c.met for r in c.reasons]


# ── loading and scope ───────────────────────────────────────────────────────────
def load(db: Session, item_type: str, item_id) -> Course | Template | Scenario | ExerciseTemplate | None:
    """The item, or None (unknown type, bad id, deleted). Not tenant-checked: see ``visible``."""
    model = {"course": Course, "range_template": Template, "scenario": Scenario,
             "exercise_template": ExerciseTemplate}.get(item_type)
    try:
        oid = item_id if isinstance(item_id, uuid.UUID) else uuid.UUID(str(item_id))
    except (TypeError, ValueError):
        return None
    if model is None:
        return None
    q = db.query(model).filter(model.id == oid)
    if hasattr(model, "deleted_at"):
        q = q.filter(model.deleted_at.is_(None))
    return q.first()


def visible(row, tenant: uuid.UUID) -> bool:
    """The caller's tenant's own content, or shared content (a public template or scenario,
    a course of no tenant), as the templates, scenarios and courses lists show it."""
    if row.tenant_id is None or row.tenant_id == tenant:
        return True
    return bool(getattr(row, "is_public", False))


def _in_item_scope(dep, owner) -> bool:
    """``dep`` may be part of ``owner``'s content: the same tenant, or shared."""
    return dep.tenant_id is None or dep.tenant_id == owner.tenant_id or bool(getattr(dep, "is_public", False))


def _tenant_filter(q, column, scope: Scope):
    return q if scope is None else q.filter(column.in_(scope))


def _doc(text: str | None) -> dict:
    try:
        doc = safe_yaml.load(text or "")
    except Exception:  # noqa: BLE001 - unparseable or refused YAML names nothing
        return {}
    return doc if isinstance(doc, dict) else {}


# ── dependencies ────────────────────────────────────────────────────────────────
def scenario_template(db: Session, sc: Scenario) -> Template | None:
    """The range template a scenario names (``range_template``: an id, else a name)."""
    ref = str(_doc(sc.yaml).get("range_template") or "").strip()
    if not ref:
        return None
    try:
        by_id = load(db, "range_template", uuid.UUID(ref))
    except ValueError:
        by_id = None
    if by_id is not None:
        return by_id if _in_item_scope(by_id, sc) else None
    named = (db.query(Template).filter(Template.name == ref, Template.deleted_at.is_(None))
             .order_by(Template.created_at, Template.id).all())
    own = [t for t in named if sc.tenant_id is not None and t.tenant_id == sc.tenant_id]
    shared = [t for t in named if t.tenant_id is None or t.is_public]
    pick = own or shared
    return pick[0] if pick else None


def course_scenario_ids(db: Session, course: Course) -> list[str]:
    ids: list[str] = []
    modules = db.query(CourseModule).filter(CourseModule.course_id == course.id).all()
    for m in modules:
        try:
            ref = json.loads(m.content_ref or "{}")
        except ValueError:
            ref = {}
        if isinstance(ref, dict) and ref.get("scenario_id"):
            ids.append(str(ref["scenario_id"]))
    if modules:
        for (sid,) in (db.query(ModuleContent.scenario_id)
                       .filter(ModuleContent.module_id.in_([m.id for m in modules]),
                               ModuleContent.scenario_id.is_not(None)).all()):
            ids.append(str(sid))
    return list(dict.fromkeys(ids))


def _course_booked_templates(db: Session, course: Course) -> list[Template]:
    rows = (db.query(Template).join(ScheduledEvent, ScheduledEvent.template_id == Template.id)
            .filter(ScheduledEvent.course_id == course.id, Template.deleted_at.is_(None),
                    ScheduledEvent.state.notin_([EventState.cancelled, EventState.rejected]))
            .order_by(Template.name, Template.id).all())
    return [t for t in dict.fromkeys(rows) if _in_item_scope(t, course)]


def _exercise_template_dependencies(db: Session, row: ExerciseTemplate) -> list[Dependency]:
    """The range template it names, the scenario it names, and the range template that
    scenario names (range templates first). A reference this site cannot read leaves a
    dependency with no title."""
    templates: dict[str, Dependency] = {}
    scenarios: dict[str, Dependency] = {}
    if row.range_template_id is not None:
        t = load(db, "range_template", row.range_template_id)
        ok = t is not None and _in_item_scope(t, row)
        templates[str(row.range_template_id)] = Dependency("range_template", str(row.range_template_id),
                                                           t.name if ok else None)
    if row.scenario_id is not None:
        sc = load(db, "scenario", row.scenario_id)
        if sc is None or not _in_item_scope(sc, row):
            scenarios[str(row.scenario_id)] = Dependency("scenario", str(row.scenario_id), None)
        else:
            scenarios[str(row.scenario_id)] = Dependency("scenario", str(sc.id), sc.name)
            for dep in dependencies(db, "scenario", sc):
                templates.setdefault(dep.item_id, dep)
    return [*templates.values(), *scenarios.values()]


def dependencies(db: Session, item_type: str, row) -> list[Dependency]:
    """What ``row`` needs on the new site, range templates first."""
    if item_type == "range_template":
        return []
    if item_type == "exercise_template":
        return _exercise_template_dependencies(db, row)
    if item_type == "scenario":
        tmpl = scenario_template(db, row)
        ref = str(_doc(row.yaml).get("range_template") or "").strip()
        if tmpl is not None:
            return [Dependency("range_template", str(tmpl.id), tmpl.name)]
        return [Dependency("range_template", ref, None)] if ref else []
    templates: dict[str, Dependency] = {}
    scenarios: dict[str, Dependency] = {}
    for t in _course_booked_templates(db, row):
        templates[str(t.id)] = Dependency("range_template", str(t.id), t.name)
    for sid in course_scenario_ids(db, row):
        sc = load(db, "scenario", sid)
        if sc is None or not _in_item_scope(sc, row):
            scenarios[sid] = Dependency("scenario", sid, None)
            continue
        scenarios[sid] = Dependency("scenario", sid, sc.name)
        for dep in dependencies(db, "scenario", sc):
            templates.setdefault(dep.item_id, dep)
    return [*templates.values(), *scenarios.values()]


# ── the three conditions ─────────────────────────────────────────────────────────
def _template_builds(entries: list) -> list[dict]:
    top = sec.latest(entries, "template_provenance")
    vms = (top.detail or {}).get("vms") if top is not None and isinstance(top.detail, dict) else None
    out: dict[tuple, dict] = {}
    for v in vms if isinstance(vms, list) else []:
        if isinstance(v, dict) and v.get("catalogue_id") and v.get("build_id"):
            key = (str(v["catalogue_id"]), str(v["build_id"]), str(v.get("checksum") or ""))
            out[key] = {"catalogue_id": key[0], "build_id": key[1], "checksum": key[2]}
    return [out[k] for k in sorted(out)]


def template_certification(db: Session, tmpl: Template, scope: Scope) -> tuple[Condition, list[dict]]:
    """(the condition, the golden-image builds the certified build ran)."""
    current = plan_hash(tmpl)
    q = db.query(RangeCertificationRecord).filter(RangeCertificationRecord.template_id == tmpl.id)
    records = (_tenant_filter(q, RangeCertificationRecord.tenant_id, scope)
               .order_by(RangeCertificationRecord.opened_at.desc(), RangeCertificationRecord.id)
               .limit(MAX_RECORDS).all())
    if not records:
        return Condition(CERTIFIED, False, ["No range built from it has reached Certified yet (no certification "
                                            "record)"]), []
    by_record: dict[uuid.UUID, list] = {r.id: [] for r in records}
    for e in db.query(RangeCertificationEntry).filter(RangeCertificationEntry.record_id.in_(list(by_record))).all():
        by_record[e.record_id].append(e)
    earlier_version = False
    newest: tuple[str, list[str]] | None = None
    for r in records:
        verdict, reasons = sec.verdict(by_record[r.id])
        if newest is None:
            newest = (verdict, reasons)
        if verdict != sec.CERTIFIED:
            continue
        if current and r.plan_hash == current:
            ev = Evidence("certification", str(r.id), f"{sec.VERDICT_LABELS[verdict]}: range {r.range_name}",
                          r.opened_at)
            return Condition(CERTIFIED, True, evidence=[ev]), _template_builds(by_record[r.id])
        earlier_version = True
    reasons: list[str] = []
    if earlier_version:
        reasons.append("Certified only from an earlier version; it has changed since, so a range built from it now "
                       "must reach Certified")
    if newest is not None and newest[0] != sec.CERTIFIED:
        why = f": {newest[1][0]}" if newest[1] else ""
        reasons.append(f"Its newest range is {sec.VERDICT_LABELS[newest[0]]}{why}")
    return Condition(CERTIFIED, False, reasons), []


def _closed_classes(db: Session, class_ids, scope: Scope) -> list[Evidence]:
    ids = list(dict.fromkeys(class_ids))
    if not ids:
        return []
    q = (db.query(Class, ClassCloseout.closed_at).join(ClassCloseout, ClassCloseout.class_id == Class.id)
         .filter(Class.id.in_(ids), Class.status == ClassStatus.closed.value))
    rows = _tenant_filter(q, Class.tenant_id, scope).order_by(ClassCloseout.closed_at.desc(), Class.id).all()
    if not rows:
        return []
    passed: dict[uuid.UUID, int] = {}
    for (cid,) in (db.query(ClassResult.class_id)
                   .filter(ClassResult.class_id.in_([c.id for c, _ in rows]),
                           ClassResult.outcome == ResultOutcome.passed.value).all()):
        passed[cid] = passed.get(cid, 0) + 1
    return [Evidence("class", str(c.id), f"Class {c.name} closed with {passed[c.id]} passing Student(s)", closed_at)
            for c, closed_at in rows if passed.get(c.id)]


def exercise_passed(ex: Exercise) -> bool:
    return (ex.state == ExerciseState.completed and (ex.max_score or 0) > 0
            and (ex.total_score or 0) * 100 >= ex.max_score * PASS_PERCENT)


def _exercises(db: Session, q, scope: Scope) -> list[Evidence]:
    rows = (_tenant_filter(q, Exercise.tenant_id, scope)
            .filter(Exercise.state == ExerciseState.completed, Exercise.deleted_at.is_(None))
            .order_by(Exercise.completed_at.desc(), Exercise.id).limit(200).all())
    return [Evidence("exercise", str(ex.id), f"Exercise {ex.name} completed with {ex.total_score}/{ex.max_score}",
                     ex.completed_at) for ex in rows if exercise_passed(ex)]


_NO_USE = ("No closed class with a passing Student and no completed exercise with a passing score has used it yet")


def template_use(db: Session, tmpl: Template, scope: Scope) -> Condition:
    class_ids = [cid for (cid,) in db.query(ClassRange.class_id).filter(ClassRange.template_id == tmpl.id).all()]
    class_ids += [cid for (cid,) in db.query(ClassSession.class_id)
                  .join(ScheduledEvent, ScheduledEvent.id == ClassSession.event_id)
                  .filter(ScheduledEvent.template_id == tmpl.id).all()]
    ev = _closed_classes(db, class_ids, scope)
    ev += _exercises(db, db.query(Exercise).join(Range, Range.id == Exercise.range_id)
                     .filter(Range.template_id == tmpl.id), scope)
    return Condition(USED, bool(ev), [] if ev else [_NO_USE], ev)


def scenario_use(db: Session, sc: Scenario, scope: Scope) -> Condition:
    class_ids = [cid for (cid,) in db.query(ClassSession.class_id)
                 .join(ScheduledEvent, ScheduledEvent.id == ClassSession.event_id)
                 .filter(ScheduledEvent.scenario_id == sc.id).all()]
    ev = _closed_classes(db, class_ids, scope)
    ev += _exercises(db, db.query(Exercise).filter(Exercise.scenario_id == sc.id), scope)
    return Condition(USED, bool(ev), [] if ev else [_NO_USE], ev)


NODE_KEYS = ("id", "type", "os", "name")


def node_types(templates: list[Template]) -> set[str]:
    """What a range template's nodes can be called: each node's id, type, os or name."""
    out: set[str] = set()
    for t in templates:
        nodes = _doc(t.yaml).get("nodes")
        for n in nodes if isinstance(nodes, list) else []:
            if isinstance(n, dict):
                out |= {str(n[k]) for k in NODE_KEYS if n.get(k)}
    return out


def sizing_problems(et: ExerciseTemplate, templates: list[Template]) -> list[str]:
    """Roles and scaling rules must name node types the range template builds (ADR 0017): a VM
    profile that points at nothing sizes nothing. Missing evidence never passes."""
    known = node_types(templates)
    wanted = [(f"Role {r.get('name')}", v.get("node_type")) for r in et.roles or [] for v in r.get("vm_profile") or []]
    wanted += [(f"Scaling rule {r.get('name')}", r.get("node_type")) for r in et.scaling_rules or []]
    names = ", ".join(t.name for t in templates)
    return [f"{who} needs node type '{nt}', which range template {names} does not define"
            for who, nt in wanted if nt not in known][:5]


def effective_range_template(db: Session, et: ExerciseTemplate) -> Template | None:
    """The range template an exercise from ``et`` runs on: the one it names, else the one its
    scenario names."""
    if et.range_template_id is not None:
        return load(db, "range_template", et.range_template_id)
    if et.scenario_id is not None:
        sc = load(db, "scenario", et.scenario_id)
        return scenario_template(db, sc) if sc is not None else None
    return None


def exercise_template_use(db: Session, et: ExerciseTemplate, scope: Scope) -> Condition:
    """Exercises *made from the template as it is now* that completed with a passing score: made
    from this revision, on a range built from the range template it runs on now. An edit (a new
    revision, another range template) needs a new exercise to prove it again."""
    want = effective_range_template(db, et)
    if want is None:
        return Condition(USED, False, ["It names no range template, so no exercise made from it ran on one"])
    ev = _exercises(db, db.query(Exercise).join(ExerciseTemplateUse, ExerciseTemplateUse.exercise_id == Exercise.id)
                    .join(Range, Range.id == Exercise.range_id)
                    .filter(ExerciseTemplateUse.template_id == et.id,
                            ExerciseTemplateUse.template_revision == et.revision, Range.template_id == want.id), scope)
    return Condition(USED, bool(ev), [] if ev else [
        "No exercise made from its current revision, on a range built from its range template, has completed with a "
        "passing score yet"], ev)


def course_use(db: Session, course: Course, scope: Scope) -> Condition:
    ev = _closed_classes(db, [cid for (cid,) in db.query(Class.id).filter(Class.course_id == course.id).all()], scope)
    return Condition(USED, bool(ev), [] if ev else ["No class of it has been closed with a passing Student yet"], ev)


def freshness(db: Session, item_type: str, item_id: str) -> Condition:
    row = (db.query(ContentReviewItem)
           .filter(ContentReviewItem.item_type == item_type, ContentReviewItem.item_id == str(item_id)).first())
    if row is None or row.missing:
        return Condition(FRESH, False, ["Not on the content clock yet (its hourly sync adds new content)"])
    if row.retired:
        return Condition(FRESH, False, ["Retired on the content clock"])
    if row.stale:
        texts = [str(r.get("text") or "") for r in (row.stale_reasons or []) if isinstance(r, dict)]
        texts = [t for t in texts if t][:3]
        return Condition(FRESH, False, [f"Stale: {t}" for t in texts] or ["Stale on the content clock"])
    label = (f"Reviewed {row.last_reviewed_at:%d %b %Y}, next due {row.due_at:%d %b %Y}" if row.last_reviewed_at
             else f"On the content clock, review due {row.due_at:%d %b %Y}")
    return Condition(FRESH, True, evidence=[Evidence("review", str(row.id), label, row.last_reviewed_at)])


# ── per item ────────────────────────────────────────────────────────────────────
def _redact(db: Session, st: Status, viewer: uuid.UUID) -> Status:
    """What ``viewer`` (a tenant) may read of ``st``: a dependency that is another tenant's private
    content is not named, in the dependency list or in any reason."""
    hidden: list[str] = []
    for d in st.dependencies:
        dep = load(db, d.item_type, d.item_id) if d.title is not None else None
        if dep is not None and not visible(dep, viewer):
            hidden.append(d.title)
            d.title = f"a {LABELS[d.item_type]} that is not shared with you"
    if hidden:
        # Longest first (a name inside another hidden name), whatever the case, and only the whole
        # name (not a word that merely contains it).
        names = sorted({n for n in hidden if n}, key=len, reverse=True)
        pattern = re.compile("|".join(rf"(?<!\w){re.escape(n)}(?!\w)" for n in names), re.IGNORECASE)
        for c in st.conditions:
            c.reasons = [pattern.sub("a private item", r) for r in c.reasons]
    return st


def status(db: Session, item_type: str, row, scope: Scope, viewer: uuid.UUID | None = None) -> Status:
    """``viewer``: the reading tenant (None: the platform administrator, who reads every name)."""
    st = _status(db, item_type, row, scope)
    return st if viewer is None else _redact(db, st, viewer)


def _status(db: Session, item_type: str, row, scope: Scope) -> Status:
    deps = dependencies(db, item_type, row)
    builds: list[dict] = []
    if item_type == "range_template":
        cert, builds = template_certification(db, row, scope)
        use = template_use(db, row, scope)
    else:
        templates = [load(db, "range_template", d.item_id) for d in deps
                     if d.item_type == "range_template" and d.title is not None]
        templates = [t for t in templates if t is not None]
        broken = [d for d in deps if d.title is None]
        if broken:
            # Missing evidence never passes: a dependency this site cannot read is not certified.
            cert = Condition(CERTIFIED, False, [
                f"It needs {LABELS[d.item_type]} {d.item_id}, which is missing here, deleted, or not shared with "
                f"its tenant" for d in broken[:5]])
        elif item_type in ("scenario", "exercise_template") and not templates:
            cert = Condition(CERTIFIED, False, ["It names no range template this site has, so no range was built "
                                                "from it"])
        elif not templates:
            cert = Condition(CERTIFIED, True, ["It uses no range: there is nothing to certify"], not_applicable=True)
        else:
            cert = Condition(CERTIFIED, True)
            for t in templates:
                c, b = template_certification(db, t, scope)
                builds += b
                cert.evidence += c.evidence
                if not c.met:
                    cert.met = False
                    cert.reasons += [f"Range template {t.name}: {r}" for r in c.reasons]
        use = {"scenario": scenario_use, "exercise_template": exercise_template_use,
               "course": course_use}[item_type](db, row, scope)
        if item_type == "exercise_template" and templates:
            problems = sizing_problems(row, templates)
            if problems:  # a role or rule that names a machine the range template does not build
                cert.met = False
                cert.reasons += problems
    fresh = freshness(db, item_type, str(row.id))
    unique = {(b["catalogue_id"], b["build_id"], b["checksum"]): b for b in builds}
    return Status(item_type=item_type, item_id=str(row.id), title=row.name, conditions=[cert, use, fresh],
                  dependencies=deps, golden_builds=[unique[k] for k in sorted(unique)])


def closure(db: Session, roots: list[tuple[str, object]]) -> tuple[list[tuple[str, object]], list[Dependency]]:
    """Every item ``roots`` need, the roots included, range templates first, then scenarios,
    then courses; and the dependencies that could not be loaded (missing, deleted, or outside
    their owner's scope)."""
    found: dict[tuple[str, str], object] = {}
    broken: list[Dependency] = []
    queue = list(roots)
    while queue:
        item_type, row = queue.pop()
        key = (item_type, str(row.id))
        if key in found:
            continue
        found[key] = row
        for d in dependencies(db, item_type, row):
            dep = load(db, d.item_type, d.item_id) if d.title is not None else None
            if dep is None:
                broken.append(d)
                continue
            queue.append((d.item_type, dep))
    order = {"range_template": 0, "scenario": 1, "course": 2, "exercise_template": 3}
    items = sorted(found.items(), key=lambda kv: (order[kv[0][0]], kv[1].name or "", kv[0][1]))
    return [(k[0], row) for k, row in items], broken


def scope_for(db: Session, user) -> Scope:
    """Whose evidence counts for ``user``: every tenant's for the platform administrator,
    else their own tenant's."""
    from ..rbac import is_platform_admin
    from ..tenancy import tenant_uuid

    return None if is_platform_admin(user, db) else {tenant_uuid(user)}


def any_visible(db: Session, user, item_type: str, item_id) -> object | None:
    from ..tenancy import tenant_uuid

    row = load(db, item_type, item_id)
    return row if row is not None and visible(row, tenant_uuid(user)) else None


__all__ = ["CERTIFIED", "FRESH", "PORTABLE", "USED", "Condition", "Dependency", "Evidence", "Status", "closure",
           "dependencies", "load", "scope_for", "status", "visible"]
