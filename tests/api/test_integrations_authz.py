"""Authorization on the endpoints an LMS integration (Moodle over LTI 1.3) relies on.

Before this, every one of these routes required only a logged-in user:

- any user could register an external platform, or repoint an existing one's JWKS URL —
  the key an LTI launch is verified against, i.e. who may sign people in;
- external activities and transcripts were readable, and writable, for any user id,
  with no tenant predicate at all;
- ``POST /lti/grades`` let anyone post any score to anyone's external gradebook.

Each test here is a negative: the wrong role, or the wrong tenant, must not get through.
Tenant mismatches are 404 (``app.tenancy`` never confirms a foreign id exists); a caller
missing a permission they could see in their own role is 403.
"""

import uuid
from contextlib import contextmanager

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import (
    Course,
    Enrollment,
    Exercise,
    ExternalActivity,
    ExternalPlatform,
    IntegrationAuthType,
    User,
    UserRole,
)

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


@contextmanager
def acting_as(role: UserRole, *, user_id: uuid.UUID | None = None, tenant: str = DEV_TENANT):
    """Swap the caller identity for the duration of the block."""
    who = CurrentUser(
        id=str(user_id or uuid.uuid4()),
        email=f"{role.value}-{uuid.uuid4().hex[:6]}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=tenant,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _user(db, tenant: str, role: UserRole = UserRole.student) -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        display_name="learner",
        role=role,
        tenant_id=uuid.UUID(tenant),
    )
    db.add(u)
    db.flush()
    return u


def _platform(db, tenant: str) -> ExternalPlatform:
    p = ExternalPlatform(
        id=uuid.uuid4(),
        name="Moodle",
        slug=f"moodle-{uuid.uuid4().hex[:6]}",
        platform_type="moodle",
        base_url="http://moodle.test",
        auth_type=IntegrationAuthType.lti13,
        tenant_id=uuid.UUID(tenant),
        lti_jwks_url="http://moodle.test/mod/lti/certs.php",
    )
    db.add(p)
    db.flush()
    return p


def _activity(db, platform: ExternalPlatform, user: User, title: str) -> ExternalActivity:
    a = ExternalActivity(
        id=uuid.uuid4(),
        platform_id=platform.id,
        user_id=user.id,
        external_ref=uuid.uuid4().hex,
        activity_type="course",
        title=title,
    )
    db.add(a)
    db.flush()
    return a


PLATFORM_BODY = {
    "name": "Moodle",
    "slug": "moodle",
    "platform_type": "moodle",
    "base_url": "http://moodle.test",
    "auth_type": "lti13",
    "lti_issuer": "http://moodle.test",
    "lti_client_id": "client-1",
    "lti_deployment_id": "1",
    "lti_jwks_url": "http://moodle.test/mod/lti/certs.php",
    "lti_token_url": "http://moodle.test/mod/lti/token.php",
    "lti_auth_login_url": "http://moodle.test/mod/lti/auth.php",
}


# ── Platform registration ────────────────────────────────────────────────────


class TestPlatformAdministration:
    @pytest.mark.parametrize("role", [UserRole.student, UserRole.instructor, UserRole.observer])
    def test_only_admins_register_platforms(self, client, role):
        with acting_as(role):
            resp = client.post("/integrations/platforms", json=PLATFORM_BODY)
        assert resp.status_code == 403, resp.text

    def test_a_student_cannot_repoint_the_jwks_url(self, client, db_session):
        platform = _platform(db_session, DEV_TENANT)
        db_session.commit()
        with acting_as(UserRole.student):
            resp = client.patch(
                f"/integrations/platforms/{platform.id}",
                json={"lti_jwks_url": "http://attacker.test/keys"},
            )
        assert resp.status_code == 403
        db_session.refresh(platform)
        assert platform.lti_jwks_url == "http://moodle.test/mod/lti/certs.php"

    @pytest.mark.parametrize("method", ["delete", "post"])
    def test_a_student_cannot_delete_or_probe_a_platform(self, client, db_session, method):
        platform = _platform(db_session, DEV_TENANT)
        db_session.commit()
        url = f"/integrations/platforms/{platform.id}" + ("/test" if method == "post" else "")
        with acting_as(UserRole.student):
            resp = getattr(client, method)(url)
        assert resp.status_code == 403

    def test_instructors_may_read_but_not_write(self, client):
        with acting_as(UserRole.instructor):
            assert client.get("/integrations/platforms").status_code == 200
            assert client.post("/integrations/platforms", json=PLATFORM_BODY).status_code == 403

    def test_the_auth_login_url_is_accepted_and_returned(self, client):
        """Without it, lti13.build_login_redirect refuses every Moodle launch."""
        resp = client.post("/integrations/platforms", json=PLATFORM_BODY)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["lti_auth_login_url"] == "http://moodle.test/mod/lti/auth.php"

        patched = client.patch(
            f"/integrations/platforms/{body['id']}",
            json={"lti_auth_login_url": "http://moodle.test/other/auth.php"},
        )
        assert patched.status_code == 200
        assert patched.json()["lti_auth_login_url"] == "http://moodle.test/other/auth.php"

    def test_update_rejects_an_unknown_auth_type(self, client, db_session):
        platform = _platform(db_session, DEV_TENANT)
        db_session.commit()
        resp = client.patch(f"/integrations/platforms/{platform.id}", json={"auth_type": "nonsense"})
        assert resp.status_code == 422


