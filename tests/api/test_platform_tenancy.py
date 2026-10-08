"""Tenant isolation and permissions on the platform-core routers.

directory, auth_zones, storage, ad_sync and admin (teams, roster import). Until
2026-10-07 the directory and auth-zone routers fetched by id with ``db.get`` and listed
with no tenant predicate, every directory write rode on ``user:read``, storage needed
only a login, and AD-sync status counted every tenant's users.

Each test seeds a second tenant's rows straight into the session and asserts the API
cannot see or reach them. Foreign ids are 404 (``app.tenancy`` never confirms that a
foreign id exists); a caller missing a permission of their own role is 403.
"""

from __future__ import annotations

import io
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import (
    AuthZonePolicy,
    OrganizationalUnit,
    SecurityGroup,
    SecurityGroupMembership,
    StorageAppliance,
    StorageVolume,
    Team,
    TeamMembership,
    User,
    UserRole,
)
from app.rbac import ROLE_PERMISSIONS, Permission

# The identity `get_current_user` returns when AUTH_DISABLED is set (an admin).
DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


@contextmanager
def acting_as(role: UserRole, *, tenant: str = DEV_TENANT):
    who = CurrentUser(
        id=str(uuid.uuid4()),
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


def _tid(t: str) -> uuid.UUID:
    return uuid.UUID(t)


def _user(db, tenant: str, **kw) -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=kw.pop("email", f"{uuid.uuid4().hex[:10]}@example.test"),
        display_name="someone",
        role=kw.pop("role", UserRole.student),
        tenant_id=_tid(tenant),
        **kw,
    )
    db.add(u)
    db.flush()
    return u


def _ou(db, tenant: str, name: str) -> OrganizationalUnit:
    ou = OrganizationalUnit(id=uuid.uuid4(), name=name, slug=name.lower(), tenant_id=_tid(tenant))
    db.add(ou)
    db.flush()
    return ou


def _group(db, tenant: str, name: str, **kw) -> SecurityGroup:
    g = SecurityGroup(id=uuid.uuid4(), name=name, slug=name.lower(), tenant_id=_tid(tenant), **kw)
    db.add(g)
    db.flush()
    return g


def _zone(db, tenant: str | None, name: str) -> AuthZonePolicy:
    z = AuthZonePolicy(id=uuid.uuid4(), zone_name=name, tenant_id=_tid(tenant) if tenant else None)
    db.add(z)
    db.flush()
    return z


def _appliance(db, tenant: str, name: str) -> StorageAppliance:
    a = StorageAppliance(
        id=uuid.uuid4(),
        name=name,
        vendor="NetApp",
        model="AFF",
        management_ip="10.0.0.1",
        raw_capacity_tb=10,
        usable_capacity_tb=8,
        tenant_id=_tid(tenant),
    )
    db.add(a)
    db.flush()
    return a


