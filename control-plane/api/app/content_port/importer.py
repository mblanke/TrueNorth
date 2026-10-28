"""Preview and apply a verified content-port bundle on the new site.

**Where it lands.** In the importing platform administrator's chosen tenant
(``target_tenant_id``, default their own), or as shared content (``shared``) the existing
way: range templates and scenarios in that tenant and public, courses with no tenant.

**Each item's action** (``plan``), matched by the item's origin id in that tenant
(``content_port_items``), else by name:

=============  =================================================================
``create``     nothing here yet
``update``     imported before from an older export, unchanged here since
``unchanged``  this exact content was imported before and nobody changed it here
``conflict``   needs explicit confirmation (``overwrite``) to proceed:
               ``local_changed``: changed here since it was imported;
               ``local_name``: an item of the same name exists here that no
               import made; ``older``: the bundle is older than the version
               imported before (a downgrade)
``refused``    its content is refused here as the API refuses it (a template
               that pins networks or carries credentials, a scenario whose
               objectives cannot be judged)
``skipped``    (apply only) a conflict not confirmed, or a dependency that was
               not imported
=============  =================================================================

**A bundle is imported once.** Its id is recorded; the same bundle again is refused
(replay). An older bundle of the same item is a ``conflict`` (downgrade), never silent.

**Not proven here.** Nothing of the origin's proof makes an item proven on this site:
``proven.py`` reads only local evidence, so a range built from it must reach Certified here,
be used in a class or exercise here, and be on this site's content clock. The origin's proof
summary is kept for display. Golden-image builds the bundle needs and this site lacks are
listed: port those templates first.
"""

from __future__ import annotations

import difflib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import yaml
from sqlalchemy.orm import Session

from .. import engine_bridge, safe_yaml
from ..golden_image_builds.models import GoldenImageBuild
from ..models import (
    ContentKind,
    Course,
    CourseModule,
    GoldenImage,
    Lesson,
    ModuleContent,
    ModuleContentType,
    PerformanceObjective,
    Qualification,
    Quiz,
    QuizAttempt,
    QuizQuestion,
    QuizQuestionType,
    Scenario,
    Template,
)
from ..range_credentials import accounts
from ..range_topology import network_placement_keys
from . import bundle as bnd
from . import proven
from .models import ContentPortImport, ContentPortItem
from .schemas import BundleIn, BundleItem, CourseContent, PortMissingBuildOut, PortPreviewItemOut, ScenarioContent

MAX_DIFF_LINES = 300
ORDER = {"range_template": 0, "scenario": 1, "course": 2}


@dataclass
class PlanItem:
    item: BundleItem
    action: str
    conflict: str | None = None
    local: object | None = None
    link: ContentPortItem | None = None
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    diff: str = ""

    def out(self) -> PortPreviewItemOut:
        return PortPreviewItemOut(key=self.item.key, item_type=self.item.item_type, name=self.item.content["name"],
                              action=self.action, conflict=self.conflict,
                              local_id=str(self.local.id) if self.local is not None else None,
                              reasons=self.reasons, notes=self.notes, diff=self.diff,
                              proven_at_origin=self.item.proof.proven)


@dataclass
class Plan:
    bundle: BundleIn
    key_id: str
    signed_sha256: str
    tenant_id: uuid.UUID
    shared: bool
    items: list[PlanItem]
    missing_builds: list[PortMissingBuildOut]
    already_imported_at: datetime | None

    @property
    def can_import(self) -> bool:
        return self.already_imported_at is None and any(p.action in ("create", "update", "conflict")
                                                        for p in self.items)


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def local_tenant(item_type: str, tenant_id: uuid.UUID, shared: bool) -> uuid.UUID | None:
    return None if item_type == "course" and shared else tenant_id


# ── reading the local side ────────────────────────────────────────────────────────
def _link(db: Session, item: BundleItem, tenant: uuid.UUID | None) -> ContentPortItem | None:
    q = db.query(ContentPortItem).filter(ContentPortItem.item_type == item.item_type,
                                         ContentPortItem.origin_id == str(item.origin_id))
    q = q.filter(ContentPortItem.tenant_id.is_(None)) if tenant is None else q.filter(
        ContentPortItem.tenant_id == tenant)
    return q.order_by(ContentPortItem.updated_at.desc()).first()


