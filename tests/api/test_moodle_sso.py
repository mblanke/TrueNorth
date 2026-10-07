"""Single sign-on from the TrueNorth app into the tenant's Moodle.

TrueNorth decides who may open a course; Moodle (``local_truenorth``'s ``sso.php``)
only checks the ticket. So everything Moodle trusts is decided here:

- a Student needs an active enrolment in that course, and staff enter as teachers;
- a course in another tenant does not exist as far as the caller can tell (404);
- the ticket is addressed to the caller's own tenant's Moodle and to no other;
- it is short-lived, single-use (unique jti) and signed with the LTI tool key.

Brought over from the Moodle farm WIP (branch claude/moodle-integration-courses-8bc905);
the claims match what ``infra/platform/moodle/local_truenorth/classes/ticket.php`` and
``classes/sso.php`` verify on this branch.
"""

import uuid
from contextlib import contextmanager

import jwt
import pytest
from app import lti13, moodle_sso
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import (
    Course,
    Enrollment,
    EnrollmentStatus,
    ExternalPlatform,
    IntegrationAuthType,
    User,
    UserRole,
)

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"
MOODLE = "http://moodle.dev.test"


@contextmanager
def acting_as(person: User):
    who = CurrentUser(
        id=str(person.id),
        email=person.email,
        display_name=person.display_name,
        role=person.role,
        tenant_id=str(person.tenant_id),
        keycloak_id=person.keycloak_id,
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _user(db, tenant=DEV_TENANT, role=UserRole.student, first="Pat", last="Student") -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        display_name="Pat Student",
        first_name=first,
        last_name=last,
        role=role,
        tenant_id=uuid.UUID(tenant),
    )
    db.add(u)
    db.flush()
    return u


def _moodle(db, tenant=DEV_TENANT, issuer=MOODLE, active=True) -> ExternalPlatform:
    p = ExternalPlatform(
        id=uuid.uuid4(),
        name="Moodle",
        slug=f"moodle-{uuid.uuid4().hex[:6]}",
        platform_type="moodle",
        base_url="http://moodle:8080",
        auth_type=IntegrationAuthType.lti13,
        tenant_id=uuid.UUID(tenant),
        lti_issuer=issuer,
        is_active=active,
    )
    db.add(p)
    db.flush()
    return p


def _course(db, tenant=DEV_TENANT) -> Course:
    c = Course(id=uuid.uuid4(), name="C101", tenant_id=uuid.UUID(tenant) if tenant else None)
    db.add(c)
    db.flush()
    return c


def _enrol(db, person: User, course: Course, status=EnrollmentStatus.enrolled) -> None:
    db.add(
        Enrollment(id=uuid.uuid4(), user_id=person.id, course_id=course.id, tenant_id=person.tenant_id, status=status)
    )
    db.flush()


def _claims(db, token: str) -> dict:
    key = lti13.get_tool_key(db)
    return jwt.decode(token, key.public_key_pem, algorithms=["RS256"], audience=MOODLE)


