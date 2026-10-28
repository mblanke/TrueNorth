"""Review of #322 (f5b0d5e1): a tenant admin must not be able to become the platform admin.

B1  The installer used to re-pick the platform tenant by SLUG on every run, and a tenant's
    own admin could rename its slug (PUT /tenants/{id}), so tenant B's admin could take the
    freed bootstrap slug and be the platform administrator after the next upgrade. Now only
    the platform administrator changes a slug, and the bootstrap finds the tenant by the
    recorded PLATFORM_TENANT_ID.
N1  PLATFORM_TENANT_ID is normalised (uppercase works); a malformed one is reported.
N3  An unknown role on PATCH /users is a 422, not a 500.
N2  Rows with the old OU / group types (department, team, role) can still be edited.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from _shared import DEV_TENANT, act_as, acting_as, real_tenant, real_user
from app import main, rbac
from app.bootstrap_admin import ensure_tenant
from app.models import AuditLog, OrganizationalUnit, SecurityGroup, User, UserRole


# ── B1 ───────────────────────────────────────────────────────────────
def test_a_tenant_admin_cannot_rename_its_slug_but_can_rename_itself(client, db_session, monkeypatch):
    a, b = real_tenant(db_session, "adv-a"), real_tenant(db_session, "adv-b")
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(a.id))
    act_as(real_user(db_session, UserRole.admin, b.id))

    r = client.put(f"/tenants/{b.id}", json={"name": "B renamed", "slug": "adv-a"})
    assert r.status_code == 403
    assert "platform administrator" in r.json()["detail"]
    db_session.refresh(b)
    assert b.slug == "adv-b" and b.name == "adv-b"

    r = client.put(f"/tenants/{b.id}", json={"name": "B renamed", "slug": "adv-b"})
    assert r.status_code == 200, r.text
    db_session.refresh(b)
    assert b.name == "B renamed" and b.slug == "adv-b"


def test_renaming_or_creating_a_tenant_to_a_taken_name_is_a_409_not_a_500(client, db_session, monkeypatch):
    a, b = real_tenant(db_session, "nm-a"), real_tenant(db_session, "nm-b")
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(a.id))
    act_as(real_user(db_session, UserRole.admin, b.id))
    r = client.put(f"/tenants/{b.id}", json={"name": "nm-a", "slug": "nm-b"})
    assert r.status_code == 409 and "name" in r.json()["detail"]
    act_as(real_user(db_session, UserRole.admin, a.id))
    assert client.post("/tenants", json={"name": "nm-b", "slug": "nm-new"}).status_code == 409
    assert client.put(f"/tenants/{a.id}", json={"name": "nm-a", "slug": "nm-a"}).status_code == 200


def test_the_platform_admin_may_change_a_slug(client, db_session, monkeypatch):
    a, b = real_tenant(db_session, "adv-pa"), real_tenant(db_session, "adv-pb")
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(a.id))
    act_as(real_user(db_session, UserRole.admin, a.id))
    assert client.put(f"/tenants/{b.id}", json={"name": "B", "slug": "adv-pb2"}).status_code == 200
    assert client.put(f"/tenants/{a.id}", json={"name": "A", "slug": "adv-pa2"}).status_code == 200


def test_bootstrap_finds_the_platform_tenant_by_its_id_not_its_slug(db_session, monkeypatch):
    """The reviewer's capture: A renamed away, B on the bootstrap slug. The id wins."""
    a = real_tenant(db_session, "boot-a")
    b = real_tenant(db_session, "boot-b")
    b.slug = "bootslug"  # B has taken the slug the installer is configured with
    db_session.flush()
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(a.id).upper())
    assert ensure_tenant(db_session, "TrueNorth Range", "bootslug").id == a.id
    admin_b = real_user(db_session, UserRole.admin, b.id)
    admin_a = real_user(db_session, UserRole.admin, a.id)
    assert not rbac.is_platform_admin(admin_b, db_session)
    assert rbac.is_platform_admin(admin_a, db_session)


def test_bootstrap_without_a_recorded_id_still_finds_the_tenant_by_slug(db_session, monkeypatch):
    monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
    t = real_tenant(db_session, "boot-slug-only")
    assert ensure_tenant(db_session, "x", "boot-slug-only").id == t.id
    made = ensure_tenant(db_session, "New", "boot-new")
    assert made.slug == "boot-new" and made.id != t.id


def test_bootstrap_creates_an_explicit_platform_tenant_with_that_id(db_session, monkeypatch):
    wanted = uuid.uuid4()
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(wanted))
    made = ensure_tenant(db_session, "Operator", "operator")
    assert made.id == wanted and made.slug == "operator"


def test_bootstrap_refuses_an_explicit_id_whose_slug_belongs_to_another_tenant(db_session, monkeypatch):
    other = real_tenant(db_session, "taken-slug")
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(uuid.uuid4()))
    with pytest.raises(RuntimeError, match="taken-slug"):
        ensure_tenant(db_session, "Operator", other.slug)