# ── Directory ──────────────────────────────────────────────────────────
class TestDirectoryTenancy:
    @pytest.fixture
    def seeded(self, db_session):
        mine = _ou(db_session, DEV_TENANT, "Mine-OU")
        theirs = _ou(db_session, OTHER_TENANT, "Theirs-OU")
        my_group = _group(db_session, DEV_TENANT, "Mine-G")
        their_group = _group(db_session, OTHER_TENANT, "Theirs-G")
        db_session.commit()
        return mine, theirs, my_group, their_group

    def test_lists_and_tree_exclude_other_tenants(self, client, seeded):
        names = {o["name"] for o in client.get("/directory/ous").json()}
        assert "Mine-OU" in names and "Theirs-OU" not in names
        tree = {o["name"] for o in client.get("/directory/ous/tree").json()}
        assert "Mine-OU" in tree and "Theirs-OU" not in tree
        groups = {g["name"] for g in client.get("/directory/groups").json()}
        assert "Mine-G" in groups and "Theirs-G" not in groups

    def test_foreign_ou_is_404_on_every_verb(self, client, seeded):
        _, theirs, _, _ = seeded
        assert client.get(f"/directory/ous/{theirs.id}").status_code == 404
        assert client.patch(f"/directory/ous/{theirs.id}", json={"name": "x"}).status_code == 404
        assert client.delete(f"/directory/ous/{theirs.id}").status_code == 404

    def test_foreign_group_is_404_on_every_verb(self, client, seeded):
        _, _, _, their_group = seeded
        gid = their_group.id
        assert client.get(f"/directory/groups/{gid}").status_code == 404
        assert client.patch(f"/directory/groups/{gid}", json={"name": "x"}).status_code == 404
        assert client.delete(f"/directory/groups/{gid}").status_code == 404
        member = {"user_id": str(uuid.uuid4()), "group_id": str(gid)}
        assert client.post(f"/directory/groups/{gid}/members", json=member).status_code == 404
        assert client.delete(f"/directory/groups/{gid}/members/{uuid.uuid4()}").status_code == 404

    def test_create_stamps_tenant_and_refuses_a_foreign_parent(self, client, seeded, db_session):
        mine, theirs, _, _ = seeded
        bad = client.post("/directory/ous", json={"name": "Child", "slug": "child", "parent_id": str(theirs.id)})
        assert bad.status_code == 404
        ok = client.post("/directory/ous", json={"name": "Child", "slug": "child", "parent_id": str(mine.id)})
        assert ok.status_code == 201, ok.text
        assert ok.json()["tenant_id"] == DEV_TENANT
        bad_group = client.post("/directory/groups", json={"name": "G", "slug": "g", "ou_id": str(theirs.id)})
        assert bad_group.status_code == 404
        good_group = client.post("/directory/groups", json={"name": "G", "slug": "g"})
        assert good_group.status_code == 201
        assert good_group.json()["tenant_id"] == DEV_TENANT

    def test_an_ou_cannot_parent_itself(self, client, seeded):
        mine, *_ = seeded
        assert client.patch(f"/directory/ous/{mine.id}", json={"parent_id": str(mine.id)}).status_code == 422

    def test_membership_needs_a_user_in_the_same_tenant(self, client, seeded, db_session):
        _, _, my_group, _ = seeded
        foreigner = _user(db_session, OTHER_TENANT)
        local = _user(db_session, DEV_TENANT)
        db_session.commit()
        url = f"/directory/groups/{my_group.id}/members"
        assert client.post(url, json={"user_id": str(foreigner.id), "group_id": str(my_group.id)}).status_code == 404
        assert client.post(url, json={"user_id": str(local.id), "group_id": str(my_group.id)}).status_code == 201
        assert client.post(url, json={"user_id": str(local.id), "group_id": str(my_group.id)}).status_code == 409
        assert client.delete(f"{url}/{local.id}").status_code == 204

    def test_instructor_may_read_but_not_write(self, client, seeded):
        mine, _, my_group, _ = seeded
        with acting_as(UserRole.instructor):
            assert client.get("/directory/ous").status_code == 200
            assert client.get(f"/directory/groups/{my_group.id}").status_code == 200
            assert client.post("/directory/ous", json={"name": "N", "slug": "n"}).status_code == 403
            assert client.patch(f"/directory/ous/{mine.id}", json={"name": "N"}).status_code == 403
            assert client.delete(f"/directory/ous/{mine.id}").status_code == 403
            assert client.post("/directory/groups", json={"name": "N", "slug": "n"}).status_code == 403
            assert client.patch(f"/directory/groups/{my_group.id}", json={"name": "N"}).status_code == 403
            assert client.delete(f"/directory/groups/{my_group.id}").status_code == 403
            member = {"user_id": str(uuid.uuid4()), "group_id": str(my_group.id)}
            assert client.post(f"/directory/groups/{my_group.id}/members", json=member).status_code == 403

    def test_student_cannot_read_the_directory(self, client, seeded):
        with acting_as(UserRole.student):
            assert client.get("/directory/ous").status_code == 403

    def test_other_tenant_admin_sees_only_their_own(self, client, seeded):
        _, theirs, _, _ = seeded
        with acting_as(UserRole.admin, tenant=OTHER_TENANT):
            names = {o["name"] for o in client.get("/directory/ous").json()}
            assert names == {"Theirs-OU"}
            assert client.get(f"/directory/ous/{theirs.id}").status_code == 200


