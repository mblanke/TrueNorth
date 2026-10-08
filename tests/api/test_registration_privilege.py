"""Registration approval may not mint authority the approver does not hold (C1).

An instructor holds registration:approve. Before the fix, ``role`` on the approve body
accepted ``admin``, so an instructor could create an administrator — and, with
PLATFORM_TENANT_ID unset, a platform administrator.
"""

from __future__ import annotations

import uuid

import pytest
from _shared import act_as, real_tenant, real_user
from app.models import RegistrationRequest, RegistrationStatus, User, UserRole
from app.rbac import grant_refusal


def _pending(db, tenant_id, sub: str | None = None) -> RegistrationRequest:
    sub = sub or f"ad-{uuid.uuid4().hex[:8]}"
    row = RegistrationRequest(
        id=uuid.uuid4(), keycloak_id=sub, email=f"{sub}@corp.example", display_name=sub,
        suggested_tenant_id=tenant_id, status=RegistrationStatus.pending,
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def tenant_a(db_session):
    return real_tenant(db_session, "alpha")


@pytest.mark.parametrize("role", ["admin", "instructor", "range_ops"])
def test_instructor_cannot_approve_a_staff_role(client, db_session, tenant_a, role):
    act_as(real_user(db_session, UserRole.instructor, tenant_a.id))
    req = _pending(db_session, tenant_a.id)

    r = client.post(f"/registration/requests/{req.id}/approve", json={"role": role, "tenant_id": str(tenant_a.id)})

    assert r.status_code == 403, r.text
    assert db_session.query(User).filter(User.keycloak_id == req.keycloak_id).count() == 0
    db_session.refresh(req)
    assert req.status == RegistrationStatus.pending


@pytest.mark.parametrize("role", ["student", "observer"])
def test_instructor_can_still_approve_a_student_or_observer(client, db_session, tenant_a, role):
    act_as(real_user(db_session, UserRole.instructor, tenant_a.id))
    req = _pending(db_session, tenant_a.id)

    r = client.post(f"/registration/requests/{req.id}/approve", json={"role": role, "tenant_id": str(tenant_a.id)})

    assert r.status_code == 200, r.text
    assert db_session.query(User).filter(User.keycloak_id == req.keycloak_id).one().role == UserRole(role)


def test_instructor_bulk_approve_as_admin_is_refused_whole(client, db_session, tenant_a):
    act_as(real_user(db_session, UserRole.instructor, tenant_a.id))
    ids = [str(_pending(db_session, tenant_a.id).id) for _ in range(2)]

    r = client.post("/registration/requests/bulk-approve",
                    json={"request_ids": ids, "role": "admin", "tenant_id": str(tenant_a.id)})

    assert r.status_code == 403
    assert db_session.query(User).filter(User.role == UserRole.admin, User.tenant_id == tenant_a.id).count() == 0


def test_admin_may_approve_an_instructor(client, db_session, tenant_a):
    act_as(real_user(db_session, UserRole.admin, tenant_a.id))
    req = _pending(db_session, tenant_a.id)

    r = client.post(f"/registration/requests/{req.id}/approve",
                    json={"role": "instructor", "tenant_id": str(tenant_a.id)})

    assert r.status_code == 200, r.text


def test_grant_rule_is_a_permission_subset():
    from app.auth import CurrentUser

    def who(role):
        return CurrentUser(id=str(uuid.uuid4()), email="x@y", display_name="x", role=role,
                           tenant_id=str(uuid.uuid4()), keycloak_id="k")

    assert grant_refusal(who(UserRole.admin), UserRole.admin) is None
    assert grant_refusal(who(UserRole.instructor), UserRole.student) is None
    assert grant_refusal(who(UserRole.instructor), UserRole.admin)
    assert grant_refusal(who(UserRole.range_ops), UserRole.student)  # range_ops lacks detection:submit
    assert grant_refusal(who(UserRole.student), UserRole.student) is None


# ── H5: admins are per tenant; only the platform administrator crosses tenants ──
@pytest.fixture
def two_tenants_operator_elsewhere(db_session, monkeypatch):
    """Tenants A and B, with PLATFORM_TENANT_ID naming a third (operator) tenant."""
    a, b, op = (real_tenant(db_session, s) for s in ("ten-a", "ten-b", "operator"))
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(op.id))
    return a, b, op


