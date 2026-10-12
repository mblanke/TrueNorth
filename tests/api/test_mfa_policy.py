"""MFA by role, enforced by the API on every request (v1.2.0, S2; F2 in the S-track spec).

Before v1.2.0 no route checked amr/acr: ``auth_zone_denial`` had one caller (registration).
Now ``auth.get_current_user`` refuses a privileged role (TN_MFA_ROLES: admin, instructor,
range_ops, observer) whose token shows no second factor, with 403 ``mfa_required`` and the
``X-TrueNorth-Auth-State: mfa_required`` header. The role is the database's.

Here the real ``get_current_user`` runs; only ``get_token_identity`` is replaced by a
crafted TokenPayload (as an IdP token would arrive after validation).
"""

from __future__ import annotations

import uuid

import pytest
from app import auth, mfa_policy
from app.auth import TokenPayload, get_token_identity, token_from_idp_claims
from app.main import app as fastapi_app
from app.models import AuditLog, User, UserRole

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
PRIVILEGED = [UserRole.admin, UserRole.instructor, UserRole.range_ops, UserRole.observer]


@pytest.fixture
def real_auth(monkeypatch, client):
    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    for name in ("TN_MFA_ROLES", "TN_MFA_AMR_VALUES", "TN_MFA_ACR_VALUES", "TN_MFA_DEMO_EXEMPT", "TN_TEST_HOST"):
        monkeypatch.delenv(name, raising=False)
    mfa_policy._demo_audited.clear()
    yield client
    fastapi_app.dependency_overrides.pop(get_token_identity, None)


def _user(db, role: UserRole, *, email: str | None = None, active: bool = True) -> User:
    uid = uuid.uuid4()
    row = User(id=uid, keycloak_id=f"kc-{uid}", email=email or f"{uid.hex[:10]}@example.test",
               display_name=role.value, role=role, tenant_id=DEV_TENANT, is_active=active)
    db.add(row)
    db.flush()
    return row


def _as(client, user: User, **claims):
    ident = TokenPayload(sub=user.keycloak_id, email=user.email, **claims)
    fastapi_app.dependency_overrides[get_token_identity] = lambda: ident
    return client.get("/users/me")


def _refused(r) -> bool:
    return (
        r.status_code == 403
        and isinstance(r.json().get("detail"), dict)
        and r.json()["detail"]["code"] == "mfa_required"
        and r.headers.get("X-TrueNorth-Auth-State") == "mfa_required"
    )


@pytest.mark.parametrize("role", PRIVILEGED)
def test_privileged_role_without_a_second_factor_is_refused(real_auth, db_session, role):
    assert _refused(_as(real_auth, _user(db_session, role)))


@pytest.mark.parametrize("role", PRIVILEGED)
def test_acr_1_is_not_mfa(real_auth, db_session, role):
    """F1: Keycloak's acr for a password sign-in."""
    assert _refused(_as(real_auth, _user(db_session, role), acr="1", amr=["pwd"]))


@pytest.mark.parametrize("claims", [{"amr": ["pwd", "otp"]}, {"amr": ["hwk"]}, {"acr": "mfa"}, {"amr": ["otp"]}])
@pytest.mark.parametrize("role", PRIVILEGED)
def test_privileged_role_with_a_second_factor_passes(real_auth, db_session, role, claims):
    r = _as(real_auth, _user(db_session, role), **claims)
    assert r.status_code == 200, r.text


def test_student_needs_no_second_factor(real_auth, db_session):
    assert _as(real_auth, _user(db_session, UserRole.student), amr=["pwd"], acr="1").status_code == 200


def test_lti_session_student_passes(real_auth, db_session):
    assert _as(real_auth, _user(db_session, UserRole.student), tn_session="lti").status_code == 200


def test_lti_session_cannot_carry_a_privileged_role(real_auth, db_session):
    """Defence in depth: lti_identity.session.verify already requires a Student."""
    assert _refused(_as(real_auth, _user(db_session, UserRole.admin), tn_session="lti", amr=["otp"]))


def test_inprocess_session_is_exempt(real_auth, db_session):
    assert _as(real_auth, _user(db_session, UserRole.admin), tn_session="inprocess").status_code == 200


