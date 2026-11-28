# Author: Marc-Andre Blanke
# Created: 2026-11-27T00:00:00Z
"""The Students page: every Student of the caller's organisation at a glance, and one
Student's record (competency and class history), for instructors and admins.

**Same rules as the readiness page.** Classes, attendance, results and qualifications come
from ``readiness.build`` over the widest window it allows (a year ahead and about nine
years back), so a number here always equals the readiness page's for the same class. A Student
is matched to their roll-call entries by email, as the roll call matches people.

**Proficiency** is a level name, never a score: the mean of the Student's NICE competency
assertions (``competency.PROFICIENCY_SCORE``) read back to the nearest level. Per category
uses ``competency.nice_category``, as the heatmap does.

**Who.** Students only (role student, active, not deleted), of the caller's tenant only.
Reads only; nothing is written.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy.orm import Session

from ..models import Competency, CompetencyAssertion, CompetencyFramework, User, UserRole
from ..routers.competency import NICE_CATEGORIES, PROFICIENCY_SCORE, nice_category
from . import readiness
from .readiness_schemas import UnitReadinessOut
from .student_progress_schemas import (
    StudentClassOut,
    StudentCompetencyOut,
    StudentListOut,
    StudentRecordOut,
    StudentRowOut,
)

# Mean score -> level: the midpoints between the levels' scores (10, 30, 50, 75, 95).
_LEVEL_FLOORS = ((85, "expert"), (62.5, "advanced"), (40, "intermediate"), (20, "beginner"), (0, "novice"))


def level_for(mean_score: float | None) -> str | None:
    """The proficiency level nearest a mean assertion score; None for no assertions."""
    if mean_score is None:
        return None
    return next(name for floor, name in _LEVEL_FLOORS if mean_score >= floor)


def _students(db: Session, tenant_id: uuid.UUID) -> list[User]:
    return (
        db.query(User)
        .filter(
            User.tenant_id == tenant_id,
            User.role == UserRole.student,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
        .all()
    )


AHEAD = timedelta(days=365)  # classes booked up to a year ahead still show on a Student's record


def _classes(db: Session, tenant_id: uuid.UUID, today: date | None = None) -> UnitReadinessOut:
    today = today or readiness._now().date()
    end = today + AHEAD
    start = end - readiness.MAX_SPAN
    return readiness.build(db, tenant_id, start, end, None)


def _scores(db: Session, tenant_id: uuid.UUID, user_ids: list[uuid.UUID] | None = None):
    """user id -> category -> list of assertion scores (NICE only, categorised ones)."""
    q = (
        db.query(CompetencyAssertion.user_id, Competency.code, Competency.category, CompetencyAssertion.proficiency)
        .join(Competency, CompetencyAssertion.competency_id == Competency.id)
        .join(User, CompetencyAssertion.user_id == User.id)
        .filter(Competency.framework == CompetencyFramework.nice, User.tenant_id == tenant_id)
    )
    if user_ids is not None:
        q = q.filter(CompetencyAssertion.user_id.in_(user_ids))
    out: dict[uuid.UUID, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for uid, code, stored, proficiency in q.all():
        category = nice_category(code, stored)
        level = getattr(proficiency, "value", proficiency)
        if category and level in PROFICIENCY_SCORE:
            out[uid][category].append(PROFICIENCY_SCORE[level])
    return out


def _mean(values: list[int]) -> float | None:
    return sum(values) / len(values) if values else None


def _row(user: User, places: list, scores: dict[str, list[int]]) -> dict:
    """The list row's fields for one Student, from their (class, student) places."""
    every = [s for cat in scores.values() for s in cat]
    quals = sorted({s.qualification_earned for _, s in places if s.qualification_earned})
    tally = defaultdict(int)
    for _, s in places:
        tally[s.outcome.value if s.outcome else None] += 1
    return {
        "user_id": user.id,
        "display_name": user.display_name or user.email,
        "email": user.email,
        "rank": user.rank,
        "unit": (user.unit or "").strip() or None,
        "callsign": user.callsign,
        "proficiency": level_for(_mean(every)),
        "classes": len(places),
        "present": sum(s.present for _, s in places),
        "excused": sum(s.excused for _, s in places),
        "absent": sum(s.absent for _, s in places),
        "passed": tally["pass"],
        "failed": tally["fail"],
        "incomplete": tally["incomplete"],
        "awaiting_result": tally[None],
        "qualifications": quals,
    }