# ── N1 ───────────────────────────────────────────────────────────────
def test_an_uppercase_platform_tenant_id_works(client, db_session, monkeypatch):
    a = real_tenant(db_session, "up-a")
    real_tenant(db_session, "up-b")
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(a.id).upper())
    admin_a = real_user(db_session, UserRole.admin, a.id)
    act_as(admin_a)
    assert rbac.is_platform_admin(admin_a, db_session)
    assert rbac.platform_tenant_problem(db_session) is None
    assert client.post("/tenants", json={"name": "zz", "slug": "zz-up"}).status_code == 201


@pytest.mark.parametrize("bad", ["default", "not-a-uuid", "1234"])
def test_a_malformed_platform_tenant_id_fails_closed_and_is_reported(client, db_session, monkeypatch, caplog, bad):
    a = real_tenant(db_session, "bad-a")
    monkeypatch.setenv("PLATFORM_TENANT_ID", bad)
    admin_a = real_user(db_session, UserRole.admin, a.id)
    assert not rbac.is_platform_admin(admin_a, db_session)  # even if the install has one tenant
    problem = rbac.platform_tenant_problem(db_session)
    assert problem and "not a tenant id (UUID)" in problem

    monkeypatch.setattr("app.db.SessionLocal", lambda: _Borrowed(db_session))
    with caplog.at_level(logging.ERROR):
        main._warn_platform_tenant()
    assert any("not a tenant id" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)


class _Borrowed:
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


# ── N3 ───────────────────────────────────────────────────────────────
def test_an_unknown_role_on_user_patch_is_a_422(client, db_session):
    t = real_tenant(db_session, "role-t")
    admin = real_user(db_session, UserRole.admin, t.id)
    other = real_user(db_session, UserRole.student, t.id)
    act_as(admin)
    r = client.patch(f"/users/{other.id}", params={"role": "superadmin"})
    assert r.status_code == 422
    assert "role" in r.text
    assert db_session.get(User, uuid.UUID(other.id)).role == UserRole.student
    assert client.patch(f"/users/{other.id}", params={"role": "instructor"}).status_code == 200


def test_user_patch_still_guards_tenant_and_last_admin(client, db_session):
    a, b = real_tenant(db_session), real_tenant(db_session)
    admin_a = real_user(db_session, UserRole.admin, a.id)
    victim_b = real_user(db_session, UserRole.admin, b.id)
    act_as(admin_a)
    assert client.patch(f"/users/{victim_b.id}", params={"is_active": "false"}).status_code == 404
    assert client.patch(f"/users/{admin_a.id}", params={"is_active": "false"}).status_code == 409
    assert client.patch(f"/users/{admin_a.id}", params={"role": "student"}).status_code == 409


def test_re_enabling_is_audited(client, db_session):
    t = real_tenant(db_session)
    admin = real_user(db_session, UserRole.admin, t.id)
    gone = real_user(db_session, UserRole.student, t.id)
    db_session.get(User, uuid.UUID(gone.id)).is_active = False
    db_session.flush()
    act_as(admin)
    before = db_session.query(AuditLog).filter(AuditLog.resource_id == gone.id).count()
    assert client.patch(f"/users/{gone.id}", params={"is_active": "true"}).status_code == 200
    assert db_session.query(AuditLog).filter(AuditLog.resource_id == gone.id).count() == before + 1


# ── N2 ───────────────────────────────────────────────────────────────
def test_a_row_with_a_legacy_type_can_be_renamed_without_resending_the_type(client, db_session):
    """The Users page sends the type back on edit; a legacy row's old value must not block it
    (the migration maps them, and an edit that leaves the type alone is accepted)."""
    ou = OrganizationalUnit(id=uuid.uuid4(), name="Legacy", slug=f"l-{uuid.uuid4().hex[:6]}",
                            ou_type="department", tenant_id=uuid.UUID(DEV_TENANT))
    g = SecurityGroup(id=uuid.uuid4(), name="LegG", slug=f"g-{uuid.uuid4().hex[:6]}",
                      group_type="role", tenant_id=uuid.UUID(DEV_TENANT))
    db_session.add_all([ou, g])
    db_session.flush()
    with acting_as(UserRole.admin):
        assert client.patch(f"/directory/ous/{ou.id}", json={"name": "Renamed"}).status_code == 200
        assert client.patch(f"/directory/groups/{g.id}", json={"name": "R"}).status_code == 200
        # Sending a value outside the enum is still refused.
        assert client.patch(f"/directory/ous/{ou.id}", json={"ou_type": "department"}).status_code == 422
        assert client.patch(f"/directory/groups/{g.id}", json={"group_type": "role"}).status_code == 422
