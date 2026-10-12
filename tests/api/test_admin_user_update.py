"""PATCH /users/{id} changes only the fields it is given.

Its fields are query parameters (``UserUpdateIn`` as a dependency, as the contract says).
Every field the caller left out used to arrive as None and be written: a PATCH that only
reactivated a user nulled ``display_name`` and failed (500, NOT NULL), and one that set a
role cleared the rest of the profile. Found by the acceptance suite, which reactivates the
users it made on its previous run (scripts/staging_acceptance.py ``_acceptance_user``).
"""

from __future__ import annotations

import uuid

from app.models import User, UserRole
from test_course_publications import DEV_TENANT


def _user(db, **kw) -> User:
    u = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email=f"{uuid.uuid4().hex[:8]}@example.test",
             display_name="Kept Name", rank="Cpl", timezone="America/Toronto", role=UserRole.student,
             tenant_id=uuid.UUID(str(DEV_TENANT)), **kw)
    db.add(u)
    db.commit()
    return u


def test_reactivating_a_user_changes_nothing_else(client, db_session):
    u = _user(db_session, is_active=False)
    r = client.patch(f"/users/{u.id}", params={"is_active": "true", "role": "student"})
    assert r.status_code == 200, r.text
    db_session.refresh(u)
    assert u.is_active is True
    assert (u.display_name, u.rank, u.timezone) == ("Kept Name", "Cpl", "America/Toronto")


def test_one_field_changes_one_field(client, db_session):
    u = _user(db_session)
    assert client.patch(f"/users/{u.id}", params={"rank": "Sgt"}).status_code == 200
    db_session.refresh(u)
    assert (u.rank, u.display_name, u.timezone, u.role) == ("Sgt", "Kept Name", "America/Toronto", UserRole.student)
