"""Course content ingestion — modules, labs and quizzes for a catalogue course.

Loads an authored course file (``content/courses/*.yaml``) onto a course that the
programme catalogue has already created, producing real ``CourseModule``,
``Quiz`` and ``QuizQuestion`` rows.

Guardrails, same as ``programme_ingest``:

* Everything is created **unpublished**. Draft courseware is not learner-facing.
* The course must already exist in the catalogue; this module never invents one.
  Import the programme catalogue first.
* No competency codes are written. The authored draft this format was designed
  around asserted NICE/CSF mappings that did not resolve, so framework mapping is
  left to ``crosswalk.csv`` and the QSP spine, which are auditable.
* Module ``refs`` are keys into ``content/catalogue/references.yaml``. Course files
  cannot name a citation that is not in that verified library; the test suite
  enforces membership, which is how invented citations are kept out.
* ``provenance`` from the file is preserved on every course it touches.

Binding to the CFITES spine is **explicit and optional**. A module may declare::

    po: {qsp_code: ALJQ, po_code: PO_009}

which sets ``CourseModule.po_id`` — the single link ``qsp_progress`` walks to place a
learner on the developmental path. A course may declare a top-level ``qsp_code`` which
sets ``Course.qualification_id``. Neither is ever inferred: asserting that a module
satisfies a performance objective is a CFITES claim that determines whether a CAF
member is qualified, and that is Standards' decision, not this importer's. A declared
PO that does not resolve against the ingested spine is an error, not a silent skip.

Re-import is idempotent (upsert on ``(course_id, ordinal)`` and ``module_id``).
"""

from __future__ import annotations

import json

import yaml
from sqlalchemy.orm import Session

from .models import (
    Course,
    CourseModule,
    ModuleContentType,
    PerformanceObjective,
    Qualification,
    Quiz,
    QuizQuestion,
    QuizQuestionType,
)

_OPTION_PREFIX_LEN = 3  # "A) "


def _option_text(raw: str) -> str:
    """'A) MQTT' -> 'MQTT'. Leaves unprefixed options untouched."""
    s = str(raw).strip()
    if len(s) > _OPTION_PREFIX_LEN and s[0].isalpha() and s[1] == ")":
        return s[_OPTION_PREFIX_LEN - 1 :].strip()
    return s


def answer_indices(answer: str, options: list[str]) -> list[int]:
    """Map an answer letter ('C', or 'A,C') onto option indices.

    QuizQuestion.correct stores indices, not letters. Unrecognised answers yield
    an empty list rather than a guessed index — a question with no key is
    reviewable; one with a wrong key silently mis-grades learners.
    """
    out: list[int] = []
    for tok in str(answer or "").replace(";", ",").split(","):
        tok = tok.strip().upper()
        if len(tok) == 1 and tok.isalpha():
            idx = ord(tok) - ord("A")
            if 0 <= idx < len(options):
                out.append(idx)
    return sorted(set(out))


def _po_ref(raw) -> dict | None:
    """Normalise an optional module-level ``po:`` binding."""
    if not isinstance(raw, dict):
        return None
    qsp = str(raw.get("qsp_code") or "").strip()
    po = str(raw.get("po_code") or "").strip()
    if not qsp or not po:
        return None
    return {"qsp_code": qsp, "po_code": po}


