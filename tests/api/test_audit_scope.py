"""``GET /audit-log`` is scoped per tenant; only the platform administrator crosses tenants.

Before v1.2.0 the cross-tenant decision was ``user_has_permission(TENANT_READ)``. Every
``admin`` holds tenant:read, so every tenant's admin read every tenant's audit log plus
the legacy rows written before writers recorded a tenant. The rule is now
``rbac.is_platform_admin``: an admin of PLATFORM_TENANT_ID's tenant, or of a
single-tenant install; with PLATFORM_TENANT_ID unset and two tenants, nobody.

Tenants here are real rows (foreign keys enforced), so ``is_single_tenant`` sees them.
"""

from __future__ import annotations

import uuid

import pytest
from _shared import act_as, real_tenant, real_user
from app import rbac
from app.models import AuditLog, Tenant, UserRole


def _row(db, tenant_id, rid: str) -> None:
    db.add(AuditLog(tenant_id=tenant_id, action="test", resource_type="probe", resource_id=rid))
    db.flush()


def _seen(client, **params) -> set[str]:
    r = client.get("/audit-log", params=params)
    assert r.status_code == 200, r.text
    return {e["resource_id"] for e in r.json() if e["resource_type"] == "probe"}


@pytest.fixture
def two_tenants(db_session):
    """Tenants A and B (plus the seeded dev tenant) with one entry each and a legacy row."""
    a, b = real_tenant(db_session, "aud-a"), real_tenant(db_session, "aud-b")
    _row(db_session, a.id, "a")
    _row(db_session, b.id, "b")
    _row(db_session, None, "legacy")
    return a, b


def test_tenant_admin_sees_only_its_tenant_not_legacy_rows(client, db_session, two_tenants, monkeypatch):
    """Regression for F4: B's admin holds tenant:read and still sees only B."""
    a, b = two_tenants
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(a.id))
    admin_b = real_user(db_session, UserRole.admin, b.id)
    assert rbac.user_has_permission(admin_b, rbac.Permission.TENANT_READ)
    act_as(admin_b)

    assert _seen(client) == {"b"}


def test_tenant_admin_may_not_ask_for_another_tenant(client, db_session, two_tenants, monkeypatch):
    a, b = two_tenants
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(a.id))
    act_as(real_user(db_session, UserRole.admin, b.id))

    assert client.get("/audit-log", params={"tenant_id": str(a.id)}).status_code == 403
    assert _seen(client, tenant_id=str(b.id)) == {"b"}


def test_platform_admin_sees_every_tenant_and_legacy_rows(client, db_session, two_tenants, monkeypatch):
    a, b = two_tenants
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(a.id))
    act_as(real_user(db_session, UserRole.admin, a.id))

    assert _seen(client) == {"a", "b", "legacy"}
    assert _seen(client, tenant_id=str(b.id)) == {"b"}


def test_unset_platform_tenant_with_two_tenants_fails_closed(client, db_session, two_tenants, monkeypatch):
    a, b = two_tenants
    monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)

    act_as(real_user(db_session, UserRole.admin, a.id))
    assert _seen(client) == {"a"}
    act_as(real_user(db_session, UserRole.admin, b.id))
    assert _seen(client) == {"b"}
    assert client.get("/audit-log", params={"tenant_id": str(a.id)}).status_code == 403


def test_single_tenant_admin_sees_everything(client, db_session, monkeypatch):
    """One tenant (the seeded dev tenant): its admin is the operator, as before."""
    monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
    (only,) = db_session.query(Tenant).all()
    _row(db_session, only.id, "mine")
    _row(db_session, None, "legacy")
    act_as(real_user(db_session, UserRole.admin, only.id))

    assert _seen(client) == {"mine", "legacy"}


def test_observer_granted_audit_read_sees_own_tenant(client, db_session, two_tenants, monkeypatch):
    a, b = two_tenants
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(a.id))
    monkeypatch.setitem(
        rbac.ROLE_PERMISSIONS,
        UserRole.observer,
        rbac.ROLE_PERMISSIONS[UserRole.observer] | {rbac.Permission.AUDIT_READ},
    )
    # Even in the platform tenant: only an admin is the platform administrator.
    act_as(real_user(db_session, UserRole.observer, a.id))
    assert _seen(client) == {"a"}


def test_tenant_filter_must_be_a_uuid(client, db_session, two_tenants, monkeypatch):
    a, _ = two_tenants
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(a.id))
    act_as(real_user(db_session, UserRole.admin, a.id))
    assert client.get("/audit-log", params={"tenant_id": "not-a-uuid"}).status_code == 422
    assert _seen(client, tenant_id=str(uuid.uuid4())) == set()