def _places(report: UnitReadinessOut) -> dict[str, list]:
    """email (lower) -> [(class, student row)], every class in the report."""
    out: dict[str, list] = defaultdict(list)
    for c in report.classes:
        for s in c.students:
            out[s.email.strip().lower()].append((c, s))
    return out


def list_students(db: Session, tenant_id: uuid.UUID, unit: str | None = None, q: str | None = None) -> StudentListOut:
    """Every Student of the tenant, filtered by formation (case and spaces ignored) and by a
    search over name, email and callsign. Sorted by formation (none last), then surname."""
    students = _students(db, tenant_id)
    spelt: dict[str, str] = {}
    for u in students:
        key, shown = readiness.unit_key(u.unit), (u.unit or "").strip()
        if key and (key not in spelt or shown < spelt[key]):
            spelt[key] = shown
    units = sorted(spelt.values(), key=lambda s: (s.lower(), s))

    want = readiness.unit_key(unit) or None
    needle = (q or "").strip().lower()
    chosen = [
        u
        for u in students
        if (want is None or readiness.unit_key(u.unit) == want)
        and (not needle or any(needle in (v or "").lower() for v in (u.display_name, u.email, u.callsign)))
    ]
    places = _places(_classes(db, tenant_id)) if chosen else {}
    scores = _scores(db, tenant_id) if chosen else {}
    # Formation (no formation last), then surname: display names often start with a rank.
    chosen.sort(
        key=lambda u: (
            not readiness.unit_key(u.unit),
            readiness.unit_key(u.unit),
            (u.last_name or u.display_name or "").lower(),
            (u.first_name or "").lower(),
            u.email,
        )
    )
    rows = [StudentRowOut(**_row(u, places.get(u.email.strip().lower(), []), scores.get(u.id, {}))) for u in chosen]
    return StudentListOut(units=units, students=rows)


def student_record(db: Session, tenant_id: uuid.UUID, user_id: uuid.UUID) -> StudentRecordOut | None:
    """One Student's record, or None when there is no such Student in the tenant."""
    user = db.get(User, user_id)  # tenant-safe: checked on the next line
    if (
        user is None
        or user.tenant_id != tenant_id
        or user.role != UserRole.student
        or not user.is_active
        or user.deleted_at is not None
    ):
        return None
    places = _places(_classes(db, tenant_id)).get(user.email.strip().lower(), [])
    scores = _scores(db, tenant_id, [user.id]).get(user.id, {})
    competencies = [
        StudentCompetencyOut(category=cat, proficiency=level_for(_mean(scores[cat])), assertions=len(scores[cat]))
        for cat in NICE_CATEGORIES
        if scores.get(cat)
    ]
    history = [
        StudentClassOut(
            class_id=c.class_id,
            name=c.name,
            course_name=c.course_name,
            qualification=c.qualification,
            start_date=c.start_date,
            end_date=c.end_date,
            status=c.status,
            sessions_held=c.sessions_held,
            present=s.present,
            excused=s.excused,
            absent=s.absent,
            outcome=s.outcome,
            comment=s.comment,
            decided_at=s.decided_at,
            qualification_earned=s.qualification_earned,
        )
        for c, s in sorted(places, key=lambda p: (p[0].start_date, p[0].name), reverse=True)
    ]
    return StudentRecordOut(**_row(user, places, scores), competencies=competencies, class_history=history)