# ── External activities ──────────────────────────────────────────────────────


@pytest.fixture
def activities(db_session):
    """Three learners' activities: two in our tenant, one in a foreign one."""
    ours = _platform(db_session, DEV_TENANT)
    theirs = _platform(db_session, OTHER_TENANT)
    me = _user(db_session, DEV_TENANT)
    classmate = _user(db_session, DEV_TENANT)
    stranger = _user(db_session, OTHER_TENANT)
    _activity(db_session, ours, me, "mine")
    _activity(db_session, ours, classmate, "classmate")
    _activity(db_session, theirs, stranger, "foreign")
    db_session.commit()
    return {"ours": ours, "theirs": theirs, "me": me, "classmate": classmate, "stranger": stranger}


def _titles(resp) -> set[str]:
    assert resp.status_code == 200, resp.text
    return {a["title"] for a in resp.json()["items"]}


class TestExternalActivities:
    def test_a_learner_sees_only_their_own(self, client, activities):
        with acting_as(UserRole.student, user_id=activities["me"].id):
            assert _titles(client.get("/integrations/activities")) == {"mine"}

    def test_a_learner_cannot_ask_for_someone_else(self, client, activities):
        with acting_as(UserRole.student, user_id=activities["me"].id):
            resp = client.get("/integrations/activities", params={"user_id": str(activities["classmate"].id)})
        assert resp.status_code == 403

    def test_staff_see_their_tenant_and_never_another(self, client, activities):
        with acting_as(UserRole.instructor):
            titles = _titles(client.get("/integrations/activities"))
        assert titles == {"mine", "classmate"}

    def test_asking_for_a_foreign_learner_returns_nothing(self, client, activities):
        resp = client.get("/integrations/activities", params={"user_id": str(activities["stranger"].id)})
        assert _titles(resp) == set()

    def _record(self, client, platform_id, user_id):
        return client.post(
            "/integrations/activities",
            params={
                "platform_id": str(platform_id),
                "user_id": str(user_id),
                "external_ref": "ref-1",
                "activity_type": "course",
                "title": "Self-credited",
            },
        )

    def test_a_learner_cannot_credit_themselves(self, client, activities):
        with acting_as(UserRole.student, user_id=activities["me"].id):
            resp = self._record(client, activities["ours"].id, activities["me"].id)
        assert resp.status_code == 403

    def test_recording_against_a_foreign_learner_is_not_found(self, client, activities):
        assert self._record(client, activities["ours"].id, activities["stranger"].id).status_code == 404

    def test_recording_through_a_foreign_platform_is_not_found(self, client, activities):
        assert self._record(client, activities["theirs"].id, activities["me"].id).status_code == 404

    def test_staff_can_record_within_their_tenant(self, client, activities):
        with acting_as(UserRole.instructor):
            resp = self._record(client, activities["ours"].id, activities["classmate"].id)
        assert resp.status_code == 201, resp.text


# ── LTI grade passback ───────────────────────────────────────────────────────


GRADE = {"resource_kind": "quiz", "resource_id": str(uuid.uuid4()), "score": 100, "max_score": 100}


class TestGradePassback:
    def test_a_learner_cannot_post_a_grade(self, client, db_session):
        me = _user(db_session, DEV_TENANT)
        db_session.commit()
        with acting_as(UserRole.student, user_id=me.id):
            resp = client.post("/lti/grades", json={**GRADE, "user_id": str(me.id)})
        assert resp.status_code == 403

    def test_grades_for_a_foreign_learner_are_not_found(self, client, db_session):
        stranger = _user(db_session, OTHER_TENANT)
        db_session.commit()
        resp = client.post("/lti/grades", json={**GRADE, "user_id": str(stranger.id)})
        assert resp.status_code == 404
        assert "User not found" in resp.text


# ── Enrolments, progress, transcripts ────────────────────────────────────────