def parse_course_content(yaml_text: str) -> dict:
    """Parse an authored course file into a normalized dict. Pure (no DB)."""
    doc = yaml.safe_load(yaml_text) or {}
    course_code = str(doc.get("course_code") or "").strip()
    if not course_code:
        raise ValueError("course file has no course_code")

    modules = []
    for raw in doc.get("modules") or []:
        options_by_q = []
        for q in (raw.get("quiz") or {}).get("questions") or []:
            options = [_option_text(o) for o in q.get("options") or []]
            options_by_q.append(
                {
                    "stem": str(q.get("question") or "").strip(),
                    "options": options,
                    "correct": answer_indices(q.get("answer", ""), options),
                }
            )
        ct = str(raw.get("content_type") or "reading").strip()
        modules.append(
            {
                "ordinal": int(raw.get("ordinal") or 0),
                "title": str(raw.get("title") or "").strip(),
                "content_type": (
                    ModuleContentType(ct) if ct in {m.value for m in ModuleContentType} else ModuleContentType.reading
                ),
                "duration_minutes": int(raw.get("duration_minutes") or 0),
                "is_required": bool(raw.get("is_required", True)),
                "pass_threshold": int(raw.get("pass_threshold") or 70),
                "objectives": list(raw.get("objectives") or []),
                "topics": list(raw.get("topics") or []),
                "lab": str(raw.get("lab") or "").strip(),
                "refs": list(raw.get("refs") or []),
                "po": _po_ref(raw.get("po")),
                "quiz_title": (raw.get("quiz") or {}).get("title") or "",
                "quiz_pass": int((raw.get("quiz") or {}).get("pass_threshold") or 70),
                "questions": options_by_q,
            }
        )

    return {
        "course_code": course_code,
        "title": str(doc.get("title") or "").strip(),
        "version": str(doc.get("version") or "1.0"),
        "difficulty": str(doc.get("difficulty") or "intermediate"),
        "duration_hours": int(doc.get("duration_hours") or 0),
        "provenance": str(doc.get("provenance") or "unsourced").lower(),
        "status": str(doc.get("status") or "draft").lower(),
        "source": doc.get("source") or {},
        "qsp_code": (str(doc.get("qsp_code")).strip() if doc.get("qsp_code") else None),
        "modules": modules,
    }


def _resolve_po(db: Session, ref: dict | None, course_code: str, ordinal: int):
    """Resolve a declared ``po:`` binding to a PerformanceObjective id.

    Returns None when the module declares no binding. Raises when it declares one
    that does not exist — a mapping that silently fails would leave the module
    invisible on the developmental path while appearing to be wired up.
    """
    if ref is None:
        return None
    qual = db.query(Qualification).filter_by(qsp_code=ref["qsp_code"]).one_or_none()
    if qual is None:
        raise ValueError(
            f"{course_code} module {ordinal} maps to qsp_code={ref['qsp_code']!r}, "
            "which is not in the ingested spine; import crosswalk.csv first"
        )
    po = db.query(PerformanceObjective).filter_by(qualification_id=qual.id, po_code=ref["po_code"]).one_or_none()
    if po is None:
        raise ValueError(
            f"{course_code} module {ordinal} maps to {ref['qsp_code']}/{ref['po_code']}, "
            "which is not a performance objective in the crosswalk"
        )
    return po.id


def _claim_po(db: Session, module: CourseModule, course_code: str, ordinal: int, stats: dict) -> None:
    """Make this module the one that delivers its PO.

    ``qsp_progress`` takes the first module it finds for a performance objective, so a
    second claimant is silently ignored. Spine-generated placeholder courses
    (``qsp_paths._po_course``) bind a module to every PO precisely because no real
    content existed; authored content supersedes them and the placeholder releases its
    claim. Two *authored* courses claiming the same PO is an error, not a race.
    """
    others = db.query(CourseModule).filter(CourseModule.po_id == module.po_id, CourseModule.id != module.id).all()
    for other in others:
        course = db.query(Course).filter_by(id=other.course_id).one_or_none()
        meta = {}
        if course is not None:
            try:
                meta = json.loads(course.course_meta or "{}")
            except (TypeError, ValueError):
                meta = {}
        if meta.get("provenance"):
            raise ValueError(
                f"{course_code} module {ordinal} claims a performance objective already "
                f"delivered by authored course {course.name!r}; each objective may have "
                "only one delivering module"
            )
        other.po_id = None  # placeholder yields to real content
        stats["placeholders_superseded"] = stats.get("placeholders_superseded", 0) + 1


