# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Directory (``app.routers.directory``): the reference data and tree behaviours.

Complements test_platform_tenancy.py (tenant isolation), test_directory_types_318.py
(enum validation) and test_delete_conflicts.py (foreign-tenant deletes) with the
behaviours those leave unasserted: the nation/coalition reference data (membership
filters, inactive rows hidden, unknown ids 404), the OU tree builder (nesting, orphan
parents landing at the root), the create/update guards (a parent or nation that does
not exist, a foreign parent, an OU made its own parent), the delete guards (sub-OU,
security group and team each 409 until cleared), and group membership (duplicate 409,
unknown member 404, delete taking the memberships with it).
"""

from __future__ import annotations

import uuid

import pytest
from _shared import DEV_TENANT, OTHER_TENANT, acting_as
from app.models import (
    Coalition,
    CoalitionMembership,
    Nation,
    OrganizationalUnit,
    SecurityGroup,
    SecurityGroupMembership,
    Team,
    User,
    UserRole,
)

# The dev tenant is seeded (unlike a fresh real_tenant row), and its admin is the
# identity AUTH_DISABLED hands back, so directory rows live there.
TENANT = uuid.UUID(DEV_TENANT)
FOREIGN = uuid.UUID(OTHER_TENANT)


def _user(db_session, tenant_id=TENANT):
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        display_name="Member",
        role=UserRole.student,
        tenant_id=tenant_id,
    )
    db_session.add(u)
    db_session.flush()
    return u


def _foreign_tenant(db_session):
    """A second tenants row: foreign keys are on, and only the dev tenant is seeded."""
    from app.models import Tenant

    row = Tenant(id=FOREIGN, name="Other Org", slug="other")
    db_session.add(row)
    db_session.flush()
    return row


def _slug() -> str:
    return f"d-{uuid.uuid4().hex[:8]}"


# ── Nations ──────────────────────────────────────────────────────────────────


@pytest.fixture
def nations(db_session):
    rows = [
        Nation(
            id=uuid.uuid4(),
            name=f"Alphaland {uuid.uuid4().hex[:6]}",
            iso_alpha2="Z1",
            iso_alpha3="Z1Z",
            flag_emoji="F",
            is_active=True,
            is_nato=True,
            is_fvey=True,
        ),
        Nation(
            id=uuid.uuid4(),
            name=f"Betalund {uuid.uuid4().hex[:6]}",
            iso_alpha2="Z2",
            iso_alpha3="Z2Z",
            flag_emoji="F",
            is_active=True,
            is_nato=True,
            is_fvey=False,
        ),
        Nation(
            id=uuid.uuid4(),
            name=f"Gammastan {uuid.uuid4().hex[:6]}",
            iso_alpha2="Z3",
            iso_alpha3="Z3Z",
            flag_emoji="F",
            is_active=True,
            is_nato=False,
            is_fvey=False,
        ),
        Nation(
            id=uuid.uuid4(),
            name=f"Deltaland {uuid.uuid4().hex[:6]}",
            iso_alpha2="Z4",
            iso_alpha3="Z4Z",
            flag_emoji="F",
            is_active=False,
            is_nato=True,
            is_fvey=True,
        ),
    ]
    db_session.add_all(rows)
    db_session.commit()
    return rows


def test_nation_listing_shows_the_active_and_hides_the_rest(client, nations):
    with acting_as(UserRole.admin):
        names = [n["name"] for n in client.get("/directory/nations").json()]
    assert {nations[0].name, nations[1].name, nations[2].name} <= set(names)
    assert nations[3].name not in names


def test_nation_membership_filters_narrow_the_list(client, nations):
    with acting_as(UserRole.admin):
        nato = {n["name"] for n in client.get("/directory/nations", params={"nato_only": "true"}).json()}
        fvey = {n["name"] for n in client.get("/directory/nations", params={"fvey_only": "true"}).json()}
    assert {nations[0].name, nations[1].name} <= nato
    assert nations[2].name not in nato
    assert nations[0].name in fvey
    assert nations[1].name not in fvey


def test_an_unknown_nation_is_not_found(client, nations):
    with acting_as(UserRole.admin):
        assert client.get(f"/directory/nations/{uuid.uuid4()}").status_code == 404
        assert client.get(f"/directory/nations/{nations[0].id}").status_code == 200


# ── Coalitions ───────────────────────────────────────────────────────────────


def test_coalition_endpoints_list_show_members_and_hide_the_dead(db_session, client, nations):
    live = Coalition(id=uuid.uuid4(), name=f"Cov-d live {uuid.uuid4().hex[:6]}", slug=_slug(), is_active=True)
    dead = Coalition(id=uuid.uuid4(), name=f"Cov-d dead {uuid.uuid4().hex[:6]}", slug=_slug(), is_active=False)
    db_session.add_all([live, dead])
    db_session.add(CoalitionMembership(coalition_id=live.id, nation_id=nations[0].id))
    db_session.commit()
    with acting_as(UserRole.admin):
        listed = {c["name"] for c in client.get("/directory/coalitions").json()}
        assert live.name in listed and dead.name not in listed
        got = client.get(f"/directory/coalitions/{live.id}")
        assert got.status_code == 200 and got.json()["name"] == live.name
        member_ids = {n["id"] for n in client.get(f"/directory/coalitions/{live.id}/members").json()}
        assert str(nations[0].id) in member_ids
        assert client.get(f"/directory/coalitions/{uuid.uuid4()}").status_code == 404
        assert client.get(f"/directory/coalitions/{uuid.uuid4()}/members").json() == []


# ── OU tree and guards ───────────────────────────────────────────────────────


def test_ou_create_rejects_a_missing_nation_and_a_foreign_parent(db_session, client):
    _foreign_tenant(db_session)
    theirs = OrganizationalUnit(id=uuid.uuid4(), name="Theirs", slug=_slug(), tenant_id=FOREIGN)
    db_session.add(theirs)
    db_session.commit()
    with acting_as(UserRole.admin):
        assert (
            client.post(
                "/directory/ous",
                json={"name": "X", "slug": _slug(), "ou_type": "custom", "nation_id": str(uuid.uuid4())},
            ).status_code
            == 404
        )
        r = client.post(
            "/directory/ous", json={"name": "X", "slug": _slug(), "ou_type": "custom", "parent_id": str(theirs.id)}
        )
        assert r.status_code == 404
        assert "Parent OU not found" in r.text


def test_ou_tree_builds_nesting_and_treats_a_foreign_parent_as_a_root(db_session, client):
    _foreign_tenant(db_session)
    theirs = OrganizationalUnit(id=uuid.uuid4(), name="Theirs", slug=_slug(), tenant_id=FOREIGN)
    parent = OrganizationalUnit(id=uuid.uuid4(), name="HQ", slug=_slug(), tenant_id=TENANT)
    child = OrganizationalUnit(id=uuid.uuid4(), name="Ops", slug=_slug(), tenant_id=TENANT, parent_id=parent.id)
    orphan = OrganizationalUnit(id=uuid.uuid4(), name="Ghost", slug=_slug(), tenant_id=TENANT, parent_id=theirs.id)
    db_session.add_all([theirs, parent, child, orphan])
    db_session.commit()
    with acting_as(UserRole.admin):
        tree = client.get("/directory/ous/tree").json()
    by_id = {str(n["id"]): n for n in tree}
    assert str(child.id) in [c["id"] for c in by_id[str(parent.id)]["children"]]
    # The orphan's parent belongs to another tenant, so it is listed as a root, not a loss.
    assert str(orphan.id) in by_id


def test_an_ou_cannot_be_made_its_own_parent(db_session, client):
    ou = OrganizationalUnit(id=uuid.uuid4(), name="Loop", slug=_slug(), tenant_id=TENANT)
    db_session.add(ou)
    db_session.commit()
    with acting_as(UserRole.admin):
        r = client.patch(f"/directory/ous/{ou.id}", json={"parent_id": str(ou.id)})
    assert r.status_code == 422
    assert "its own parent" in r.text


def test_a_foreign_ou_cannot_be_taken_on_as_a_parent(db_session, client):
    mine = OrganizationalUnit(id=uuid.uuid4(), name="Mine", slug=_slug(), tenant_id=TENANT)
    db_session.add(mine)
    db_session.commit()
    with acting_as(UserRole.admin, tenant=OTHER_TENANT):
        r = client.post(
            "/directory/ous", json={"name": "X", "slug": _slug(), "ou_type": "custom", "parent_id": str(mine.id)}
        )
    assert r.status_code == 404


@pytest.fixture
def guarded_ou(db_session):
    ou = OrganizationalUnit(id=uuid.uuid4(), name="Guarded", slug=_slug(), tenant_id=TENANT)
    db_session.add(ou)
    db_session.commit()
    return ou


def test_deleting_an_ou_with_a_sub_ou_is_refused(db_session, client, guarded_ou):
    db_session.add(
        OrganizationalUnit(
            id=uuid.uuid4(), name="Sub", slug=_slug(), tenant_id=guarded_ou.tenant_id, parent_id=guarded_ou.id
        )
    )
    db_session.commit()
    with acting_as(UserRole.admin):
        r = client.delete(f"/directory/ous/{guarded_ou.id}")
    assert r.status_code == 409
    assert "sub-OU" in r.text


def test_deleting_an_ou_holding_a_security_group_is_refused(db_session, client, guarded_ou):
    db_session.add(
        SecurityGroup(
            id=uuid.uuid4(),
            name="SG",
            slug=_slug(),
            group_type="access",
            tenant_id=guarded_ou.tenant_id,
            ou_id=guarded_ou.id,
        )
    )
    db_session.commit()
    with acting_as(UserRole.admin):
        r = client.delete(f"/directory/ous/{guarded_ou.id}")
    assert r.status_code == 409
    assert "security group" in r.text


def test_deleting_an_ou_assigned_to_a_team_is_refused(db_session, client, guarded_ou):
    db_session.add(Team(id=uuid.uuid4(), name="T", tenant_id=guarded_ou.tenant_id, ou_id=guarded_ou.id))
    db_session.commit()
    with acting_as(UserRole.admin):
        r = client.delete(f"/directory/ous/{guarded_ou.id}")
    assert r.status_code == 409
    assert "team" in r.text


def test_an_ou_whose_guards_are_clear_deletes(db_session, client, guarded_ou):
    with acting_as(UserRole.admin):
        assert client.delete(f"/directory/ous/{guarded_ou.id}").status_code == 204
        assert client.get(f"/directory/ous/{guarded_ou.id}").status_code == 404


# ── Security groups and membership ───────────────────────────────────────────


def test_a_group_cannot_be_attached_to_a_foreign_ou(db_session, client):
    _foreign_tenant(db_session)
    theirs = OrganizationalUnit(id=uuid.uuid4(), name="Theirs", slug=_slug(), tenant_id=FOREIGN)
    db_session.add(theirs)
    db_session.commit()
    with acting_as(UserRole.admin):
        r = client.post(
            "/directory/groups", json={"name": "G", "slug": _slug(), "group_type": "access", "ou_id": str(theirs.id)}
        )
    assert r.status_code == 404
    assert "OU not found" in r.text


def test_group_lifecycle_patch_and_delete_takes_memberships_along(db_session, client):
    user = _user(db_session)
    with acting_as(UserRole.admin):
        r = client.post(
            "/directory/groups",
            json={"name": "Watchers", "slug": _slug(), "group_type": "access", "description": "original"},
        )
        assert r.status_code == 201, r.text
        gid = r.json()["id"]
        member = client.post(f"/directory/groups/{gid}/members", json={"user_id": str(user.id), "group_id": gid})
        assert member.status_code == 201, member.text
        patched = client.patch(f"/directory/groups/{gid}", json={"description": "updated"})
        assert patched.status_code == 200 and patched.json()["description"] == "updated"
        assert client.delete(f"/directory/groups/{gid}").status_code == 204
        assert client.get(f"/directory/groups/{gid}").status_code == 404
    assert db_session.query(SecurityGroupMembership).filter_by(user_id=user.id).count() == 0


def test_group_membership_add_is_guarded_and_removable(db_session, client):
    user = _user(db_session)
    _foreign_tenant(db_session)
    stranger = _user(db_session, FOREIGN)
    with acting_as(UserRole.admin):
        gid = client.post("/directory/groups", json={"name": "G", "slug": _slug(), "group_type": "role_based"}).json()[
            "id"
        ]
        assert (
            client.post(f"/directory/groups/{gid}/members", json={"user_id": str(user.id), "group_id": gid}).status_code
            == 201
        )
        dup = client.post(f"/directory/groups/{gid}/members", json={"user_id": str(user.id), "group_id": gid})
        assert dup.status_code == 409 and "already in group" in dup.text
        foreign = client.post(f"/directory/groups/{gid}/members", json={"user_id": str(stranger.id), "group_id": gid})
        assert foreign.status_code == 404 and "User not found" in foreign.text
        assert client.delete(f"/directory/groups/{gid}/members/{user.id}").status_code == 204
        gone = client.delete(f"/directory/groups/{gid}/members/{user.id}")
        assert gone.status_code == 404 and "Membership not found" in gone.text


def test_a_write_needs_user_update_even_for_a_reader_role(client):
    with acting_as(UserRole.instructor):
        assert (
            client.post("/directory/ous", json={"name": "X", "slug": _slug(), "ou_type": "custom"}).status_code == 403
        )
