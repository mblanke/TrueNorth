"""Bulk cohort intake: CSV roster pre-provisioning and group-based enrolment.

These are the two paths that let an instructor prepare a course before anyone
signs in. Both converge on the same approval flow — a roster row is a
placeholder that registration approval later *adopts*, not a shortcut around it.
"""

from __future__ import annotations

import io
import json
import uuid

import pytest
from app.models import (
    Course,
    Enrollment,
    LearningPath,
    SecurityGroup,
    SecurityGroupMembership,
    Tenant,
    User,
    UserRole,
)

TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def tenant(db_session) -> Tenant:
    """The tenant AUTH_DISABLED's dev admin belongs to."""
    existing = db_session.query(Tenant).filter(Tenant.id == TENANT_ID).first()
    if existing:
        return existing
    t = Tenant(id=TENANT_ID, name="Default Org", slug="default")
    db_session.add(t)
    db_session.flush()
    return t


def _csv(rows: str) -> dict:
    return {"file": ("roster.csv", io.BytesIO(rows.encode()), "text/csv")}


# ── CSV roster import ──────────────────────────────────────────────────
def test_import_creates_placeholder_rows(client, db_session, tenant):
    r = client.post(
        "/users/import-csv",
        files=_csv(
            "email,display_name,rank,unit\n"
            "pte.bloggs@corp.tnrange.lab,Joe Bloggs,Pte,1 CMBG\n"
            "cpl.smith@corp.tnrange.lab,Ann Smith,Cpl,2 CMBG\n"
        ),
    )
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 2

    row = db_session.query(User).filter(User.email == "pte.bloggs@corp.tnrange.lab").one()
    assert row.source == "csv_import"
    assert row.rank == "Pte"
    assert row.role == UserRole.student
    # A placeholder, not a real directory subject — approval replaces it.
    assert row.keycloak_id.startswith("csv-import:")


def test_import_is_idempotent_on_email(client, db_session, tenant):
    body = "email,display_name,rank\npte.bloggs@corp.tnrange.lab,Joe Bloggs,Pte\n"
    first = client.post("/users/import-csv", files=_csv(body)).json()
    second = client.post(
        "/users/import-csv",
        files=_csv("email,display_name,rank\npte.bloggs@corp.tnrange.lab,Joe Bloggs,Cpl\n"),
    ).json()

    assert first["created"] == 1
    assert second["created"] == 0 and second["updated"] == 1
    assert db_session.query(User).filter(User.email == "pte.bloggs@corp.tnrange.lab").count() == 1
    assert db_session.query(User).filter(User.email == "pte.bloggs@corp.tnrange.lab").one().rank == "Cpl"


def test_import_never_modifies_a_directory_linked_account(client, db_session, tenant):
    """A roster upload must not be able to re-point or downgrade a live user."""
    live = User(
        id=uuid.uuid4(),
        keycloak_id="real-ad-subject",
        email="maj.reid@corp.tnrange.lab",
        display_name="Maj Reid",
        role=UserRole.admin,
        tenant_id=tenant.id,
        source="ad",
    )
    db_session.add(live)
    db_session.flush()

    r = client.post(
        "/users/import-csv",
        files=_csv("email,display_name,role\nmaj.reid@corp.tnrange.lab,Someone Else,student\n"),
    ).json()

    assert r["skipped"] == 1
    assert "already linked" in r["errors"][0]["error"]
    db_session.refresh(live)
    assert live.role == UserRole.admin
    assert live.keycloak_id == "real-ad-subject"


def test_import_dry_run_writes_nothing(client, db_session, tenant):
    r = client.post(
        "/users/import-csv?dry_run=true",
        files=_csv("email,display_name\ndry@corp.tnrange.lab,Dry Run\n"),
    ).json()
    assert r["dry_run"] is True and r["created"] == 1
    assert db_session.query(User).filter(User.email == "dry@corp.tnrange.lab").count() == 0


def test_import_rejects_a_missing_email_column(client, tenant):
    r = client.post("/users/import-csv", files=_csv("name,rank\nJoe,Pte\n"))
    assert r.status_code == 422
    assert "email" in r.json()["detail"]


