"""Where an xAPI statement sits: its registration and its language, read from the database.

``app.xapi`` builds statements and never touches the database; the routers call these
helpers to learn the context first. Registration rules (docs/xapi-conformance.md):

* work inside a course (a quiz bound to a module of a course the Student is enrolled in)
  carries the **enrolment** as its registration: ``enrollments.id``, one per Student and
  course, the same value TrueNorth's cmi5 launches use (``app.cmi5``);
* a stand-alone quiz carries the **attempt** (``quiz_attempts.id``);
* range work carries the **exercise run** (``exercises.id``), shared by everyone in it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .models import Course, CourseModule, Enrollment, Quiz

LOCALE_KEYS = ("locale", "language", "lang")


@dataclass(frozen=True)
class StatementContext:
    registration: uuid.UUID | None
    language: str | None  # None: the platform default (XAPI_DEFAULT_LANGUAGE)


def course_locale(course: Course | None) -> str | None:
    """The course's locale from ``course_meta`` (``locale``/``language``/``lang``), if set."""
    if course is None:
        return None
    try:
        meta = json.loads(course.course_meta or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    for key in LOCALE_KEYS:
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def enrolment(db: Session, user_id: uuid.UUID, course_id: uuid.UUID) -> Enrollment | None:
    return db.query(Enrollment).filter(Enrollment.user_id == user_id, Enrollment.course_id == course_id).first()


def quiz_context(db: Session, quiz: Quiz, user_id: uuid.UUID, attempt_id: uuid.UUID) -> StatementContext:
    """The enrolment when the quiz is part of a course the Student is enrolled in, else the attempt."""
    if quiz.module_id:
        module = db.get(CourseModule, quiz.module_id)  # tenant-safe: the quiz's own module
        if module is not None:
            course = db.get(Course, module.course_id)  # tenant-safe: that module's course
            enrolled = enrolment(db, user_id, module.course_id)
            return StatementContext(enrolled.id if enrolled else attempt_id, course_locale(course))
    return StatementContext(attempt_id, None)
