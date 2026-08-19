"""Academic programme catalogue ingestion.

Parses ``content/catalogue/cyber_operator_programme.csv`` — the academic delivery
schedule (institution, term, course code/title) for the developmental periods
already defined by the QSP spine — into ``Course`` rows.

This module deliberately does NOT invent curriculum structure:

* Courses are created **unpublished** (``is_published=False``). Nothing here has
  been validated by Standards.
* No ``CourseModule`` rows are created. Empty modules are honest; placeholder
  ones ("Module 1".."Module 6") are not.
* ``qualification_id`` is set **only** when the row names a real ``qsp_code``.
  Rows carrying the ``QSP-TODO`` sentinel stay unbound rather than fabricating a
  CFITES claim -- see ``qsp_paths`` on why invented spine data must never reach
  CAF users.
* Every row's provenance is preserved verbatim in ``course_meta``.

DP1/DP2 in the catalogue mean the same thing they mean in ``qsp_paths.DP_LADDER``
(DP1 = Pte/ALJQ, DP2 = Cpl/TEMP67|TEMP64|ALRA); the ``dp_order`` column carries
that linkage, so no parallel ``dp1``/``dp2`` tag namespace is introduced.

Re-import is idempotent (upsert on the natural key ``(tenant_id, name)``).
"""

from __future__ import annotations

import csv
import io
import json

from sqlalchemy.orm import Session

from .models import Course, Qualification

# `qsp_code` values that carry no real qualification linkage.
_QSP_SENTINELS = {"", "-", "qsp-todo", "todo", "n/a", "none", "tbd"}

# Provenance values that are safe to publish. Nothing currently qualifies; the
# set exists so that adding a sourced row is a data change, not a code change.
_SOURCED_PROVENANCE: set[str] = set()

EXPECTED_COLUMNS = [
    "programme",
    "institution",
    "dp_order",
    "qsp_code",
    "term_code",
    "term_label",
    "term_start",
    "term_end",
    "weeks",
    "course_code",
    "course_title",
    "duration_hours",
    "provenance",
    "status",
]