def test_import_reports_a_bad_role_per_row(client, db_session, tenant):
    r = client.post(
        "/users/import-csv",
        files=_csv(
            "email,role\n"
            "good@corp.tnrange.lab,student\n"
            "bad@corp.tnrange.lab,wizard\n"
        ),
    ).json()
    assert r["created"] == 1
    assert any("wizard" in e["error"] for e in r["errors"])


# ── Group-based cohort enrolment ───────────────────────────────────────
def test_assign_learning_path_to_a_security_group(client, db_session, tenant):
    course = Course(id=uuid.uuid4(), name="C101", tenant_id=tenant.id)
    db_session.add(course)
    db_session.flush()
    path = LearningPath(
        id=uuid.uuid4(),
        name="DP1",
        tenant_id=tenant.id,
        course_ids=json.dumps([str(course.id)]),
    )
    group = SecurityGroup(id=uuid.uuid4(), name="TN-Students", slug="tn-students", tenant_id=tenant.id)
    db_session.add_all([path, group])
    db_session.flush()

    members = []
    for n in range(3):
        u = User(
            id=uuid.uuid4(),
            keycloak_id=f"kc-{n}",
            email=f"t{n}@corp.tnrange.lab",
            display_name=f"Trainee {n}",
            role=UserRole.student,
            tenant_id=tenant.id,
            source="ad",
        )
        db_session.add(u)
        members.append(u)
    db_session.flush()
    for u in members:
        db_session.add(SecurityGroupMembership(id=uuid.uuid4(), user_id=u.id, group_id=group.id))
    db_session.flush()

    r = client.post(
        f"/learning-paths/{path.id}/assign-group", json={"group_id": str(group.id)}
    )
    assert r.status_code == 200, r.text
    assert r.json()["members"] == 3
    assert r.json()["enrolled"] == 3
    assert db_session.query(Enrollment).count() == 3


def test_assign_group_is_idempotent(client, db_session, tenant):
    course = Course(id=uuid.uuid4(), name="C102", tenant_id=tenant.id)
    db_session.add(course)
    db_session.flush()
    path = LearningPath(
        id=uuid.uuid4(), name="DP1", tenant_id=tenant.id, course_ids=json.dumps([str(course.id)])
    )
    group = SecurityGroup(id=uuid.uuid4(), name="TN-Students", slug="tn-students", tenant_id=tenant.id)
    user = User(
        id=uuid.uuid4(),
        keycloak_id="kc-solo",
        email="solo@corp.tnrange.lab",
        display_name="Solo",
        role=UserRole.student,
        tenant_id=tenant.id,
        source="ad",
    )
    db_session.add_all([path, group, user])
    db_session.flush()
    db_session.add(SecurityGroupMembership(id=uuid.uuid4(), user_id=user.id, group_id=group.id))
    db_session.flush()

    body = {"group_id": str(group.id)}
    client.post(f"/learning-paths/{path.id}/assign-group", json=body)
    client.post(f"/learning-paths/{path.id}/assign-group", json=body)
    assert db_session.query(Enrollment).filter(Enrollment.user_id == user.id).count() == 1


def test_assign_group_reports_an_empty_group_rather_than_failing(client, db_session, tenant):
    path = LearningPath(id=uuid.uuid4(), name="DP1", tenant_id=tenant.id, course_ids="[]")
    group = SecurityGroup(id=uuid.uuid4(), name="TN-Empty", slug="tn-empty", tenant_id=tenant.id)
    db_session.add_all([path, group])
    db_session.flush()

    r = client.post(
        f"/learning-paths/{path.id}/assign-group", json={"group_id": str(group.id)}
    )
    assert r.status_code == 200
    assert r.json()["members"] == 0
    assert "AD sync" in r.json()["note"]


def test_assign_group_404s_on_a_foreign_path(client, db_session, tenant):
    other = Tenant(id=uuid.uuid4(), name="Other", slug="other")
    db_session.add(other)
    db_session.flush()
    path = LearningPath(id=uuid.uuid4(), name="Theirs", tenant_id=other.id, course_ids="[]")
    group = SecurityGroup(id=uuid.uuid4(), name="G", slug="g", tenant_id=tenant.id)
    db_session.add_all([path, group])
    db_session.flush()

    r = client.post(
        f"/learning-paths/{path.id}/assign-group", json={"group_id": str(group.id)}
    )
    assert r.status_code == 404