def _same_name(db: Session, item_type: str, name: str, tenant: uuid.UUID | None):
    model = {"course": Course, "range_template": Template, "scenario": Scenario}[item_type]
    q = db.query(model).filter(model.name == name)
    q = q.filter(model.tenant_id.is_(None)) if tenant is None else q.filter(model.tenant_id == tenant)
    if hasattr(model, "deleted_at"):
        q = q.filter(model.deleted_at.is_(None))
    return q.order_by(model.id).first()


def _projection(item_type: str, content: dict) -> list[str]:
    """What the preview diff compares: the YAML itself, or a course's readable outline."""
    if item_type != "course":
        head = [f"name: {content['name']}", f"version: {content['version']}"]
        return head + ["---"] + (content.get("yaml") or "").splitlines()
    lines = [f"name: {content['name']}", f"version: {content['version']}", f"difficulty: {content['difficulty']}",
             f"duration_hours: {content['duration_hours']}", f"qualification: {content.get('qsp_code') or '-'}",
             "description:", *[f"  {x}" for x in (content.get("description") or "").splitlines()]]
    for m in content.get("modules") or []:
        po = m.get("po") or {}
        lines.append(f"module {m['ordinal']}: {m['title']} ({m['content_type']}, {m['duration_minutes']} min, "
                     f"pass {m['pass_threshold']}%{', PO ' + po['po_code'] if po else ''})")
        for c in m.get("contents") or []:
            what = [c["content_kind"]]
            if c.get("lesson"):
                what.append(f"lesson '{c['lesson']['title']}' ({len(c['lesson']['body_markdown'])} chars)")
            if c.get("quiz"):
                what.append(f"quiz '{c['quiz']['title']}' ({len(c['quiz']['questions'])} questions)")
            if c.get("scenario"):
                what.append("scenario")
            lines.append(f"  {c['ordinal']}: " + ", ".join(what))
    return lines


def _diff(item_type: str, local: dict | None, incoming: dict) -> str:
    new = _projection(item_type, incoming)
    old = _projection(item_type, local) if local is not None else []
    lines = list(difflib.unified_diff(old, new, "here", "bundle", lineterm="", n=2))
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + [f"... {len(lines) - MAX_DIFF_LINES} more lines"]
    return "\n".join(lines)


def _refusals(item: BundleItem) -> list[str]:
    """Why this site would refuse the item's content, as the API refuses it."""
    if item.item_type == "course":
        return []
    text = item.content["yaml"]
    try:
        doc = safe_yaml.load(text)
    except Exception as exc:  # noqa: BLE001 - aliases, oversize or not YAML
        return [f"its YAML is refused: {exc}"]
    if not isinstance(doc, dict):
        return ["its YAML is not a mapping"]
    if item.item_type == "range_template":
        out = []
        if "credentials" in doc:
            out.append("a template may not set credentials")
        if keys := network_placement_keys(doc):
            out.append(f"a template may not set port_group ({', '.join(keys)})")
        return out
    try:
        errors = engine_bridge.objective_errors(doc)
    except Exception as exc:  # noqa: BLE001 - the engine is missing: refuse rather than guess
        return [f"its objectives could not be checked here ({exc})"]
    return [f"objective {e.get('path')}: {e.get('message')}" for e in errors[:5]]


def _notes(db: Session, item: BundleItem, tenant: uuid.UUID | None) -> list[str]:
    notes = []
    if item.removed_passwords:
        notes.append(f"{item.removed_passwords} scenario account password(s) were removed for the port; set them "
                     f"in the template here before a range is built from it")
    if item.item_type == "course":
        c = CourseContent.model_validate(item.content)
        if c.qsp_code and _qualification(db, c.qsp_code, tenant) is None:
            notes.append(f"Its qualification {c.qsp_code} is not on this site; the course is imported without it")
        missing = sorted({m.po.po_code for m in c.modules if m.po is not None
                          and _po(db, m.po.qsp_code, m.po.po_code, tenant) is None})
        if missing:
            notes.append(f"Performance objective(s) {', '.join(missing[:10])} are not on this site; those modules are "
                         f"imported without them")
        notes.append("A new course lands unpublished; publish it through a course release when it is proven here")
    notes.append("Not proven here until a range built from it reaches Certified on this site, it is used in a class or "
                 "exercise here, and the content clock has reviewed it")
    return notes


