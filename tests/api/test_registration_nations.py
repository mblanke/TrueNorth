"""``GET /registration/nations``: the Nation list an applicant may read.

Live on rc12 the request-access form's Nation dropdown was empty: it called
``/directory/nations``, which needs ``user:read``, and an applicant (a valid AD token, no
``users`` row) got 403. The fix is a narrow read in the registration router, authenticated
by token alone like ``POST /registration``. ``/directory/*`` keeps its permission.

The real auth path runs here: ``AUTH_DISABLED`` is off and only ``get_token_identity`` is
replaced by a crafted token (as test_mfa_policy does), so "applicant" really means no row.
"""

from __future__ import annotations

import uuid

import pytest
from _shared import act_as, real_user
from app import auth
from app.auth import TokenPayload, get_token_identity
from app.main import app as fastapi_app
from app.models import Nation, UserRole

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
PUBLIC_FIELDS = {"id", "name", "iso_alpha2", "iso_alpha3", "flag_emoji"}


@pytest.fixture
def real_auth(monkeypatch, client):
    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    yield client
    fastapi_app.dependency_overrides.pop(get_token_identity, None)


@pytest.fixture
def nations(db_session):
    """Two active nations and one retired one, with codes no seed uses."""
    rows = [
        Nation(id=uuid.uuid4(), name="Zz Testland", iso_alpha2="Q1", iso_alpha3="QQA", flag_emoji="F1", is_nato=True),
        Nation(id=uuid.uuid4(), name="Aa Testland", iso_alpha2="Q2", iso_alpha3="QQB", flag_emoji="F2", is_fvey=True),
        Nation(id=uuid.uuid4(), name="Mm Retired", iso_alpha2="Q3", iso_alpha3="QQC", flag_emoji="F3", is_active=False),
    ]
    db_session.add_all(rows)
    db_session.flush()
    return rows


def _as_applicant() -> None:
    """A valid AD token whose subject has no TrueNorth account."""
    ident = TokenPayload(sub=f"ad-{uuid.uuid4().hex[:8]}", email="applicant@corp.example",
                         groups=["TN-Trainees"])
    fastapi_app.dependency_overrides[get_token_identity] = lambda: ident


def test_applicant_gets_active_nations_only(real_auth, nations):
    _as_applicant()

    r = real_auth.get("/registration/nations")

    assert r.status_code == 200, r.text
    names = [n["name"] for n in r.json()]
    assert "Aa Testland" in names and "Zz Testland" in names
    assert "Mm Retired" not in names
    assert names == sorted(names)
    # Public fields only: no alliance flags, no is_active.
    assert all(set(n) == PUBLIC_FIELDS for n in r.json())
    aa = next(n for n in r.json() if n["name"] == "Aa Testland")
    assert aa == {"id": str(nations[1].id), "name": "Aa Testland", "iso_alpha2": "Q2",
                  "iso_alpha3": "QQB", "flag_emoji": "F2"}


def test_unauthenticated_is_401(real_auth, nations):
    r = real_auth.get("/registration/nations")

    assert r.status_code == 401


def test_applicant_is_still_refused_the_directory(real_auth, nations):
    """The fix adds a narrow read; it does not open /directory to people without user:read."""
    _as_applicant()

    assert real_auth.get("/directory/nations").status_code == 403
    assert real_auth.get(f"/directory/nations/{nations[0].id}").status_code == 403


@pytest.mark.parametrize(("role", "code"), [(UserRole.student, 403), (UserRole.instructor, 200), (UserRole.admin, 200)])
def test_directory_nations_permission_is_unchanged(client, db_session, nations, role, code):
    act_as(real_user(db_session, role, DEV_TENANT))

    assert client.get("/directory/nations").status_code == code


def test_a_registered_student_may_read_the_list(real_auth, db_session, nations):
    """Onboarding runs as a Student, who lacks user:read; it uses this route too."""
    student = real_user(db_session, UserRole.student, DEV_TENANT)
    ident = TokenPayload(sub=student.keycloak_id, email=student.email)
    fastapi_app.dependency_overrides[get_token_identity] = lambda: ident

    assert real_auth.get("/directory/nations").status_code == 403
    r = real_auth.get("/registration/nations")

    assert r.status_code == 200
    assert "Aa Testland" in [n["name"] for n in r.json()]
