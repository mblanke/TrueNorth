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