class TestWhoMayEnter:
    def test_an_enrolled_student_gets_a_ticket_for_their_course(self, client, db_session):
        _moodle(db_session)
        student, course = _user(db_session), _course(db_session)
        _enrol(db_session, student, course)
        with acting_as(student):
            resp = client.post("/integrations/moodle/sso", json={"course_id": str(course.id)})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["action"] == f"{MOODLE}/local/truenorth/sso.php"
        claims = _claims(db_session, body["token"])
        assert claims["sub"] == str(student.id)
        assert claims["course"] == str(course.id)  # the live Moodle course's idnumber
        assert claims["role"] == "student"
        assert (claims["given_name"], claims["family_name"]) == ("Pat", "Student")

    @pytest.mark.parametrize("status", [EnrollmentStatus.in_progress, EnrollmentStatus.completed])
    def test_in_progress_and_completed_students_may_enter(self, client, db_session, status):
        _moodle(db_session)
        student, course = _user(db_session), _course(db_session)
        _enrol(db_session, student, course, status)
        with acting_as(student):
            resp = client.post("/integrations/moodle/sso", json={"course_id": str(course.id)})
        assert resp.status_code == 200

    def test_a_student_not_on_the_course_is_refused(self, client, db_session):
        _moodle(db_session)
        student, course = _user(db_session), _course(db_session)
        with acting_as(student):
            resp = client.post("/integrations/moodle/sso", json={"course_id": str(course.id)})
        assert resp.status_code == 403

    @pytest.mark.parametrize("status", [EnrollmentStatus.withdrawn, EnrollmentStatus.failed])
    def test_a_withdrawn_or_failed_student_is_refused(self, client, db_session, status):
        _moodle(db_session)
        student, course = _user(db_session), _course(db_session)
        _enrol(db_session, student, course, status)
        with acting_as(student):
            resp = client.post("/integrations/moodle/sso", json={"course_id": str(course.id)})
        assert resp.status_code == 403

    def test_a_course_in_another_tenant_is_not_found(self, client, db_session):
        _moodle(db_session)
        student, foreign = _user(db_session), _course(db_session, OTHER_TENANT)
        _enrol(db_session, student, foreign)
        with acting_as(student):
            resp = client.post("/integrations/moodle/sso", json={"course_id": str(foreign.id)})
        assert resp.status_code == 404

    def test_an_unknown_course_is_not_found(self, client, db_session):
        _moodle(db_session)
        with acting_as(_user(db_session, role=UserRole.instructor)):
            resp = client.post("/integrations/moodle/sso", json={"course_id": str(uuid.uuid4())})
        assert resp.status_code == 404

    @pytest.mark.parametrize("role", [UserRole.instructor, UserRole.admin])
    def test_staff_enter_any_tenant_course_as_teachers(self, client, db_session, role):
        _moodle(db_session)
        staff, course = _user(db_session, role=role), _course(db_session)
        with acting_as(staff):
            resp = client.post("/integrations/moodle/sso", json={"course_id": str(course.id)})
        assert resp.status_code == 200, resp.text
        assert _claims(db_session, resp.json()["token"])["role"] == "teacher"

    def test_without_a_course_a_student_enters_as_a_student(self, client, db_session):
        _moodle(db_session)
        with acting_as(_user(db_session)):
            claims = _claims(db_session, client.post("/integrations/moodle/sso", json={}).json()["token"])
        assert claims["role"] == "student" and "course" not in claims


class TestTheTicket:
    def test_it_is_addressed_to_the_callers_own_tenants_moodle(self, client, db_session):
        _moodle(db_session, OTHER_TENANT, issuer="http://moodle.other.test")
        _moodle(db_session, issuer=MOODLE + "/")
        student = _user(db_session)
        with acting_as(student):
            body = client.post("/integrations/moodle/sso", json={}).json()
        assert body["action"] == f"{MOODLE}/local/truenorth/sso.php"  # trailing slash dropped
        _claims(db_session, body["token"])  # audience must be this tenant's Moodle

    def test_no_moodle_for_the_tenant_is_not_found(self, client, db_session):
        _moodle(db_session, OTHER_TENANT)
        _moodle(db_session, issuer="http://inactive.test", active=False)
        student = _user(db_session)
        with acting_as(student):
            resp = client.post("/integrations/moodle/sso", json={})
        assert resp.status_code == 404

    def test_a_moodle_without_an_issuer_is_not_usable(self, client, db_session):
        _moodle(db_session, issuer=None)
        with acting_as(_user(db_session)):
            assert client.post("/integrations/moodle/sso", json={}).status_code == 404

    def test_it_expires_within_a_minute_and_is_never_reused(self, client, db_session):
        _moodle(db_session)
        student = _user(db_session)
        with acting_as(student):
            first = _claims(db_session, client.post("/integrations/moodle/sso", json={}).json()["token"])
            second = _claims(db_session, client.post("/integrations/moodle/sso", json={}).json()["token"])
        assert first["exp"] - first["iat"] == moodle_sso.TICKET_SECONDS <= 60
        assert first["jti"] != second["jti"]
        assert first["iss"] == "truenorth"
        assert first["typ"] == "sso"  # never accepted by the plugin as a sync call
        assert "tid" not in first

    def test_names_fall_back_to_the_display_name(self, client, db_session):
        _moodle(db_session)
        person = _user(db_session, first=None, last=None)
        with acting_as(person):
            claims = _claims(db_session, client.post("/integrations/moodle/sso", json={}).json()["token"])
        assert (claims["given_name"], claims["family_name"]) == ("Pat", "Student")

    def test_bad_course_id_is_422(self, client, db_session):
        _moodle(db_session)
        with acting_as(_user(db_session)):
            assert client.post("/integrations/moodle/sso", json={"course_id": "nope"}).status_code == 422