def missing_builds(db: Session, bundle: BundleIn) -> list[PortMissingBuildOut]:
    needed: dict[tuple, list[str]] = {}
    for i in bundle.items:
        for b in i.golden_builds:
            needed.setdefault((b.catalogue_id, b.build_id, b.checksum), []).append(i.key)
    out = []
    for (cat, build, checksum), keys in sorted(needed.items()):
        rows = (db.query(GoldenImageBuild.checksum).join(GoldenImage, GoldenImage.id == GoldenImageBuild.golden_image_id)
                .filter(GoldenImage.catalogue_id == cat, GoldenImageBuild.build_id == build,
                        GoldenImage.deleted_at.is_(None)).all())
        if not any(not checksum or r.checksum == checksum for r in rows):
            out.append(PortMissingBuildOut(catalogue_id=cat, build_id=build, checksum=checksum, needed_by=sorted(keys)))
    return out


def _visibility_differs(row, item_type: str, shared: bool) -> bool:
    """A template or scenario here is public or not; the import asks for ``shared``. (A
    course's visibility is its tenant, which the match already includes.)"""
    return item_type != "course" and bool(row.is_public) != shared


def plan(db: Session, doc: dict, bundle: BundleIn, kid: str, *, tenant_id: uuid.UUID, shared: bool) -> Plan:
    sha = bnd.signing.signed_sha256(doc)
    seen = db.query(ContentPortImport).filter(ContentPortImport.bundle_id == bundle.bundle_id).first()
    exported = _utc(bundle.exported_at)
    items: list[PlanItem] = []
    for item in sorted(bundle.items, key=lambda i: (ORDER[i.item_type], i.key)):
        tenant = local_tenant(item.item_type, tenant_id, shared)
        p = PlanItem(item=item, action="create")
        p.link = _link(db, item, tenant)
        linked = proven.load(db, item.item_type, p.link.local_id) if p.link is not None else None
        if linked is not None:
            p.local = linked
            here = bnd.fingerprint(db, item.item_type, linked)
            changed_here = here != p.link.local_fingerprint
            if p.link.origin_content_sha256 == item.content_sha256 and not changed_here:
                p.action = "unchanged"
            elif _utc(p.link.exported_at) > exported:
                p.action, p.conflict = "conflict", "older"
                p.reasons.append(f"This bundle (made {exported:%d %b %Y %H:%M}) is older than the version imported "
                                 f"here before (made {_utc(p.link.exported_at):%d %b %Y %H:%M})")
            elif changed_here:
                p.action, p.conflict = "conflict", "local_changed"
                p.reasons.append("It was changed here since it was imported; importing replaces those changes")
            else:
                p.action = "update"
            if p.action in ("unchanged", "update") and _visibility_differs(linked, item.item_type, shared):
                p.action, p.conflict = "conflict", "visibility"
                p.reasons.append(f"It is {'private to its tenant' if shared else 'shared'} here; importing makes it "
                                 f"{'shared' if shared else 'private to its tenant'}")
        else:
            p.link = None
            same = _same_name(db, item.item_type, item.content["name"], tenant)
            if same is not None:
                p.local = same
                p.action, p.conflict = "conflict", "local_name"
                p.reasons.append(f"A {proven.LABELS[item.item_type]} of this name exists here that no import made; "
                                 f"importing replaces its content")
        if refused := _refusals(item):
            p.action, p.conflict = "refused", None
            p.reasons = refused
        if p.local is not None:
            p.diff = _diff(item.item_type, bnd.content_of(db, item.item_type, p.local, bnd.local_ref), item.content)
        else:
            p.diff = _diff(item.item_type, None, item.content)
        if p.action != "unchanged":
            p.notes = _notes(db, item, tenant)
            if isinstance(p.local, Course) and p.local.is_published:
                p.notes.append("It is published here: the update changes what its Students see")
        items.append(p)
    return Plan(bundle=bundle, key_id=kid, signed_sha256=sha, tenant_id=tenant_id, shared=shared, items=items,
                missing_builds=missing_builds(db, bundle),
                already_imported_at=_utc(seen.imported_at) if seen is not None else None)


# ── applying ──────────────────────────────────────────────────────────────────────
def _qualification(db: Session, code: str, tenant: uuid.UUID | None) -> Qualification | None:
    q = db.query(Qualification).filter(Qualification.qsp_code == code)
    rows = q.all()
    return next((r for r in rows if r.tenant_id is None or r.tenant_id == tenant), None)