def _find_course(db: Session, course_code: str, tenant_id: str | None) -> Course | None:
    """Locate the catalogue course by its course_meta.course_code."""
    for course in db.query(Course).filter_by(tenant_id=tenant_id).all():
        try:
            meta = json.loads(course.course_meta or "{}")
        except (TypeError, ValueError):
            continue
        if str(meta.get("course_code") or "").strip() == course_code:
            return course
    return None


def import_course_content(db: Session, yaml_text: str, tenant_id: str | None = None) -> dict:
    """Attach authored modules and quizzes to an existing catalogue course."""
    doc = parse_course_content(yaml_text)
    course = _find_course(db, doc["course_code"], tenant_id)
    if course is None:
        raise ValueError(
            f"no catalogue course with course_code={doc['course_code']!r}; "
            "import the programme catalogue before its content"
        )

    stats = {
        "course_code": doc["course_code"],
        "modules_created": 0,
        "modules_updated": 0,
        "quizzes": 0,
        "questions": 0,
        "questions_without_key": 0,
        "modules_bound_to_po": 0,
        "placeholders_superseded": 0,
        "bound_to_qualification": False,
    }

    # Optional course-level CFITES binding.
    if doc["qsp_code"]:
        qual = db.query(Qualification).filter_by(qsp_code=doc["qsp_code"]).one_or_none()
        if qual is None:
            raise ValueError(
                f"course {doc['course_code']} declares qsp_code={doc['qsp_code']!r}, "
                "which is not in the ingested spine; import crosswalk.csv first"
            )
        course.qualification_id = qual.id
        stats["bound_to_qualification"] = True

    course.duration_hours = doc["duration_hours"] or course.duration_hours
    course.difficulty = doc["difficulty"]
    course.version = doc["version"]
    meta = json.loads(course.course_meta or "{}")
    meta["content_provenance"] = doc["provenance"]
    meta["content_status"] = doc["status"]
    meta["content_source"] = doc["source"]
    course.course_meta = json.dumps(meta)
    # Authored draft content never publishes a course.
    course.is_published = False

    for m in doc["modules"]:
        module = db.query(CourseModule).filter_by(course_id=course.id, ordinal=m["ordinal"]).one_or_none()
        created = module is None
        if created:
            module = CourseModule(course_id=course.id, ordinal=m["ordinal"])
            db.add(module)
        module.title = m["title"]
        module.description = "\n".join(m["topics"])
        module.content_type = m["content_type"]
        module.duration_minutes = m["duration_minutes"]
        module.is_required = m["is_required"]
        module.pass_threshold = m["pass_threshold"]
        module.po_id = _resolve_po(db, m["po"], doc["course_code"], m["ordinal"])
        if module.po_id is not None:
            _claim_po(db, module, doc["course_code"], m["ordinal"], stats)
            stats["modules_bound_to_po"] += 1
        module.content_ref = json.dumps(
            {
                "objectives": m["objectives"],
                "topics": m["topics"],
                "lab": m["lab"],
                "refs": m["refs"],
            }
        )
        db.flush()
        stats["modules_created" if created else "modules_updated"] += 1

        if not m["questions"]:
            continue
        quiz = db.query(Quiz).filter_by(module_id=module.id).one_or_none()
        if quiz is None:
            quiz = Quiz(module_id=module.id, tenant_id=tenant_id)
            db.add(quiz)
            stats["quizzes"] += 1
        quiz.title = m["quiz_title"] or f"{m['title']} quiz"
        quiz.pass_pct = m["quiz_pass"]
        quiz.is_published = False
        db.flush()

        quiz.questions.clear()
        db.flush()
        for i, q in enumerate(m["questions"], start=1):
            if not q["correct"]:
                stats["questions_without_key"] += 1
            db.add(
                QuizQuestion(
                    quiz_id=quiz.id,
                    ordinal=i,
                    question_type=QuizQuestionType.mcq,
                    stem=q["stem"],
                    options=json.dumps(q["options"]),
                    correct=json.dumps(q["correct"]),
                )
            )
            stats["questions"] += 1
        db.flush()

    db.commit()
    return stats