def test_tenant_admin_does_not_see_other_tenants_requests(client, db_session, two_tenants_operator_elsewhere):
    a, b, _ = two_tenants_operator_elsewhere
    mine, theirs = _pending(db_session, a.id), _pending(db_session, b.id)
    act_as(real_user(db_session, UserRole.admin, a.id))

    ids = {r["id"] for r in client.get("/registration/requests").json()}

    assert str(mine.id) in ids and str(theirs.id) not in ids
    assert client.get(f"/registration/requests/{theirs.id}").status_code == 404
    assert client.post(f"/registration/requests/{theirs.id}/approve", json={"role": "student"}).status_code == 404


def test_tenant_admin_cannot_approve_into_another_tenant(client, db_session, two_tenants_operator_elsewhere):
    a, b, _ = two_tenants_operator_elsewhere
    req = _pending(db_session, None)  # unassigned: visible to A's admin
    act_as(real_user(db_session, UserRole.admin, a.id))

    r = client.post(f"/registration/requests/{req.id}/approve", json={"role": "admin", "tenant_id": str(b.id)})

    assert r.status_code == 403
    assert db_session.query(User).filter(User.tenant_id == b.id).count() == 0


def test_platform_admin_sees_and_approves_across_tenants(client, db_session, two_tenants_operator_elsewhere):
    _, b, op = two_tenants_operator_elsewhere
    req = _pending(db_session, b.id)
    act_as(real_user(db_session, UserRole.admin, op.id))

    assert str(req.id) in {r["id"] for r in client.get("/registration/requests").json()}
    r = client.post(f"/registration/requests/{req.id}/approve", json={"role": "student", "tenant_id": str(b.id)})
    assert r.status_code == 200, r.text


def test_tenant_admin_tenant_endpoints_are_scoped(client, db_session, two_tenants_operator_elsewhere):
    a, b, _ = two_tenants_operator_elsewhere
    act_as(real_user(db_session, UserRole.admin, a.id))

    assert {t["id"] for t in client.get("/tenants").json()} == {str(a.id)}
    assert client.post("/tenants", json={"name": "Rogue", "slug": "rogue"}).status_code == 403
    assert client.put(f"/tenants/{b.id}", json={"name": "Pwned", "slug": "pwned"}).status_code == 404
    db_session.refresh(b)
    assert b.name == "ten-b"
    assert client.put(f"/tenants/{a.id}", json={"name": "Alpha 2", "slug": "ten-a"}).status_code == 200


def test_platform_admin_manages_all_tenants(client, db_session, two_tenants_operator_elsewhere):
    a, b, op = two_tenants_operator_elsewhere
    act_as(real_user(db_session, UserRole.admin, op.id))

    assert {str(a.id), str(b.id), str(op.id)} <= {t["id"] for t in client.get("/tenants").json()}
    assert client.post("/tenants", json={"name": "New", "slug": "new-tenant"}).status_code == 201
    assert client.put(f"/tenants/{b.id}", json={"name": "B2", "slug": "ten-b"}).status_code == 200


# ── H5 fails closed: PLATFORM_TENANT_ID unset and more than one tenant ──
@pytest.fixture
def two_tenants_platform_unset(db_session, monkeypatch):
    """Tenants A and B (the seeded dev tenant makes three), PLATFORM_TENANT_ID unset."""
    monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
    return real_tenant(db_session, "uns-a"), real_tenant(db_session, "uns-b")


def test_unset_platform_tenant_admin_of_b_cannot_manage_tenants(client, db_session, two_tenants_platform_unset):
    a, b = two_tenants_platform_unset
    act_as(real_user(db_session, UserRole.admin, b.id))

    assert {t["id"] for t in client.get("/tenants").json()} == {str(b.id)}
    assert client.post("/tenants", json={"name": "Rogue", "slug": "rogue"}).status_code == 403
    assert client.put(f"/tenants/{a.id}", json={"name": "Pwned", "slug": "pwned"}).status_code == 404


def test_unset_platform_tenant_admin_of_b_cannot_approve_into_a(client, db_session, two_tenants_platform_unset):
    a, b = two_tenants_platform_unset
    theirs, unassigned = _pending(db_session, a.id), _pending(db_session, None)
    act_as(real_user(db_session, UserRole.admin, b.id))

    assert str(theirs.id) not in {r["id"] for r in client.get("/registration/requests").json()}
    assert client.post(f"/registration/requests/{theirs.id}/approve", json={"role": "student"}).status_code == 404
    r = client.post(f"/registration/requests/{unassigned.id}/approve", json={"role": "admin", "tenant_id": str(a.id)})
    assert r.status_code == 403
    assert db_session.query(User).filter(User.keycloak_id == unassigned.keycloak_id).count() == 0


