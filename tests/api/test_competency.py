"""Competency framework, profiles, skill gaps, assertions, certifications, heatmap.

``app.routers.competency`` had no tests and no authorization beyond "signed in": any
user could read anyone's profile across tenants, assert their own proficiency, and add
to the platform-wide catalogue; a bad enum value or a duplicate code was a 500.
These pin the rules now in its module docstring, plus the error paths.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import (
    Certification,
    Competency,
    CompetencyAssertion,
    CompetencyFramework,
    ProficiencyLevel,
    Tenant,
    User,
    UserRole,
)

TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c001")
OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c0ff")


@pytest.fixture
def tenants(db_session):
    for tid in (TENANT, OTHER_TENANT):
        if db_session.get(Tenant, tid) is None:
            db_session.add(Tenant(id=tid, name=f"comp-{tid.hex[-4:]}", slug=f"comp-{tid.hex[-4:]}"))
    db_session.flush()


def _user(db, role: UserRole = UserRole.student, tenant: uuid.UUID = TENANT) -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=tenant,
    )
    db.add(u)
    db.flush()
    return u


@contextmanager
def acting_as(user: User):
    who = CurrentUser(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        tenant_id=str(user.tenant_id),
        keycloak_id=user.keycloak_id,
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _comp(db, code: str, category: str = "Protect & Defend", framework=CompetencyFramework.nice) -> Competency:
    c = Competency(code=code, name=f"Competency {code}", framework=framework, category=category)
    db.add(c)
    db.flush()
    return c


def _assert(db, user: User, comp: Competency, level=ProficiencyLevel.intermediate) -> CompetencyAssertion:
    a = CompetencyAssertion(user_id=user.id, competency_id=comp.id, proficiency=level)
    db.add(a)
    db.flush()
    return a


def _code() -> str:
    return f"T-{uuid.uuid4().hex[:8]}"


# ── catalogue ───────────────────────────────────────────────────────────


def test_anyone_signed_in_can_list_and_filter(client, db_session, tenants):
    nice = _comp(db_session, _code(), category="Analyze")
    attack = _comp(db_session, _code(), framework=CompetencyFramework.mitre_attack)
    with acting_as(_user(db_session)):
        all_codes = {c["code"] for c in client.get("/competency/frameworks", params={"limit": 500}).json()}
        nice_codes = {
            c["code"] for c in client.get("/competency/frameworks", params={"framework": "nice", "limit": 500}).json()
        }
        analyze = client.get("/competency/frameworks", params={"category": "Analyze", "limit": 500}).json()
    assert {nice.code, attack.code} <= all_codes
    assert nice.code in nice_codes and attack.code not in nice_codes
    assert all(c["category"] == "Analyze" for c in analyze)


def test_list_limit_is_bounded(client, db_session, tenants):
    with acting_as(_user(db_session)):
        assert client.get("/competency/frameworks", params={"limit": 501}).status_code == 422
        assert client.get("/competency/frameworks", params={"offset": -1}).status_code == 422


def test_student_cannot_add_to_the_catalogue(client, db_session, tenants):
    with acting_as(_user(db_session)):
        r = client.post("/competency/frameworks", params={"code": _code(), "name": "x", "framework": "nice"})
        assert r.status_code == 403
        assert client.post("/competency/frameworks/import-nice").status_code == 403


def test_instructor_creates_a_competency(client, db_session, tenants):
    code = _code()
    with acting_as(_user(db_session, UserRole.instructor)):
        r = client.post(
            "/competency/frameworks",
            params={"code": code, "name": "Packet analysis", "framework": "custom", "category": "Analyze"},
        )
    assert r.status_code == 201, r.text
    assert r.json()["code"] == code and r.json()["framework"] == "custom"


def test_unknown_framework_is_422_not_500(client, db_session, tenants):
    with acting_as(_user(db_session, UserRole.instructor)):
        r = client.post("/competency/frameworks", params={"code": _code(), "name": "x", "framework": "cobit"})
    assert r.status_code == 422
    assert "framework" in r.json()["detail"]


def test_duplicate_code_in_a_framework_is_409(client, db_session, tenants):
    code = _code()
    _comp(db_session, code)
    with acting_as(_user(db_session, UserRole.instructor)):
        r = client.post("/competency/frameworks", params={"code": code, "name": "again", "framework": "nice"})
    assert r.status_code == 409


def test_import_nice_is_idempotent(client, db_session, tenants):
    with acting_as(_user(db_session, UserRole.admin)):
        first = client.post("/competency/frameworks/import-nice").json()
        second = client.post("/competency/frameworks/import-nice").json()
    assert first["total_nice_roles"] == second["total_nice_roles"] == 18
    assert second["imported"] == 0
    assert db_session.query(Competency).filter(Competency.code == "PR-CDA-001").count() == 1


# ── profile ─────────────────────────────────────────────────────────────


def test_student_reads_own_profile(client, db_session, tenants):
    student = _user(db_session)
    comp = _comp(db_session, _code())
    _assert(db_session, student, comp, ProficiencyLevel.advanced)
    with acting_as(student):
        r = client.get(f"/competency/users/{student.id}/profile")
    assert r.status_code == 200
    body = r.json()
    assert body["total_competencies"] == 1
    assert body["by_framework"] == {"nice": 1}
    assert body["by_proficiency"] == {"advanced": 1}


def test_student_cannot_read_another_students_profile(client, db_session, tenants):
    other = _user(db_session)
    with acting_as(_user(db_session)):
        assert client.get(f"/competency/users/{other.id}/profile").status_code == 403


def test_instructor_reads_a_profile_in_their_tenant_only(client, db_session, tenants):
    mine = _user(db_session)
    foreign = _user(db_session, tenant=OTHER_TENANT)
    with acting_as(_user(db_session, UserRole.instructor)):
        assert client.get(f"/competency/users/{mine.id}/profile").status_code == 200
        assert client.get(f"/competency/users/{foreign.id}/profile").status_code == 404
        assert client.get(f"/competency/users/{uuid.uuid4()}/profile").status_code == 404


def test_profile_with_no_assertions_is_empty(client, db_session, tenants):
    student = _user(db_session)
    with acting_as(student):
        body = client.get(f"/competency/users/{student.id}/profile").json()
    assert body["total_competencies"] == 0 and body["assertions"] == []


# ── skill gaps ──────────────────────────────────────────────────────────


def test_skill_gaps(client, db_session, tenants):
    category = f"Cat-{uuid.uuid4().hex[:6]}"
    role = _comp(db_session, _code(), category=category)
    met = _comp(db_session, _code(), category=category)
    weak = _comp(db_session, _code(), category=category)
    missing = _comp(db_session, _code(), category=category)
    _comp(db_session, _code(), category="Unrelated")
    student = _user(db_session)
    _assert(db_session, student, role, ProficiencyLevel.expert)
    _assert(db_session, student, met, ProficiencyLevel.intermediate)
    _assert(db_session, student, weak, ProficiencyLevel.beginner)

    with acting_as(student):
        r = client.get(f"/competency/users/{student.id}/skill-gaps", params={"target_role": role.code})
    assert r.status_code == 200
    gaps = {g["competency"]["code"]: g["current_level"] for g in r.json()}
    assert gaps == {weak.code: "beginner", missing.code: None}


def test_skill_gaps_errors(client, db_session, tenants):
    student = _user(db_session)
    other = _user(db_session)
    with acting_as(student):
        assert client.get(f"/competency/users/{student.id}/skill-gaps").status_code == 422  # target_role required
        r = client.get(f"/competency/users/{student.id}/skill-gaps", params={"target_role": "NOPE-000"})
        assert r.status_code == 404
        r = client.get(f"/competency/users/{other.id}/skill-gaps", params={"target_role": "NOPE-000"})
        assert r.status_code == 403


# ── assertions ──────────────────────────────────────────────────────────


def test_student_cannot_assert_their_own_proficiency(client, db_session, tenants):
    student = _user(db_session)
    comp = _comp(db_session, _code())
    with acting_as(student):
        r = client.post(
            f"/competency/users/{student.id}/assertions",
            params={"competency_id": str(comp.id), "proficiency": "expert"},
        )
    assert r.status_code == 403
    assert db_session.query(CompetencyAssertion).filter(CompetencyAssertion.user_id == student.id).count() == 0


def test_instructor_asserts_for_a_student(client, db_session, tenants):
    student = _user(db_session)
    comp = _comp(db_session, _code())
    with acting_as(_user(db_session, UserRole.instructor)):
        r = client.post(
            f"/competency/users/{student.id}/assertions",
            params={"competency_id": str(comp.id), "proficiency": "advanced", "source": "moodle"},
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user_id"] == str(student.id) and body["proficiency"] == "advanced"


def test_assertion_error_paths(client, db_session, tenants):
    student = _user(db_session)
    foreign = _user(db_session, tenant=OTHER_TENANT)
    comp = _comp(db_session, _code())
    with acting_as(_user(db_session, UserRole.instructor)):
        url = f"/competency/users/{student.id}/assertions"
        bad_level = client.post(url, params={"competency_id": str(comp.id), "proficiency": "godlike"})
        unknown = client.post(url, params={"competency_id": str(uuid.uuid4())})
        not_uuid = client.post(url, params={"competency_id": "abc"})
        cross_tenant = client.post(
            f"/competency/users/{foreign.id}/assertions", params={"competency_id": str(comp.id)}
        )
    assert bad_level.status_code == 422 and "proficiency" in bad_level.json()["detail"]
    assert unknown.status_code == 404
    assert not_uuid.status_code == 422
    assert cross_tenant.status_code == 404


# ── certifications ──────────────────────────────────────────────────────


CERT = {"cert_name": "Security+", "issuer": "CompTIA", "nice_work_roles": ["PR-CDA-001"]}


def test_student_adds_and_lists_own_certification(client, db_session, tenants):
    student = _user(db_session)
    with acting_as(student):
        created = client.post(f"/certifications/users/{student.id}", json=CERT)
        listed = client.get(f"/certifications/users/{student.id}")
    assert created.status_code == 201, created.text
    assert [c["cert_name"] for c in listed.json()] == ["Security+"]


def test_certifications_are_personal_records(client, db_session, tenants):
    student = _user(db_session)
    other = _user(db_session)
    foreign = _user(db_session, tenant=OTHER_TENANT)
    db_session.add(Certification(user_id=other.id, cert_name="OSCP", issuer="OffSec"))
    db_session.flush()
    with acting_as(student):
        assert client.get(f"/certifications/users/{other.id}").status_code == 403
        assert client.post(f"/certifications/users/{other.id}", json=CERT).status_code == 403
    with acting_as(_user(db_session, UserRole.instructor)):
        assert [c["cert_name"] for c in client.get(f"/certifications/users/{other.id}").json()] == ["OSCP"]
        assert client.get(f"/certifications/users/{foreign.id}").status_code == 404
        assert client.post(f"/certifications/users/{foreign.id}", json=CERT).status_code == 404


def test_certification_validation(client, db_session, tenants):
    student = _user(db_session)
    with acting_as(student):
        assert client.post(f"/certifications/users/{student.id}", json={"issuer": "x"}).status_code == 422
        assert (
            client.post(f"/certifications/users/{student.id}", json={"cert_name": "", "issuer": "x"}).status_code
            == 422
        )


# ── heatmap ─────────────────────────────────────────────────────────────


def _cell(body: dict, category: str) -> list[int]:
    y = body["categories"].index(category)
    return [v[2] for v in body["values"] if v[1] == y]


def test_heatmap_scores_nice_assertions(client, db_session, tenants):
    student = _user(db_session)
    comp = _comp(db_session, _code(), category="Investigate")
    _assert(db_session, student, comp, ProficiencyLevel.advanced)
    with acting_as(student):
        body = client.get("/competency/heatmap", params={"view": "individual"}).json()
    assert set(_cell(body, "Investigate")) == {75}
    assert "Investigate" in body["work_roles"]  # real categories, not the placeholder list


def test_heatmap_team_view_is_tenant_scoped(client, db_session, tenants):
    category = "Securely Provision"
    comp = _comp(db_session, _code(), category=category)
    _assert(db_session, _user(db_session, tenant=OTHER_TENANT), comp, ProficiencyLevel.expert)
    mine = _user(db_session)
    _assert(db_session, mine, comp, ProficiencyLevel.novice)
    with acting_as(_user(db_session, UserRole.instructor)):
        body = client.get("/competency/heatmap", params={"view": "team"}).json()
    assert set(_cell(body, category)) == {10}  # the other tenant's expert is not averaged in


def test_heatmap_individual_view_only_counts_the_caller(client, db_session, tenants):
    comp = _comp(db_session, _code(), category="Analyze")
    _assert(db_session, _user(db_session), comp, ProficiencyLevel.expert)
    with acting_as(_user(db_session)):
        body = client.get("/competency/heatmap", params={"view": "individual"}).json()
    assert set(_cell(body, "Analyze")) == {0}