@pytest.fixture
def roster(db_session):
    """A global catalogue course with learners from two tenants on it."""
    course = Course(id=uuid.uuid4(), name="GLOBAL — Standard programme", tenant_id=None, is_published=True)
    db_session.add(course)
    me = _user(db_session, DEV_TENANT)
    classmate = _user(db_session, DEV_TENANT)
    stranger = _user(db_session, OTHER_TENANT)
    for u in (me, classmate, stranger):
        db_session.add(Enrollment(id=uuid.uuid4(), user_id=u.id, course_id=course.id))
    db_session.commit()
    return {"course": course, "me": me, "classmate": classmate, "stranger": stranger}


class TestEnrollmentRecords:
    def test_a_learner_cannot_list_a_course_roster(self, client, roster):
        with acting_as(UserRole.student, user_id=roster["me"].id):
            resp = client.get(f"/courses/{roster['course'].id}/enrollments")
        assert resp.status_code == 403

    def test_a_global_course_roster_shows_only_this_tenant(self, client, roster):
        with acting_as(UserRole.instructor):
            resp = client.get(f"/courses/{roster['course'].id}/enrollments")
        assert resp.status_code == 200, resp.text
        seen = {e["user_id"] for e in resp.json()}
        assert seen == {str(roster["me"].id), str(roster["classmate"].id)}

    def test_a_learner_cannot_read_a_classmates_progress(self, client, roster):
        with acting_as(UserRole.student, user_id=roster["me"].id):
            resp = client.get(f"/courses/{roster['course'].id}/progress/{roster['classmate'].id}")
        assert resp.status_code == 403

    def test_a_learner_can_read_their_own_progress(self, client, roster):
        with acting_as(UserRole.student, user_id=roster["me"].id):
            resp = client.get(f"/courses/{roster['course'].id}/progress/{roster['me'].id}")
        assert resp.status_code == 200

    def test_staff_cannot_read_a_foreign_learners_progress(self, client, roster):
        resp = client.get(f"/courses/{roster['course'].id}/progress/{roster['stranger'].id}")
        assert resp.status_code == 404

    def test_a_learner_cannot_complete_a_classmates_course(self, client, roster):
        with acting_as(UserRole.student, user_id=roster["me"].id):
            resp = client.post(f"/courses/{roster['course'].id}/complete/{roster['classmate'].id}")
        assert resp.status_code == 403

    def test_staff_cannot_complete_a_foreign_learners_course(self, client, roster):
        resp = client.post(f"/courses/{roster['course'].id}/complete/{roster['stranger'].id}")
        assert resp.status_code == 404

    def test_staff_cannot_enrol_a_foreign_learner(self, client, db_session):
        stranger = _user(db_session, OTHER_TENANT)
        course = Course(id=uuid.uuid4(), name="Ours", tenant_id=uuid.UUID(DEV_TENANT))
        db_session.add(course)
        db_session.commit()
        resp = client.post(
            f"/courses/{course.id}/enroll",
            json={"user_id": str(stranger.id), "course_id": str(course.id)},
        )
        assert resp.status_code == 404
        assert "User not found" in resp.text


class TestTranscript:
    def test_a_learner_cannot_read_a_classmates_transcript(self, client, roster):
        with acting_as(UserRole.student, user_id=roster["me"].id):
            resp = client.get(f"/users/{roster['classmate'].id}/transcript")
        assert resp.status_code == 403

    def test_staff_cannot_read_a_foreign_transcript(self, client, roster):
        assert client.get(f"/users/{roster['stranger'].id}/transcript").status_code == 404

    def test_a_global_course_appears_instead_of_breaking_the_transcript(self, client, roster):
        """get_owned 404'd on NULL-tenant courses and took the whole transcript down."""
        with acting_as(UserRole.student, user_id=roster["me"].id):
            resp = client.get(f"/users/{roster['me'].id}/transcript")
        assert resp.status_code == 200, resp.text
        titles = {e["title"] for e in resp.json()["entries"]}
        assert "GLOBAL — Standard programme" in titles

    def test_other_peoples_exercises_are_not_on_my_transcript(self, client, db_session, roster):
        """Exercise has no learner column; the tenant's runs are nobody's in particular."""
        db_session.add(
            Exercise(
                id=uuid.uuid4(),
                name="Someone else's assessment",
                range_id=uuid.uuid4(),
                tenant_id=uuid.UUID(DEV_TENANT),
                total_score=12,
                max_score=100,
            )
        )
        db_session.commit()
        with acting_as(UserRole.student, user_id=roster["me"].id):
            resp = client.get(f"/users/{roster['me'].id}/transcript")
        titles = {e["title"] for e in resp.json()["entries"]}
        assert "Someone else's assessment" not in titles