def _to_int(value: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def course_name(course_code: str, course_title: str) -> str:
    """Natural key for a catalogue course.

    Mirrors the ``"{code} — {title}"`` convention already used by
    ``qsp_paths._po_course`` so both generators produce consistent course names.
    """
    code = (course_code or "").strip()
    title = (course_title or "").strip()
    if code and title:
        return f"{code} — {title}"
    return code or title


def parse_programme(csv_text: str) -> list[dict]:
    """Parse programme catalogue CSV text into normalized, typed row dicts.

    Pure function (no DB) so it is unit-testable. Rows missing a course code or
    title are skipped. ``qsp_code`` sentinels normalize to ``None``.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    rows: list[dict] = []
    for raw in reader:
        if not raw:
            continue
        course_code = (raw.get("course_code") or "").strip()
        course_title = (raw.get("course_title") or "").strip()
        if not course_code or not course_title:
            continue
        qsp_raw = (raw.get("qsp_code") or "").strip()
        rows.append(
            {
                "programme": (raw.get("programme") or "").strip(),
                "institution": (raw.get("institution") or "").strip(),
                "dp_order": _to_int(raw.get("dp_order", 0)),
                "qsp_code": None if qsp_raw.lower() in _QSP_SENTINELS else qsp_raw,
                "term_code": (raw.get("term_code") or "").strip(),
                "term_label": (raw.get("term_label") or "").strip(),
                "term_start": (raw.get("term_start") or "").strip(),
                "term_end": (raw.get("term_end") or "").strip(),
                "weeks": _to_int(raw.get("weeks", 0)),
                "course_code": course_code,
                "course_title": course_title,
                "duration_hours": _to_int(raw.get("duration_hours", 0)),
                "provenance": (raw.get("provenance") or "unsourced").strip().lower(),
                "status": (raw.get("status") or "proposed").strip().lower(),
            }
        )
    return rows


def import_programme(db: Session, csv_text: str, tenant_id: str | None = None) -> dict:
    """Upsert catalogue courses from programme CSV. Idempotent.

    Returns counts of courses created/updated, plus how many rows stayed unbound
    from the qualification spine.
    """
    rows = parse_programme(csv_text)
    stats = {
        "rows": len(rows),
        "courses_created": 0,
        "courses_updated": 0,
        "unbound_rows": 0,
        "published": 0,
    }

    qual_cache: dict[str, Qualification | None] = {}
    for row in rows:
        qual_id = None
        qsp_code = row["qsp_code"]
        if qsp_code:
            if qsp_code not in qual_cache:
                qual_cache[qsp_code] = db.query(Qualification).filter_by(qsp_code=qsp_code).one_or_none()
            qual = qual_cache[qsp_code]
            qual_id = qual.id if qual is not None else None
        if qual_id is None:
            stats["unbound_rows"] += 1

        name = course_name(row["course_code"], row["course_title"])
        course = db.query(Course).filter_by(tenant_id=tenant_id, name=name).one_or_none()
        created = course is None
        if created:
            course = Course(name=name, tenant_id=tenant_id)
            db.add(course)

        course.description = f"{row['course_title']} — {row['term_label']}, {row['institution']}."
        course.duration_hours = row["duration_hours"]
        course.qualification_id = qual_id
        course.tags = json.dumps([row["programme"], row["status"]])
        course.course_meta = json.dumps(
            {
                "course_code": row["course_code"],
                "programme": row["programme"],
                "institution": row["institution"],
                "dp_order": row["dp_order"],
                "qsp_code": qsp_code,
                "term_code": row["term_code"],
                "term_label": row["term_label"],
                "term_start": row["term_start"],
                "term_end": row["term_end"],
                "weeks": row["weeks"],
                "provenance": row["provenance"],
                "status": row["status"],
            }
        )
        # Unsourced content is never published. This is the guardrail, not a default.
        course.is_published = row["provenance"] in _SOURCED_PROVENANCE
        if course.is_published:
            stats["published"] += 1

        db.flush()
        if created:
            stats["courses_created"] += 1
        else:
            stats["courses_updated"] += 1

    db.commit()
    return stats


def generate_programme_paths(db: Session, tenant_id: str | None = None) -> dict:
    """Build LearningPaths for the academic programme from imported catalogue courses.

    These are *delivery* paths — the order courses are taught in, term by term. They
    are deliberately separate from the CFITES developmental path built by
    ``qsp_paths.generate_learning_paths``, which is derived from the QSP spine and
    carries qualification meaning. A programme path asserts only "these courses are
    scheduled in this order", which is what the calendar actually says.

    Paths are created unpublished. Idempotent.
    """
    # Imported locally: these are the same helpers the QSP path generator uses, and
    # reusing them keeps both generators producing structurally identical paths.
    from .qsp_paths import _get_or_create_path, _linear_prereq

    courses = db.query(Course).filter_by(tenant_id=tenant_id).all()
    by_term: dict[str, list[tuple[str, str, Course]]] = {}
    by_programme: dict[tuple[str, int], list[tuple[str, str, Course]]] = {}

    for course in courses:
        try:
            meta = json.loads(course.course_meta or "{}")
        except (TypeError, ValueError):
            continue
        term = str(meta.get("term_code") or "").strip()
        code = str(meta.get("course_code") or "").strip()
        programme = str(meta.get("programme") or "").strip()
        if not term or not code or not programme:
            continue
        entry = (code, str(meta.get("term_label") or term), course)
        by_term.setdefault(term, []).append(entry)
        by_programme.setdefault((programme, int(meta.get("dp_order") or 0)), []).append(entry)

    stats = {"term_paths": 0, "programme_paths": 0}

    for term, entries in sorted(by_term.items()):
        entries.sort(key=lambda e: e[0])
        course_ids = [str(c.id) for _, _, c in entries]
        label = entries[0][1]
        _get_or_create_path(
            db,
            name=f"Programme term — {label}",
            description=(
                f"Scheduled delivery for {label} ({term}). Unsourced draft schedule; carries no qualification claim."
            ),
            course_ids=course_ids,
            prereq=_linear_prereq(course_ids),
            tenant_id=tenant_id,
        )
        stats["term_paths"] += 1

    for (programme, dp_order), entries in sorted(by_programme.items()):
        entries.sort(key=lambda e: e[0])
        course_ids = [str(c.id) for _, _, c in entries]
        _get_or_create_path(
            db,
            name=f"Programme — {programme} DP{dp_order}",
            description=(
                f"Full course sequence for {programme} developmental period {dp_order}. "
                "Unsourced draft schedule; carries no qualification claim."
            ),
            course_ids=course_ids,
            prereq=_linear_prereq(course_ids),
            tenant_id=tenant_id,
        )
        stats["programme_paths"] += 1

    db.commit()
    return stats
