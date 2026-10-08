"""Moodle sign-in tickets are bound to one tenant (security sweep H3).

One tool key signs every tenant's tickets, and ``local_truenorth``'s ticket.php checked
``tid`` only on sync tickets: an SSO ticket's only binding was its audience, the
platform's ``lti_issuer``, which any tenant's integration admin could set to another
tenant's Moodle. Now every ticket carries ``tid``, ticket.php checks it for every type,
an issuer belongs to one tenant, and re-pointing a Moodle needs the platform operator.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path

import jwt
import pytest
from app import lti13
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import ExternalPlatform, IntegrationAuthType, Tenant, User, UserRole

TENANT_A = uuid.UUID("00000000-0000-0000-0000-000000000001")  # seeded by conftest
TENANT_B = uuid.UUID("00000000-0000-0000-0000-0000000000b3")
MOODLE_A = "http://moodle-a.test"
MOODLE_B = "http://moodle-b.test"
TICKET_PHP = Path(__file__).resolve().parents[2] / "infra/platform/moodle/local_truenorth/classes/ticket.php"


@pytest.fixture(autouse=True)
def tenant_b(db_session):
    db_session.add(Tenant(id=TENANT_B, name="Unit B", slug=f"unit-b-{uuid.uuid4().hex[:6]}"))
    db_session.flush()


def _user(db, role: UserRole, tenant=TENANT_A) -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        display_name="Pat Person",
        role=role,
        tenant_id=tenant,
    )
    db.add(u)
    db.flush()
    return u


@contextmanager
def acting_as(u: User):
    fastapi_app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=str(u.id),
        email=u.email,
        display_name=u.display_name,
        role=u.role,
        tenant_id=str(u.tenant_id),
        keycloak_id=u.keycloak_id,
    )
    try:
        yield
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _moodle(db, tenant, issuer) -> ExternalPlatform:
    p = ExternalPlatform(
        id=uuid.uuid4(),
        name="Moodle",
        slug=f"moodle-{uuid.uuid4().hex[:6]}",
        platform_type="moodle",
        base_url="http://moodle:8080",
        auth_type=IntegrationAuthType.lti13,
        tenant_id=tenant,
        lti_issuer=issuer,
        is_active=True,
    )
    db.add(p)
    db.flush()
    return p


def _register(client, issuer: str):
    return client.post(
        "/integrations/platforms",
        json={
            "name": "Moodle",
            "slug": f"moodle-{uuid.uuid4().hex[:6]}",
            "platform_type": "moodle",
            "base_url": "http://moodle:8080",
            "auth_type": "lti13",
            "lti_issuer": issuer,
        },
    )


def test_a_students_sso_ticket_names_their_tenant(client, db_session):
    _moodle(db_session, TENANT_A, MOODLE_A)
    _moodle(db_session, TENANT_B, MOODLE_B)
    for tenant, site in ((TENANT_A, MOODLE_A), (TENANT_B, MOODLE_B)):
        with acting_as(_user(db_session, UserRole.student, tenant)):
            token = client.post("/integrations/moodle/sso", json={}).json()["token"]
        claims = jwt.decode(token, lti13.get_tool_key(db_session).public_key_pem, algorithms=["RS256"], audience=site)
        assert claims["typ"] == "sso" and claims["tid"] == str(tenant)


def test_ticket_php_checks_tid_for_every_ticket_type():
    src = TICKET_PHP.read_text()
    assert "if ($ok && $typ === 'sync')" not in src
    assert "hash_equals($tenant, (string) ($claims->tid ?? ''))" in src


def test_an_issuer_registered_to_another_tenant_is_refused(client, db_session):
    _moodle(db_session, TENANT_B, MOODLE_B)
    with acting_as(_user(db_session, UserRole.admin, TENANT_A)):
        assert _register(client, MOODLE_B).status_code == 409
        assert _register(client, MOODLE_B + "/").status_code == 409
        assert _register(client, MOODLE_A).status_code == 201


def test_a_tenant_admin_cannot_repoint_a_moodle(client, db_session, monkeypatch):
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(TENANT_B))  # tenant A's admin is not the operator
    p = _moodle(db_session, TENANT_A, MOODLE_A)
    db_session.commit()
    with acting_as(_user(db_session, UserRole.admin, TENANT_A)):
        r = client.patch(f"/integrations/platforms/{p.id}", json={"lti_issuer": "http://elsewhere.test"})
        assert r.status_code == 403
        # an unchanged issuer (the farm script re-sends it) and other fields are fine
        ok = client.patch(f"/integrations/platforms/{p.id}", json={"lti_issuer": MOODLE_A + "/", "name": "Renamed"})
        assert ok.status_code == 200, ok.text


def test_the_platform_operator_can_repoint_but_not_onto_another_tenants_site(client, db_session, monkeypatch):
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(TENANT_A))
    _moodle(db_session, TENANT_B, MOODLE_B)
    p = _moodle(db_session, TENANT_A, MOODLE_A)
    db_session.commit()
    with acting_as(_user(db_session, UserRole.admin, TENANT_A)):
        assert client.patch(f"/integrations/platforms/{p.id}", json={"lti_issuer": MOODLE_B}).status_code == 409
        r = client.patch(f"/integrations/platforms/{p.id}", json={"lti_issuer": "http://moodle-a2.test"})
        assert r.status_code == 200 and r.json()["lti_issuer"] == "http://moodle-a2.test"