def test_unset_platform_tenant_single_tenant_admin_is_still_the_operator(client, db_session, monkeypatch):
    """One tenant (the seeded dev tenant): its admin is the platform administrator."""
    from app.models import Tenant
    from app.rbac import is_platform_admin, platform_tenant_problem

    monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
    (only,) = db_session.query(Tenant).all()
    admin = real_user(db_session, UserRole.admin, only.id)
    act_as(admin)

    assert is_platform_admin(admin, db_session)
    assert platform_tenant_problem(db_session) is None
    assert client.post("/tenants", json={"name": "Second", "slug": "second"}).status_code == 201
    # A second tenant now exists: nobody is the operator until PLATFORM_TENANT_ID is set.
    assert not is_platform_admin(admin, db_session)
    assert "PLATFORM_TENANT_ID" in platform_tenant_problem(db_session)
    assert client.post("/tenants", json={"name": "Third", "slug": "third"}).status_code == 403


# ── Adoption: approval may take over only an account the approver could have made ──
def _local_account(db, tenant_id, role: UserRole, email: str) -> User:
    row = User(id=uuid.uuid4(), keycloak_id=f"local-{uuid.uuid4().hex[:8]}", email=email,
               display_name=email, role=role, tenant_id=tenant_id, source="local")
    db.add(row)
    db.flush()
    return row


def _pending_for(db, tenant_id, email: str) -> RegistrationRequest:
    req = _pending(db, tenant_id)
    req.email = email
    db.flush()
    return req


def test_admin_of_b_cannot_adopt_tenant_a_admin_by_email(client, db_session, two_tenants_operator_elsewhere):
    """The hijack: B's admin approves a request carrying A's admin's email into B."""
    a, b, _ = two_tenants_operator_elsewhere
    victim = _local_account(db_session, a.id, UserRole.admin, "boss@alpha.example")
    kc_before = victim.keycloak_id
    req = _pending_for(db_session, b.id, "boss@alpha.example")
    act_as(real_user(db_session, UserRole.admin, b.id))

    r = client.post(f"/registration/requests/{req.id}/approve", json={"role": "student", "tenant_id": str(b.id)})

    assert r.status_code == 409, r.text
    assert "another tenant" in r.json()["detail"]
    db_session.refresh(victim)
    assert (victim.tenant_id, victim.role, victim.keycloak_id) == (a.id, UserRole.admin, kc_before)
    db_session.refresh(req)
    assert req.status == RegistrationStatus.pending


def test_instructor_cannot_adopt_an_admin_account_in_own_tenant(client, db_session, tenant_a):
    admin_row = _local_account(db_session, tenant_a.id, UserRole.admin, "chief@alpha.example")
    req = _pending_for(db_session, tenant_a.id, "chief@alpha.example")
    act_as(real_user(db_session, UserRole.instructor, tenant_a.id))

    r = client.post(f"/registration/requests/{req.id}/approve", json={"role": "student", "tenant_id": str(tenant_a.id)})

    assert r.status_code == 409, r.text
    assert "cannot grant" in r.json()["detail"]
    db_session.refresh(admin_row)
    assert admin_row.role == UserRole.admin


def test_roster_student_in_the_target_tenant_is_still_adopted(client, db_session, tenant_a):
    roster = _local_account(db_session, tenant_a.id, UserRole.student, "cadet@alpha.example")
    roster.source = "csv_import"
    req = _pending_for(db_session, tenant_a.id, "cadet@alpha.example")
    act_as(real_user(db_session, UserRole.instructor, tenant_a.id))

    r = client.post(f"/registration/requests/{req.id}/approve", json={"role": "student", "tenant_id": str(tenant_a.id)})

    assert r.status_code == 200, r.text
    db_session.refresh(roster)
    assert roster.keycloak_id == req.keycloak_id and roster.source == "ad"


def test_production_refuses_a_platform_tenant_id_that_is_not_a_uuid(monkeypatch):
    from app.settings import production_problems

    monkeypatch.setenv("PLATFORM_TENANT_ID", "operator")
    assert "PLATFORM_TENANT_ID is not a tenant id (UUID)" in production_problems()
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(uuid.uuid4()))
    assert not any("PLATFORM_TENANT_ID" in p for p in production_problems())