def _po(db: Session, qsp_code: str, po_code: str, tenant: uuid.UUID | None) -> PerformanceObjective | None:
    qual = _qualification(db, qsp_code, tenant)
    if qual is None:
        return None
    return (db.query(PerformanceObjective)
            .filter(PerformanceObjective.qualification_id == qual.id, PerformanceObjective.po_code == po_code).first())


_UUID_LINE = re.compile(r"^(range_template:[ \t]*['\"]?)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
                        r"(['\"]?[ \t]*(#.*)?)$", re.M)


def _relink_scenario_yaml(text: str, origin_template: str | None, local_template: str | None) -> str:
    """A scenario that names its template by the origin's id names the local copy's id."""
    if not origin_template or not local_template or origin_template == local_template:
        return text
    return _UUID_LINE.sub(lambda m: m.group(1) + local_template + m.group(3) if m.group(2) == origin_template
                          else m.group(0), text)


def _write_template(db: Session, p: PlanItem, tenant: uuid.UUID | None, shared: bool) -> Template:
    c = p.item.content
    row = p.local if isinstance(p.local, Template) else None
    yaml_text = accounts.seal_yaml(c["yaml"], tenant_id=tenant) or c["yaml"]
    if row is None:
        row = Template(id=uuid.uuid4(), name=c["name"], version=c["version"] or "0.1.0", yaml=yaml_text,
                       tenant_id=tenant, is_public=shared)
        db.add(row)
    else:
        row.name, row.version, row.yaml = c["name"], c["version"] or row.version, yaml_text
        row.is_public = shared  # a visibility change is a confirmed conflict (plan)
    return row


def _write_scenario(db: Session, p: PlanItem, tenant: uuid.UUID | None, shared: bool, keymap: dict
                    ) -> tuple[Scenario, list[str]]:
    c = ScenarioContent.model_validate(p.item.content)
    origin_t = c.range_template.split(":", 1)[1] if c.range_template else None
    local_t = keymap.get(c.range_template) if c.range_template else None
    yaml_text = _relink_scenario_yaml(c.yaml, origin_t, local_t)
    row = p.local if isinstance(p.local, Scenario) else None
    if row is None:
        row = Scenario(id=uuid.uuid4(), name=c.name, version=c.version or "0.1.0", yaml=yaml_text, tenant_id=tenant,
                       is_public=shared)
        db.add(row)
    else:
        row.name, row.version, row.yaml = c.name, c.version or row.version, yaml_text
        row.is_public = shared  # a visibility change is a confirmed conflict (plan)
    db.flush()
    notes: list[str] = []
    if local_t is not None:
        found = proven.scenario_template(db, row)
        if found is None or str(found.id) != local_t:
            # Named in a spelling the line rewrite does not cover: set it on the document.
            doc = proven._doc(row.yaml)
            if doc:
                doc["range_template"] = local_t
                row.yaml = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
                db.flush()
            found = proven.scenario_template(db, row)
            if found is None or str(found.id) != local_t:
                notes.append("Its range_template could not be pointed at the imported template; set it here")
    return row, notes


def _exclusive(db: Session, column, row_id, mc: ModuleContent) -> bool:
    """No other module content row points at ``row_id`` (a lesson or quiz library row may be
    shared between courses; an import never rewrites one another course uses)."""
    return db.query(ModuleContent.id).filter(column == row_id, ModuleContent.id != mc.id).first() is None


def _own_lesson(db: Session, mc: ModuleContent, tenant) -> Lesson | None:
    lesson = db.get(Lesson, mc.lesson_id) if mc.lesson_id else None  # this module's own row's lesson
    if lesson is None or lesson.deleted_at is not None or lesson.tenant_id != tenant:
        return None
    return lesson if _exclusive(db, ModuleContent.lesson_id, lesson.id, mc) else None


def _own_quiz(db: Session, mc: ModuleContent, module: CourseModule, tenant) -> Quiz | None:
    quiz = db.get(Quiz, mc.quiz_id) if mc.quiz_id else None  # this module's own row's quiz
    if (quiz is None or quiz.deleted_at is not None or quiz.tenant_id != tenant
            or quiz.module_id not in (None, module.id)):
        return None
    return quiz if _exclusive(db, ModuleContent.quiz_id, quiz.id, mc) else None


