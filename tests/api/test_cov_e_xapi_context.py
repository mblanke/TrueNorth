# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Round-2 coverage for app/xapi_context.py: registration and language of an xAPI statement.

The helpers read only three tables (Course, CourseModule, Enrollment), so each rule is
exercised against real rows on the in-memory database.
"""

import json
import uuid

from app.models import Course, CourseModule, Enrollment, ModuleContentType, Quiz, User, UserRole
from app.xapi_context import LOCALE_KEYS, StatementContext, course_locale, enrolment, quiz_context

DEV = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _course(db, *, meta=None) -> Course:
    c = Course(
        id=uuid.uuid4(),
        name=f"C9{uuid.uuid4().hex[:2]} XAPI course",
        version="1.0",
        tenant_id=DEV,
        duration_hours=1,
        course_meta=json.dumps(meta) if meta is not None else None,
    )
    db.add(c)
    db.flush()
    return c


def _quiz(db, course: Course | None = None) -> Quiz:
    module_id = None
    if course is not None:
        m = CourseModule(
            id=uuid.uuid4(),
            course_id=course.id,
            ordinal=1,
            title="Quiz module",
            content_type=ModuleContentType.scenario,
        )
        db.add(m)
        db.flush()
        module_id = m.id
    q = Quiz(id=uuid.uuid4(), title="Check", module_id=module_id, tenant_id=DEV)
    db.add(q)
    db.flush()
    return q


def _student(db) -> User:
    u = User(
        id=uuid.uuid4(),
        email=f"s{uuid.uuid4().hex[:6]}@example.test",
        display_name="Student",
        role=UserRole.student,
        tenant_id=DEV,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    db.add(u)
    db.flush()
    return u


def _enroll(db, user_id, course_id) -> Enrollment:
    e = Enrollment(id=uuid.uuid4(), user_id=user_id, course_id=course_id, tenant_id=DEV)
    db.add(e)
    db.flush()
    return e


# -- course_locale ----------------------------------------------------------


def test_locale_reads_the_course_meta_locale(db_session):
    assert course_locale(_course(db_session, meta={"locale": " de-DE "})) == "de-DE"


def test_locale_falls_back_to_the_language_and_lang_keys(db_session):
    assert course_locale(_course(db_session, meta={"language": "fr"})) == "fr"
    assert course_locale(_course(db_session, meta={"lang": "es"})) == "es"


def test_locale_prefers_the_first_key_in_order(db_session):
    course = _course(db_session, meta={"locale": "de", "language": "fr", "lang": "es"})
    assert course_locale(course) == "de"


def test_a_blank_or_non_string_locale_is_skipped(db_session):
    assert course_locale(_course(db_session, meta={"locale": "  ", "lang": "es"})) == "es"
    assert course_locale(_course(db_session, meta={"locale": 5})) is None


def test_no_locale_key_means_none(db_session):
    assert course_locale(_course(db_session, meta={"title": "x"})) is None
    assert course_locale(_course(db_session, meta=None)) is None


def test_a_missing_course_and_bad_meta_are_none(db_session):
    assert course_locale(None) is None
    bad = _course(db_session)
    bad.course_meta = "not json {"
    assert course_locale(bad) is None
    bad.course_meta = "[1, 2]"  # valid JSON, not an object
    assert course_locale(bad) is None


def test_the_locale_key_order_is_the_documented_one():
    assert LOCALE_KEYS == ("locale", "language", "lang")


# -- enrolment --------------------------------------------------------------


def test_enrolment_finds_the_student_course_pair(db_session):
    u = _student(db_session)
    c = _course(db_session)
    assert enrolment(db_session, u.id, c.id) is None
    row = _enroll(db_session, u.id, c.id)
    found = enrolment(db_session, u.id, c.id)
    assert found is not None and found.id == row.id


def test_enrolment_of_another_course_is_none(db_session):
    u = _student(db_session)
    _enroll(db_session, u.id, _course(db_session).id)
    assert enrolment(db_session, u.id, _course(db_session).id) is None


# -- quiz_context -----------------------------------------------------------


def test_a_standalone_quiz_carries_the_attempt(db_session):
    ctx = quiz_context(db_session, _quiz(db_session), uuid.uuid4(), uuid.uuid4())
    assert isinstance(ctx, StatementContext)
    assert ctx.language is None


def test_an_enrolled_student_gets_the_enrolment_as_registration(db_session):
    u = _student(db_session)
    c = _course(db_session, meta={"locale": "de-DE"})
    q = _quiz(db_session, c)
    row = _enroll(db_session, u.id, c.id)
    attempt = uuid.uuid4()
    ctx = quiz_context(db_session, q, u.id, attempt)
    assert ctx.registration == row.id and ctx.registration != attempt
    assert ctx.language == "de-DE"


def test_an_unenrolled_student_gets_the_attempt_as_registration(db_session):
    u = _student(db_session)
    c = _course(db_session)
    q = _quiz(db_session, c)
    attempt = uuid.uuid4()
    ctx = quiz_context(db_session, q, u.id, attempt)
    assert ctx.registration == attempt
    assert ctx.language is None  # the platform default applies


def test_a_quiz_whose_module_is_gone_carries_the_attempt(db_session):
    q = _quiz(db_session)
    q.module_id = uuid.uuid4()  # a module that does not exist
    attempt = uuid.uuid4()
    ctx = quiz_context(db_session, q, uuid.uuid4(), attempt)
    assert ctx.registration == attempt and ctx.language is None