# ── Auth zones ─────────────────────────────────────────────────────────
class TestAuthZoneTenancy:
    @pytest.fixture
    def zones(self, db_session):
        mine = _zone(db_session, DEV_TENANT, "mine-zone")
        theirs = _zone(db_session, OTHER_TENANT, "theirs-zone")
        shared = _zone(db_session, None, "platform-default-zone")
        db_session.commit()
        return mine, theirs, shared

    def test_list_is_own_plus_platform_defaults(self, client, zones):
        names = {z["zone_name"] for z in client.get("/auth-zones").json()}
        assert {"mine-zone", "platform-default-zone"} <= names
        assert "theirs-zone" not in names

    def test_foreign_zone_is_404(self, client, zones):
        _, theirs, _ = zones
        assert client.get(f"/auth-zones/{theirs.id}").status_code == 404
        assert client.patch(f"/auth-zones/{theirs.id}", json={"zone_name": "x"}).status_code == 404
        assert client.delete(f"/auth-zones/{theirs.id}").status_code == 404

    def test_create_stamps_tenant_and_duplicate_name_is_409(self, client, zones):
        resp = client.post("/auth-zones", json={"zone_name": "new-zone"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["tenant_id"] == DEV_TENANT
        assert client.post("/auth-zones", json={"zone_name": "new-zone"}).status_code == 409

    def test_admin_may_change_a_platform_default(self, client, zones):
        _, _, shared = zones
        resp = client.patch(f"/auth-zones/{shared.id}", json={"zone_name": "platform-default-zone", "require_mfa": False})
        assert resp.status_code == 200, resp.text

    def test_platform_default_needs_tenant_update(self, client, zones, monkeypatch):
        """A role with user:update but not tenant:update owns its tenant's zones only."""
        mine, _, shared = zones
        monkeypatch.setitem(
            ROLE_PERMISSIONS, UserRole.instructor, ROLE_PERMISSIONS[UserRole.instructor] | {Permission.USER_UPDATE}
        )
        with acting_as(UserRole.instructor):
            assert client.patch(f"/auth-zones/{shared.id}", json={"zone_name": "p"}).status_code == 403
            assert client.delete(f"/auth-zones/{shared.id}").status_code == 403
            assert client.patch(f"/auth-zones/{mine.id}", json={"zone_name": "mine-zone"}).status_code == 200

    def test_instructor_cannot_touch_zones(self, client, zones):
        with acting_as(UserRole.instructor):
            assert client.get("/auth-zones").status_code == 403
            assert client.post("/auth-zones", json={"zone_name": "nope"}).status_code == 403


# ── Storage ────────────────────────────────────────────────────────────
APPLIANCE = {"name": "new-array", "vendor": "Dell", "model": "PowerStore", "management_ip": "10.0.0.9"}


class TestStorageTenancy:
    @pytest.fixture
    def arrays(self, db_session):
        mine = _appliance(db_session, DEV_TENANT, "mine-array")
        theirs = _appliance(db_session, OTHER_TENANT, "theirs-array")
        their_vol = StorageVolume(id=uuid.uuid4(), appliance_id=theirs.id, volume_name="v", tenant_id=_tid(OTHER_TENANT))
        db_session.add(their_vol)
        db_session.commit()
        return mine, theirs, their_vol

    def test_lists_and_summary_exclude_other_tenants(self, client, arrays):
        names = {a["name"] for a in client.get("/storage/appliances").json()}
        assert names == {"mine-array"}
        assert client.get("/storage/volumes").json() == []
        summary = client.get("/storage/summary").json()
        assert summary["total_appliances"] == 1
        assert summary["total_volumes"] == 0

    def test_foreign_appliance_and_volume_are_404(self, client, arrays):
        _, theirs, their_vol = arrays
        assert client.patch(f"/storage/appliances/{theirs.id}", json={"name": "x"}).status_code == 404
        assert client.delete(f"/storage/appliances/{theirs.id}").status_code == 404
        assert client.delete(f"/storage/volumes/{their_vol.id}").status_code == 404
        vol = {"appliance_id": str(theirs.id), "volume_name": "carve"}
        assert client.post("/storage/volumes", json=vol).status_code == 404

    def test_volume_on_own_appliance(self, client, arrays):
        mine, _, _ = arrays
        resp = client.post("/storage/volumes", json={"appliance_id": str(mine.id), "volume_name": "ok"})
        assert resp.status_code == 201, resp.text

    def test_student_cannot_read_or_write(self, client, arrays):
        mine, _, _ = arrays
        with acting_as(UserRole.student):
            assert client.get("/storage/appliances").status_code == 403
            assert client.get("/storage/summary").status_code == 403
            assert client.post("/storage/appliances", json=APPLIANCE).status_code == 403
            assert client.delete(f"/storage/appliances/{mine.id}").status_code == 403

    def test_instructor_reads_but_cannot_modify(self, client, arrays):
        mine, _, _ = arrays
        with acting_as(UserRole.instructor):
            assert client.get("/storage/appliances").status_code == 200
            assert client.post("/storage/appliances", json=APPLIANCE).status_code == 403
            assert client.patch(f"/storage/appliances/{mine.id}", json={"name": "x"}).status_code == 403
            assert client.delete(f"/storage/appliances/{mine.id}").status_code == 403
            vol = {"appliance_id": str(mine.id), "volume_name": "v"}
            assert client.post("/storage/volumes", json=vol).status_code == 403

    def test_range_ops_may_modify(self, client, arrays):
        with acting_as(UserRole.range_ops):
            resp = client.post("/storage/appliances", json=APPLIANCE)
            assert resp.status_code == 201, resp.text


# ── AD sync ────────────────────────────────────────────────────────────
class TestADSyncTenancy:
    def test_status_counts_only_the_callers_tenant(self, client, db_session, monkeypatch):
        for var in ("KEYCLOAK_URL", "KEYCLOAK_LDAP_COMPONENT_ID", "KEYCLOAK_ADMIN_CLIENT_SECRET"):
            monkeypatch.delenv(var, raising=False)
        now = datetime.now(UTC)
        _user(db_session, DEV_TENANT, source="ad", last_synced_at=now)
        for _ in range(3):
            _user(db_session, OTHER_TENANT, source="ad", last_synced_at=now)
        _group(db_session, DEV_TENANT, "ad-g", ad_object_guid=str(uuid.uuid4()))
        _group(db_session, OTHER_TENANT, "ad-g2", ad_object_guid=str(uuid.uuid4()))
        _group(db_session, OTHER_TENANT, "ad-g3", ad_object_guid=str(uuid.uuid4()))
        db_session.commit()
        body = client.get("/ad-sync/status").json()
        assert body["users_synced"] == 1
        assert body["groups_synced"] == 1
        with acting_as(UserRole.admin, tenant=OTHER_TENANT):
            body = client.get("/ad-sync/status").json()
            assert body["users_synced"] == 3
            assert body["groups_synced"] == 2

    def test_student_is_403(self, client):
        with acting_as(UserRole.student):
            assert client.get("/ad-sync/status").status_code == 403
            assert client.post("/ad-sync/trigger").status_code == 403
            assert client.get("/ad-sync/config").status_code == 403

    def test_instructor_cannot_trigger_or_read_config(self, client):
        with acting_as(UserRole.instructor):
            assert client.post("/ad-sync/trigger").status_code == 403
            assert client.get("/ad-sync/config").status_code == 403


# ── Admin: teams and roster import ─────────────────────────────────────
class TestAdminTenancy:
    @pytest.fixture
    def teams(self, db_session):
        mine = Team(id=uuid.uuid4(), name="Blue", tenant_id=_tid(DEV_TENANT))
        theirs = Team(id=uuid.uuid4(), name="Red", tenant_id=_tid(OTHER_TENANT))
        their_member = _user(db_session, OTHER_TENANT)
        db_session.add_all([mine, theirs])
        db_session.flush()
        db_session.add(TeamMembership(user_id=their_member.id, team_id=theirs.id))
        db_session.commit()
        return mine, theirs, their_member

    def test_student_cannot_update_a_team(self, client, teams):
        mine, _, _ = teams
        with acting_as(UserRole.student):
            assert client.patch(f"/teams/{mine.id}", json={"name": "Hijacked"}).status_code == 403

    def test_foreign_team_is_404(self, client, teams, db_session):
        _, theirs, their_member = teams
        assert client.patch(f"/teams/{theirs.id}", json={"name": "x"}).status_code == 404
        assert client.delete(f"/teams/{theirs.id}").status_code == 404
        assert client.get(f"/teams/{theirs.id}/members").status_code == 404
        resp = client.delete(f"/teams/{theirs.id}/members/{their_member.id}")
        assert resp.status_code == 404
        still = db_session.query(TeamMembership).filter_by(team_id=theirs.id, user_id=their_member.id).first()
        assert still is not None, "a foreign team's membership was removed"

    def test_foreign_user_is_404(self, client, teams):
        _, _, their_member = teams
        assert client.patch(f"/users/{their_member.id}?display_name=x").status_code == 404
        assert client.delete(f"/users/{their_member.id}").status_code == 404

    def test_lists_exclude_other_tenants(self, client, teams):
        names = {t["name"] for t in client.get("/teams").json()}
        assert "Blue" in names and "Red" not in names

    def test_roster_import_cannot_pull_in_another_tenants_user(self, client, db_session):
        victim = _user(db_session, OTHER_TENANT, email="victim@example.test", role=UserRole.instructor)
        db_session.commit()
        csv = b"email,role\nvictim@example.test,student\n"
        resp = client.post("/users/import-csv", files={"file": ("r.csv", io.BytesIO(csv), "text/csv")})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["created"] == 0 and body["updated"] == 0 and body["skipped"] == 1
        db_session.refresh(victim)
        assert str(victim.tenant_id) == OTHER_TENANT
        assert victim.role == UserRole.instructor

    def test_tenant_management_is_admin_only(self, client):
        with acting_as(UserRole.instructor):
            assert client.get("/tenants").status_code == 403
            assert client.post("/tenants", json={"name": "t", "slug": "t"}).status_code == 403
            assert client.get("/audit-log").status_code == 403


def test_membership_rows_have_no_tenant_of_their_own():
    """Why the directory checks the group before touching memberships."""
    assert "tenant_id" not in SecurityGroupMembership.__table__.columns
