"""The content-port bundle: what an item's content is, and reading a bundle safely.

One JSON document, signed (``signing.py``)::

    {"format": "truenorth.content-port.v1", "bundle_id": "<uuid>", "exported_at": "<iso>",
     "origin": {"key_id": "<16 hex>"},
     "items": [{"key": "<item_type>:<origin uuid>", "item_type": ..., "origin_id": ...,
                "content_sha256": "<sha256 of canonical(content)>", "content": {...},
                "depends_on": ["<key>", ...], "proof": {...}, "golden_builds": [...],
                "removed_passwords": 0}],
     "golden_builds": [{"catalogue_id", "build_id", "checksum"}],
     "signature": {"alg": "Ed25519", "key_id", "public_key", "signed_sha256", "value"}}

**What an item's content is** (``content``):

* a range template: name, version and YAML. Every account password is removed
  (``range_credentials.accounts.redact_yaml``: typed-in scenario passwords become the
  redacted placeholder, wherever they appear), so a ported template must have its scenario
  passwords set on the new site before a range is built from it; ``removed_passwords``
  counts them;
* a scenario: name, version, YAML, and the key of the range template it names;
* a course: its fields (never ``is_published``), its qualification by QSP code, and each
  module with its performance objective by code, its scenario, and its content rows: the
  lesson and quiz bodies (the questions and their keys are course content), external refs.

Never in a bundle: Students, enrolments, progress, attempts, results, classes, ranges,
credentials, tenant ids, owners or reviewers, media files, or the golden images themselves
(only the builds a certified range ran, by catalogue id, build id and checksum).

**Reading** (``parse``): at most ``MAX_BUNDLE_BYTES``; strict JSON (no duplicate keys, no
NaN or Infinity, no floats: canonical JSON of a float is not portable); the signature is
checked against the trusted keys before anything else is read; then the document must be
exactly a bundle (``schemas.BundleIn``), each item's key must be its type and origin id,
its content must be exactly its type's content and hash to ``content_sha256``, every
reference must be an item of the bundle and listed in ``depends_on``, and every dependency
must be in the bundle.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..models import (
    Course,
    CourseModule,
    Lesson,
    ModuleContent,
    PerformanceObjective,
    Qualification,
    Quiz,
    QuizQuestion,
    Scenario,
    Template,
)
from ..range_certification.signing import canonical
from ..range_credentials import accounts
from . import proven, signing
from .schemas import CONTENT_MODELS, FORMAT, BundleIn

MAX_BUNDLE_BYTES = 50 * 1024 * 1024  # the edge's upload limit for this route (nginx 50M)
MAX_FUTURE = timedelta(hours=24)  # clock skew allowed between the two sites
Ref = Callable[[str, str], str | None]  # (item_type, local id) -> key, or None when it is not carried


class BundleRejectedError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def key(item_type: str, item_id) -> str:
    return f"{item_type}:{item_id}"


def sha256(content: dict) -> str:
    return hashlib.sha256(canonical(content)).hexdigest()


# ── what an item's content is ─────────────────────────────────────────────────────
def removed_passwords(text: str | None) -> int:
    doc = proven._doc(text)
    items = doc.get("accounts") if isinstance(doc.get("accounts"), list) else []
    return sum(1 for a in items if isinstance(a, dict) and a.get("password") is not None)


def template_content(t: Template) -> dict:
    yaml_text = accounts.redact_yaml(t.yaml, tenant_id=t.tenant_id) if t.yaml else ""
    return {"name": t.name, "version": t.version or "", "yaml": yaml_text}


def scenario_content(db: Session, sc: Scenario, ref: Ref) -> dict:
    tmpl = proven.scenario_template(db, sc)
    return {"name": sc.name, "version": sc.version or "", "yaml": sc.yaml or "",
            "range_template": ref("range_template", str(tmpl.id)) if tmpl is not None else None}


def _scenario_ref(db: Session, owner, sid, ref: Ref) -> str | None:
    if not sid:
        return None
    sc = proven.load(db, "scenario", sid)
    return ref("scenario", str(sc.id)) if sc is not None and proven._in_item_scope(sc, owner) else None


def _lesson(db: Session, owner, lesson_id) -> dict | None:
    row = db.get(Lesson, lesson_id) if lesson_id else None  # tenant-checked below
    if row is None or row.deleted_at is not None or not proven._in_item_scope(row, owner):
        return None
    return {"title": row.title, "body_markdown": row.body_markdown or "", "duration_minutes": row.duration_minutes or 0,
            "competency_codes": row.competency_codes or "[]"}


def _quiz(db: Session, owner, quiz_id) -> dict | None:
    row = db.get(Quiz, quiz_id) if quiz_id else None  # tenant-checked below
    if row is None or row.deleted_at is not None or not proven._in_item_scope(row, owner):
        return None
    questions = (db.query(QuizQuestion).filter(QuizQuestion.quiz_id == row.id)
                 .order_by(QuizQuestion.ordinal, QuizQuestion.id).all())
    return {"title": row.title, "description": row.description or "", "pass_pct": row.pass_pct,
            "time_limit_minutes": row.time_limit_minutes or 0, "shuffle_questions": bool(row.shuffle_questions),
            "max_attempts": row.max_attempts or 0,
            "questions": [{"ordinal": q.ordinal or 0, "question_type": getattr(q.question_type, "value",
                                                                                 q.question_type),
                           "stem": q.stem, "options": q.options or "[]", "correct": q.correct or "[]",
                           "explanation": q.explanation or "", "competency_code": q.competency_code or "",
                           "difficulty": q.difficulty or "intermediate", "points": q.points or 0}
                          for q in questions]}


ORIGIN_ONLY_META = ("superseded_by",)  # course_meta keys naming the origin site's own records


def _json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def module_ref(raw: str | None) -> tuple[str, str | None]:
    """(a module's ``content_ref`` without its scenario id, that scenario id). The authored
    content it holds (objectives, topics, lab, the ``retired`` flag ...) is carried; the
    scenario travels as a reference of its own. Text that is not a JSON object is carried as
    it is."""
    try:
        doc = json.loads(raw or "{}")
    except ValueError:
        return raw or "", None
    if not isinstance(doc, dict):
        return raw or "", None
    sid = doc.pop("scenario_id", None)
    return (_json_text(doc) if doc else ""), (str(sid) if sid else None)


def course_meta(raw: str | None) -> str:
    try:
        doc = json.loads(raw or "{}")
    except ValueError:
        return "{}"
    if not isinstance(doc, dict):
        return "{}"
    return _json_text({k: v for k, v in doc.items() if k not in ORIGIN_ONLY_META})


def course_content(db: Session, c: Course, ref: Ref) -> dict:
    qual = db.get(Qualification, c.qualification_id) if c.qualification_id else None  # a code only
    modules = []
    for m in (db.query(CourseModule).filter(CourseModule.course_id == c.id)
              .order_by(CourseModule.ordinal, CourseModule.id).all()):
        po = db.get(PerformanceObjective, m.po_id) if m.po_id else None
        po_qual = db.get(Qualification, po.qualification_id) if po is not None else None
        content_ref, legacy_scenario = module_ref(m.content_ref)
        contents = []
        for mc in (db.query(ModuleContent).filter(ModuleContent.module_id == m.id)
                   .order_by(ModuleContent.ordinal, ModuleContent.id).all()):
            contents.append({"ordinal": mc.ordinal or 0,
                             "content_kind": getattr(mc.content_kind, "value", mc.content_kind),
                             "scenario": _scenario_ref(db, c, mc.scenario_id, ref),
                             "external_ref": mc.external_ref or "",
                             "lesson": _lesson(db, c, mc.lesson_id), "quiz": _quiz(db, c, mc.quiz_id)})
        modules.append({
            "ordinal": m.ordinal or 0, "title": m.title, "description": m.description or "",
            "content_type": getattr(m.content_type, "value", m.content_type),
            "duration_minutes": m.duration_minutes or 0, "is_required": bool(m.is_required),
            "pass_threshold": m.pass_threshold if m.pass_threshold is not None else 70,
            "po": {"qsp_code": po_qual.qsp_code, "po_code": po.po_code} if po is not None and po_qual else None,
            "scenario": _scenario_ref(db, c, legacy_scenario, ref), "content_ref": content_ref,
            "contents": contents})
    return {"name": c.name, "description": c.description or "", "version": c.version or "",
            "difficulty": c.difficulty or "intermediate", "duration_hours": c.duration_hours or 0,
            "tags": c.tags or "", "nice_work_roles": c.nice_work_roles or "", "course_meta": course_meta(c.course_meta),
            "qsp_code": qual.qsp_code if qual is not None else None, "modules": modules}


def content_of(db: Session, item_type: str, row, ref: Ref) -> dict:
    if item_type == "range_template":
        return template_content(row)
    if item_type == "scenario":
        return scenario_content(db, row, ref)
    return course_content(db, row, ref)


def local_ref(item_type: str, item_id: str) -> str:
    return key(item_type, item_id)


def fingerprint(db: Session, item_type: str, row) -> str:
    """The local item's content, hashed: equal before and after means nobody changed it. A
    template or scenario is hashed as stored (comments and sealed values included), so any
    edit shows."""
    if item_type == "course":
        return sha256(content_of(db, item_type, row, local_ref))
    return sha256({"name": row.name, "version": row.version or "", "yaml": row.yaml or ""})


def refs_in(item_type: str, content: dict) -> set[str]:
    """Every item key ``content`` refers to."""
    if item_type == "scenario":
        return {content["range_template"]} if content.get("range_template") else set()
    if item_type != "course":
        return set()
    out: set[str] = set()
    for m in content.get("modules") or []:
        if m.get("scenario"):
            out.add(m["scenario"])
        out |= {c["scenario"] for c in m.get("contents") or [] if c.get("scenario")}
    return out


# ── writing ──────────────────────────────────────────────────────────────────────
def proof_summary(st: proven.Status) -> dict:
    """What a bundle says about an item's proof: conditions and evidence ids and dates only
    (no class, range or Student names)."""
    return {"proven": st.proven, "conditions": [
        {"name": c.name, "met": c.met, "not_applicable": c.not_applicable,
         "evidence": [{"kind": e.kind, "id": e.id, "at": e.at.isoformat() if e.at else None}
                      for e in c.evidence[:20]]}
        for c in st.conditions]}


def build(db: Session, items: list[tuple[str, object]], statuses: dict[str, proven.Status], *,
          now: datetime) -> dict:
    """The signed bundle of ``items`` (already closed over and proven)."""
    carried = {key(t, str(r.id)) for t, r in items}

    def ref(item_type: str, item_id: str) -> str | None:
        k = key(item_type, item_id)
        return k if k in carried else None

    out_items, all_builds = [], {}
    for item_type, row in items:
        k = key(item_type, str(row.id))
        content = content_of(db, item_type, row, ref)
        st = statuses[k]
        deps = sorted({key(d.item_type, d.item_id) for d in st.dependencies} & carried | refs_in(item_type, content))
        for b in st.golden_builds:
            all_builds[(b["catalogue_id"], b["build_id"], b["checksum"])] = b
        out_items.append({"key": k, "item_type": item_type, "origin_id": str(row.id),
                          "content_sha256": sha256(content), "content": content, "depends_on": deps,
                          "proof": proof_summary(st), "golden_builds": st.golden_builds,
                          "removed_passwords": removed_passwords(row.yaml) if item_type == "range_template" else 0})
    doc = {"format": FORMAT, "bundle_id": str(uuid.uuid4()), "exported_at": now.isoformat(),
           "origin": {"key_id": signing.public_info()["key_id"]}, "items": out_items,
           "golden_builds": [all_builds[k] for k in sorted(all_builds)]}
    return signing.sign(doc)


# ── reading ──────────────────────────────────────────────────────────────────────
def _pairs(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _no_constant(name: str):
    raise ValueError(f"{name} is not allowed")


def _no_float(text: str):
    raise ValueError("numbers must be integers")


def loads(raw: bytes) -> object:
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_no_constant, parse_float=_no_float)


def parse(raw: bytes, *, now: datetime) -> tuple[dict, BundleIn, str]:
    """(the document, the validated bundle, the trusted key id that signed it), or
    BundleRejectedError. signing.NoTrustedKeysError when no key is configured."""
    signing.trusted_keys()  # fail closed before reading anything
    if len(raw) > MAX_BUNDLE_BYTES:
        raise BundleRejectedError(413, f"The bundle is larger than {MAX_BUNDLE_BYTES // (1024 * 1024)} MB")
    try:
        doc = loads(raw)
    except (ValueError, RecursionError, MemoryError):
        raise BundleRejectedError(422, "The file is not a content-port bundle (not strict JSON)") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("signature"), dict):
        raise BundleRejectedError(422, "The bundle is not signed; nothing was imported")
    kid = signing.verify(doc)
    if kid is None:
        raise BundleRejectedError(422, "The bundle's signature does not verify against a trusted content-port key "
                                       "(tn_content_port_trusted_keys); nothing was imported")
    try:
        bundle = BundleIn.model_validate({k: v for k, v in doc.items() if k != "signature"})
    except RecursionError:
        raise BundleRejectedError(422, "The signed bundle is not a content-port bundle (nested too deep)") from None
    except ValidationError as exc:
        raise BundleRejectedError(422, f"The signed bundle is not a content-port bundle ({_first(exc)})") from None
    if bundle.origin.key_id != kid:
        raise BundleRejectedError(422, "The bundle names another origin key than the one that signed it")
    exported = bundle.exported_at if bundle.exported_at.tzinfo else bundle.exported_at.replace(tzinfo=UTC)
    if exported > now + MAX_FUTURE:
        raise BundleRejectedError(422, f"This bundle is dated {exported.isoformat()}, more than a day in the future; "
                                       f"check the clock of the site that exported it")
    _check_items(bundle)
    return doc, bundle, kid


def _first(exc: ValidationError) -> str:
    first = exc.errors()[0]
    return f"{'.'.join(str(p) for p in first.get('loc', ()))}: {first.get('msg')}"


def _check_items(bundle: BundleIn) -> None:
    keys = [i.key for i in bundle.items]
    if len(set(keys)) != len(keys):
        raise BundleRejectedError(422, "The bundle carries an item twice")
    present = set(keys)
    for i in bundle.items:
        if i.key != key(i.item_type, i.origin_id):
            raise BundleRejectedError(422, f"Item {i.key}: its key is not its type and origin id")
        try:
            content = CONTENT_MODELS[i.item_type].model_validate(i.content)
        except RecursionError:
            raise BundleRejectedError(422, f"Item {i.key}: content nested too deep") from None
        except ValidationError as exc:
            raise BundleRejectedError(422, f"Item {i.key}: not a {i.item_type} ({_first(exc)})") from None
        if content.model_dump(mode="json") != i.content:
            raise BundleRejectedError(422, f"Item {i.key}: its content is not exactly a {i.item_type}'s")
        if sha256(i.content) != i.content_sha256:
            raise BundleRejectedError(422, f"Item {i.key}: its content does not match its content_sha256")
        refs = refs_in(i.item_type, i.content)
        if not refs <= set(i.depends_on):
            raise BundleRejectedError(422, f"Item {i.key}: it refers to items it does not list as dependencies")
        if missing := sorted(set(i.depends_on) - present):
            raise BundleRejectedError(422, f"Item {i.key}: its dependencies are not in the bundle "
                                           f"({', '.join(missing[:5])})")
        if i.key in i.depends_on:
            raise BundleRejectedError(422, f"Item {i.key}: it depends on itself")