def test_an_idp_token_claiming_inprocess_is_refused(real_auth, db_session):
    """F6: the claim is stripped on the IdP path, so the admin meets the MFA rule."""
    admin = _user(db_session, UserRole.admin)
    ident = token_from_idp_claims({"sub": admin.keycloak_id, "email": admin.email, "tn_session": "inprocess"})
    assert ident.tn_session == ""
    fastapi_app.dependency_overrides[get_token_identity] = lambda: ident
    assert _refused(real_auth.get("/users/me"))


def test_disabled_account_reads_as_disabled_not_mfa(real_auth, db_session):
    r = _as(real_auth, _user(db_session, UserRole.admin, active=False))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "account_disabled"


def test_role_comes_from_the_database_not_the_token(real_auth, db_session):
    admin = _user(db_session, UserRole.admin)
    r = _as(real_auth, admin, realm_access={"roles": ["student"]}, groups=["TN-Students"])
    assert _refused(r)


def test_observer_can_be_left_out_outside_production(real_auth, db_session, monkeypatch):
    monkeypatch.setenv("TN_MFA_ROLES", "admin,instructor,range_ops")
    assert _as(real_auth, _user(db_session, UserRole.observer)).status_code == 200


# ── The test-host demo exemption ───────────────────────────────────────
DEMO = "instructor.demo@truenorth.test"


def test_demo_staff_need_mfa_without_the_exemption(real_auth, db_session):
    assert _refused(_as(real_auth, _user(db_session, UserRole.instructor, email=DEMO)))


def test_demo_exemption_needs_tn_test_host(real_auth, db_session, monkeypatch):
    monkeypatch.setenv("TN_MFA_DEMO_EXEMPT", "true")
    assert _refused(_as(real_auth, _user(db_session, UserRole.instructor, email=DEMO)))


def test_demo_exemption_on_a_test_host_is_audited(real_auth, db_session, monkeypatch):
    monkeypatch.setenv("TN_MFA_DEMO_EXEMPT", "true")
    monkeypatch.setenv("TN_TEST_HOST", "true")
    demo = _user(db_session, UserRole.instructor, email=DEMO)
    assert _as(real_auth, demo).status_code == 200
    assert _as(real_auth, demo).status_code == 200
    rows = db_session.query(AuditLog).filter_by(action="mfa_demo_exempt", resource_id=str(demo.id)).all()
    assert len(rows) == 1  # once per user per day per process


@pytest.mark.parametrize(
    "email", ["admin@truenorth.test", "instructor.demo@truenorth.test.evil", "x@instructor.demo@truenorth.test",
              ".demo@truenorth.test", "instructor.demo@example.test"],
)
def test_demo_exemption_is_only_for_demo_addresses(real_auth, db_session, monkeypatch, email):
    monkeypatch.setenv("TN_MFA_DEMO_EXEMPT", "true")
    monkeypatch.setenv("TN_TEST_HOST", "true")
    assert _refused(_as(real_auth, _user(db_session, UserRole.admin, email=email)))


# ── /auth/me reports the MFA state (stays 200) ─────────────────────────
def test_auth_me_reports_mfa_required_and_unsatisfied(real_auth, db_session):
    admin = _user(db_session, UserRole.admin)
    fastapi_app.dependency_overrides[get_token_identity] = lambda: TokenPayload(sub=admin.keycloak_id, acr="1")
    body = real_auth.get("/auth/me").json()
    assert body["status"] == "registered"
    assert body["mfa_required"] is True and body["mfa_satisfied"] is False


def test_auth_me_reports_mfa_satisfied(real_auth, db_session):
    admin = _user(db_session, UserRole.admin)
    fastapi_app.dependency_overrides[get_token_identity] = lambda: TokenPayload(sub=admin.keycloak_id, amr=["otp"])
    body = real_auth.get("/auth/me").json()
    assert body["mfa_required"] is True and body["mfa_satisfied"] is True


def test_auth_me_student_is_not_required(real_auth, db_session):
    s = _user(db_session, UserRole.student)
    fastapi_app.dependency_overrides[get_token_identity] = lambda: TokenPayload(sub=s.keycloak_id)
    body = real_auth.get("/auth/me").json()
    assert body["mfa_required"] is False and body["mfa_satisfied"] is True


def test_auth_disabled_is_unchanged(client):
    """The e2e/itest/CI lanes run AUTH_DISABLED (as this suite does, tests/conftest.py):
    get_current_user returns the dev admin before the MFA rule, which needs no change there."""
    assert auth.AUTH_DISABLED
    assert client.get("/users/me").status_code == 200