def _write_quiz(db: Session, module: CourseModule, current: Quiz | None, q, tenant) -> tuple[Quiz, str | None]:
    note = None
    if current is not None and db.query(QuizAttempt.id).filter(QuizAttempt.quiz_id == current.id).first():
        note = f"Quiz '{current.title}' has Student attempts; it is kept and a new quiz replaces it in the module"
        current = None
    if current is None:
        current = Quiz(id=uuid.uuid4(), title=q.title, module_id=module.id, tenant_id=tenant, is_published=False)
        db.add(current)
    else:
        db.query(QuizQuestion).filter(QuizQuestion.quiz_id == current.id).delete(synchronize_session=False)
    current.title, current.description, current.pass_pct = q.title, q.description, q.pass_pct
    current.time_limit_minutes, current.shuffle_questions = q.time_limit_minutes, q.shuffle_questions
    current.max_attempts, current.module_id = q.max_attempts, module.id
    db.flush()
    for qq in q.questions:
        db.add(QuizQuestion(id=uuid.uuid4(), quiz_id=current.id, ordinal=qq.ordinal,
                            question_type=QuizQuestionType(qq.question_type), stem=qq.stem, options=qq.options,
                            correct=qq.correct, explanation=qq.explanation, competency_code=qq.competency_code,
                            difficulty=qq.difficulty, points=qq.points))
    return current, note


def _module_ref(carried: str, scenario_id: str | None) -> str:
    """The module's ``content_ref``: the authored content carried, with the local scenario id."""
    try:
        doc = json.loads(carried) if carried else {}
    except ValueError:
        return carried  # carried as it was (not a JSON object at the origin either)
    if not isinstance(doc, dict):
        return carried
    if scenario_id:
        doc["scenario_id"] = scenario_id
    return json.dumps(doc) if doc else ""


def _write_course(db: Session, p: PlanItem, tenant: uuid.UUID | None, keymap: dict) -> tuple[Course, list[str]]:
    c = CourseContent.model_validate(p.item.content)
    notes: list[str] = []
    row = p.local if isinstance(p.local, Course) else None
    if row is None:
        row = Course(id=uuid.uuid4(), name=c.name, tenant_id=tenant, is_published=False)
        db.add(row)
    qual = _qualification(db, c.qsp_code, tenant) if c.qsp_code else None
    row.name, row.description, row.version, row.difficulty = c.name, c.description, c.version or "1.0", c.difficulty
    row.duration_hours, row.tags, row.nice_work_roles = c.duration_hours, c.tags, c.nice_work_roles
    row.course_meta, row.qualification_id = c.course_meta, qual.id if qual else None
    db.flush()
    existing = {m.ordinal: m for m in db.query(CourseModule).filter(CourseModule.course_id == row.id)
                .order_by(CourseModule.id).all()}
    for bm in c.modules:
        m = existing.pop(bm.ordinal, None)
        if m is None:
            m = CourseModule(id=uuid.uuid4(), course_id=row.id, ordinal=bm.ordinal, title=bm.title,
                             content_type=ModuleContentType(bm.content_type))
            db.add(m)
        po = _po(db, bm.po.qsp_code, bm.po.po_code, tenant) if bm.po else None
        m.title, m.description, m.content_type = bm.title, bm.description, ModuleContentType(bm.content_type)
        m.duration_minutes, m.is_required, m.pass_threshold = bm.duration_minutes, bm.is_required, bm.pass_threshold
        m.po_id = po.id if po else None
        m.content_ref = _module_ref(bm.content_ref, keymap.get(bm.scenario) if bm.scenario else None)
        db.flush()
        rows = {}
        for mc in (db.query(ModuleContent).filter(ModuleContent.module_id == m.id)
                   .order_by(ModuleContent.ordinal, ModuleContent.id).all()):
            if mc.ordinal in rows:
                db.delete(mc)
            else:
                rows[mc.ordinal] = mc
        for bc in bm.contents:
            mc = rows.pop(bc.ordinal, None)
            if mc is None:
                mc = ModuleContent(id=uuid.uuid4(), module_id=m.id, ordinal=bc.ordinal)
                db.add(mc)
            mc.content_kind, mc.external_ref = ContentKind(bc.content_kind), bc.external_ref
            mc.scenario_id = uuid.UUID(keymap[bc.scenario]) if bc.scenario and keymap.get(bc.scenario) else None
            if bc.lesson is not None:
                lesson = _own_lesson(db, mc, tenant)
                if lesson is None:
                    lesson = Lesson(id=uuid.uuid4(), title=bc.lesson.title, tenant_id=tenant, is_published=False)
                    db.add(lesson)
                lesson.title, lesson.body_markdown = bc.lesson.title, bc.lesson.body_markdown
                lesson.duration_minutes, lesson.competency_codes = bc.lesson.duration_minutes, bc.lesson.competency_codes
                db.flush()
                mc.lesson_id = lesson.id
            else:
                mc.lesson_id = None
            if bc.quiz is not None:
                quiz, note = _write_quiz(db, m, _own_quiz(db, mc, m, tenant), bc.quiz, tenant)
                mc.quiz_id = quiz.id
                if note:
                    notes.append(note)
            else:
                mc.quiz_id = None
        for mc in rows.values():
            db.delete(mc)
        db.flush()
    if existing:
        notes.append(f"Module(s) {', '.join(str(o) for o in sorted(existing))} exist here but not in the bundle; "
                     f"they are kept")
    return row, notes


