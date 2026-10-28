"""Who the platform administrator is, as a fresh multi-tenant site meets it (#318, #273).

The installer now renders ``PLATFORM_TENANT_ID`` (``tn_platform_tenant_id``, by default the
bootstrap tenant 80-seed creates). The API side, end to end through platform actions:

- an admin of the platform tenant (A) creates tenants and lists every tenant;
- an admin of another tenant (B) cannot, and sees only B;
- unset with two tenants, nobody is the platform administrator (it fails closed, never
  "every admin"), and startup logs an error that says what to set;
- set to an id that is no tenant, nobody is either, and startup says so.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from _shared import act_as, real_tenant, real_user
from app import main, rbac
from app.models import UserRole


@pytest.fixture
def tenants(db_session):
    """Tenants A (the operator's) and B, plus the seeded dev tenant: never single-tenant."""
    return real_tenant(db_session, "plat-a"), real_tenant(db_session, "plat-b")


def _new_tenant(client):
    slug = f"n-{uuid.uuid4().hex[:8]}"
    return client.post("/tenants", json={"name": slug, "slug": slug})


def test_platform_admin_in_tenant_a_can_create_and_list_tenants(client, db_session, tenants, monkeypatch):
    a, b = tenants
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(a.id))
    admin_a = real_user(db_session, UserRole.admin, a.id)
    act_as(admin_a)

    assert rbac.is_platform_admin(admin_a, db_session)
    assert _new_tenant(client).status_code == 201
    listed = {t["id"] for t in client.get("/tenants").json()}
    assert {str(a.id), str(b.id)} <= listed


def test_tenant_admin_in_tenant_b_cannot_act_for_the_platform(client, db_session, tenants, monkeypatch):
    a, b = tenants
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(a.id))
    admin_b = real_user(db_session, UserRole.admin, b.id)
    act_as(admin_b)

    assert not rbac.is_platform_admin(admin_b, db_session)
    r = _new_tenant(client)
    assert r.status_code == 403
    assert "platform administrator" in r.json()["detail"]
    assert {t["id"] for t in client.get("/tenants").json()} == {str(b.id)}


def test_unset_with_two_tenants_nobody_is_platform_admin(client, db_session, tenants, monkeypatch):
    a, b = tenants
    monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
    for t in (a, b):
        admin = real_user(db_session, UserRole.admin, t.id)
        act_as(admin)
        assert not rbac.is_platform_admin(admin, db_session), "unset must never mean every admin"
        assert _new_tenant(client).status_code == 403
        assert {x["id"] for x in client.get("/tenants").json()} == {str(t.id)}


def test_unset_with_two_tenants_is_logged_at_startup(db_session, tenants, monkeypatch, caplog):
    monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
    problem = rbac.platform_tenant_problem(db_session)
    assert problem and "PLATFORM_TENANT_ID is not set" in problem and "tn_platform_tenant_id" in problem

    monkeypatch.setattr("app.db.SessionLocal", lambda: _Borrowed(db_session))
    with caplog.at_level(logging.ERROR):
        main._warn_platform_tenant()
    assert any("PLATFORM_TENANT_ID is not set" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)


def test_set_to_no_such_tenant_fails_closed_and_is_reported(client, db_session, tenants, monkeypatch):
    a, _ = tenants
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(uuid.uuid4()))
    admin_a = real_user(db_session, UserRole.admin, a.id)
    act_as(admin_a)

    assert not rbac.is_platform_admin(admin_a, db_session)
    assert _new_tenant(client).status_code == 403
    problem = rbac.platform_tenant_problem(db_session)
    assert problem and "names no tenant" in problem


def test_set_to_an_existing_tenant_is_no_problem(db_session, tenants, monkeypatch):
    a, _ = tenants
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(a.id))
    assert rbac.platform_tenant_problem(db_session) is None


def test_a_non_admin_of_the_platform_tenant_is_not_platform_admin(db_session, tenants, monkeypatch):
    a, _ = tenants
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(a.id))
    for role in (UserRole.instructor, UserRole.range_ops, UserRole.student):
        assert not rbac.is_platform_admin(real_user(db_session, role, a.id), db_session)


class _Borrowed:
    """The test's session, handed to code that closes what it opens."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass
