"""app.bootstrap_admin.ensure_admin adopts by email only what is safe to adopt (M2).

80-seed makes the nominated Keycloak account TrueNorth's administrator. When no row has
that Keycloak subject it falls back to the email: right for a roster row or a stale admin
row from an earlier realm, wrong for someone else's instructor or range_ops account.
"""

from __future__ import annotations

import uuid

import pytest
from _shared import real_tenant
from app.bootstrap_admin import AdoptionRefusedError, ensure_admin
from app.models import User, UserRole


def _row(db, tenant, role: UserRole, email: str, source: str = "local") -> User:
    row = User(id=uuid.uuid4(), keycloak_id=f"old-{uuid.uuid4().hex[:8]}", email=email, display_name=email,
               role=role, tenant_id=tenant.id, source=source)
    db.add(row)
    db.flush()
    return row


def _kc(email: str) -> dict:
    return {"id": str(uuid.uuid4()), "email": email, "username": email, "firstName": "Boot", "lastName": "Admin"}


@pytest.mark.parametrize("role", [UserRole.instructor, UserRole.range_ops])
def test_refuses_another_staff_account_with_the_same_email(db_session, role):
    tenant = real_tenant(db_session)
    staff = _row(db_session, tenant, role, "lead@unit.example")
    before = (staff.keycloak_id, staff.role)

    with pytest.raises(AdoptionRefusedError):
        ensure_admin(db_session, tenant, _kc("lead@unit.example"))

    db_session.refresh(staff)
    assert (staff.keycloak_id, staff.role) == before


@pytest.mark.parametrize("role,source", [(UserRole.student, "csv_import"), (UserRole.admin, "ad")])
def test_still_adopts_a_roster_row_or_a_stale_admin_row(db_session, role, source):
    tenant = real_tenant(db_session)
    row = _row(db_session, tenant, role, "boss@unit.example", source)
    kc = _kc("boss@unit.example")

    adopted = ensure_admin(db_session, tenant, kc)

    assert adopted.id == row.id and adopted.keycloak_id == kc["id"] and adopted.role == UserRole.admin


def test_creates_the_admin_when_nothing_matches(db_session):
    tenant = real_tenant(db_session)
    kc = _kc("new@unit.example")
    made = ensure_admin(db_session, tenant, kc)
    assert made.keycloak_id == kc["id"] and made.role == UserRole.admin