def apply(db: Session, user_id: uuid.UUID | None, pl: Plan, overwrite: set[str], *, now: datetime
          ) -> tuple[ContentPortImport, list[PlanItem]]:
    """Write the plan. Conflicts proceed only when their key is in ``overwrite``. Does not
    commit. The caller refuses a replayed bundle before calling."""
    imp = ContentPortImport(id=uuid.uuid4(), bundle_id=pl.bundle.bundle_id, key_id=pl.key_id,
                            signed_sha256=pl.signed_sha256, exported_at=_utc(pl.bundle.exported_at), imported_at=now,
                            imported_by=user_id, tenant_id=pl.tenant_id, shared=pl.shared, summary={})
    db.add(imp)
    db.flush()
    keymap: dict[str, str] = {}
    done: list[PlanItem] = []
    for p in pl.items:
        tenant = local_tenant(p.item.item_type, pl.tenant_id, pl.shared)
        if p.action == "unchanged":
            keymap[p.item.key] = str(p.local.id)
            done.append(p)
            continue
        if p.action == "conflict" and p.item.key not in overwrite:
            if p.link is not None and p.local is not None and p.conflict != "visibility":
                keymap[p.item.key] = str(p.local.id)  # what is here stays what dependents use
            p.action = "skipped"
            p.reasons = [*p.reasons, "Not confirmed: left as it is here"]
            done.append(p)
            continue
        if p.action == "refused":
            done.append(p)
            continue
        if missing := [d for d in p.item.depends_on if d not in keymap]:
            p.action, p.conflict = "skipped", None
            p.reasons = [f"It needs {', '.join(missing[:5])}, which was not imported"]
            done.append(p)
            continue
        verb = "update" if p.local is not None else "create"
        if p.item.item_type == "range_template":
            row = _write_template(db, p, tenant, pl.shared)
        elif p.item.item_type == "scenario":
            row, more = _write_scenario(db, p, tenant, pl.shared, keymap)
            p.notes = [*p.notes, *more]
        else:
            row, more = _write_course(db, p, tenant, keymap)
            p.notes = [*p.notes, *more]
        db.flush()
        keymap[p.item.key] = str(row.id)
        link = p.link or db.query(ContentPortItem).filter(ContentPortItem.item_type == p.item.item_type,
                                                          ContentPortItem.local_id == str(row.id)).first()
        if link is None:
            link = ContentPortItem(id=uuid.uuid4(), item_type=p.item.item_type, local_id=str(row.id),
                                   local_fingerprint="")
            db.add(link)
        link.tenant_id, link.origin_id, link.origin_key_id = tenant, str(p.item.origin_id), pl.key_id
        link.origin_content_sha256, link.exported_at, link.import_id = (
            p.item.content_sha256, _utc(pl.bundle.exported_at), imp.id)
        link.proof = p.item.proof.model_dump(mode="json")
        link.golden_builds = [b.model_dump(mode="json") for b in p.item.golden_builds]
        db.flush()
        link.local_fingerprint = bnd.fingerprint(db, p.item.item_type, row)
        p.action, p.local, p.link = verb, row, link
        done.append(p)
    counts: dict[str, int] = {}
    for p in done:
        counts[p.action] = counts.get(p.action, 0) + 1
    imp.summary = counts
    db.flush()
    return imp, done
