"""Trainee registration: AD login -> registration -> instructor approval.

The property under test throughout is that **AD group membership suggests but
never grants**. A request may arrive carrying `TN-Platform-Admins`, and it still
takes an explicit approval decision to decide what role the account gets.
"""

from __future__ import annotations

import json
import uuid

import pytest
from app.auth import CurrentUser, TokenPayload, get_current_user, get_token_identity
from app.main import app as fastapi_app
from app.models import (
    Course,
    Enrollment,
    Qualification,
    RegistrationRequest,
    RegistrationStatus,
    Tenant,
    User,
    UserRole,
)
from fastapi.testclient import TestClient

TRAINEE_SUB = "ad-sub-trainee-0001"


def _identity(sub: str = TRAINEE_SUB, groups: list[str] | None = None, **over) -> TokenPayload:
    data = {
        "sub": sub,
        "email": "pte.bloggs@corp.tnrange.lab",
        "preferred_username": "pte.bloggs",
        "given_name": "Joe",
        "family_name": "Bloggs",
        "groups": groups if groups is not None else ["TN-Students"],
        # Derived from the subject: users.ad_object_guid is UNIQUE, so distinct
        # identities must carry distinct GUIDs the way real AD would.
        "ad_object_guid": str(uuid.uuid5(uuid.NAMESPACE_OID, sub)),
        "ad_distinguished_name": f"CN={sub},OU=Users,DC=corp,DC=tnrange,DC=lab",
    }
    data.update(over)
    return TokenPayload(**data)


@pytest.fixture
def tenant(db_session) -> Tenant:
    t = Tenant(id=uuid.uuid4(), name="Default Org", slug="default")
    db_session.add(t)
    db_session.flush()
    return t


@pytest.fixture
def as_trainee(client, db_session):
    """Client whose token is an AD identity with no TrueNorth account."""

    def _apply(identity: TokenPayload) -> TestClient:
        fastapi_app.dependency_overrides[get_token_identity] = lambda: identity
        return client

    yield _apply
    fastapi_app.dependency_overrides.pop(get_token_identity, None)


@pytest.fixture
def approver(db_session, tenant):
    """An instructor who can drain the approval queue."""
    row = User(
        id=uuid.uuid4(),
        keycloak_id="ad-sub-instructor",
        email="instructor@corp.tnrange.lab",
        display_name="Instructor",
        role=UserRole.instructor,
        tenant_id=tenant.id,
        source="ad",
    )
    db_session.add(row)
    db_session.flush()
    cu = CurrentUser(
        id=str(row.id),
        email=row.email,
        display_name=row.display_name,
        role=UserRole.instructor,
        tenant_id=str(tenant.id),
        keycloak_id=row.keycloak_id,
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: cu
    yield cu
    fastapi_app.dependency_overrides.pop(get_current_user, None)


# ── /auth/me: the status oracle ────────────────────────────────────────
def test_auth_me_reports_unregistered_with_prefill(as_trainee, tenant):
    c = as_trainee(_identity())
    r = c.get("/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unregistered"
    assert body["prefill"]["email"] == "pte.bloggs@corp.tnrange.lab"
    assert body["prefill"]["display_name"] == "Joe Bloggs"
    assert body["prefill"]["ad_object_guid"] == str(uuid.uuid5(uuid.NAMESPACE_OID, TRAINEE_SUB))


def test_auth_me_suggests_role_from_ad_groups(as_trainee, tenant):
    c = as_trainee(_identity(groups=["TN-Instructors"]))
    body = c.get("/auth/me").json()
    assert body["suggestions"]["role"] == "instructor"
    assert body["suggestions"]["matched_groups"] == ["TN-Instructors"]


def test_auth_me_role_suggestion_uses_highest_precedence(as_trainee, tenant):
    c = as_trainee(_identity(groups=["TN-Students", "TN-Platform-Admins", "TN-Instructors"]))
    assert c.get("/auth/me").json()["suggestions"]["role"] == "admin"


# ── Submitting a request ───────────────────────────────────────────────
def test_submit_creates_pending_request(as_trainee, db_session, tenant):
    c = as_trainee(_identity())
    r = c.post("/registration", json={"rank": "Pte", "unit": "1 CMBG", "callsign": "BLOGGS"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["rank"] == "Pte"
    # AD identity is carried across from the token, not from the request body.
    assert body["email"] == "pte.bloggs@corp.tnrange.lab"
    assert json.loads(body["ad_groups"]) == ["TN-Students"]
    assert body["suggested_role"] == "student"


def test_auth_me_reports_pending_after_submit(as_trainee, tenant):
    c = as_trainee(_identity())
    c.post("/registration", json={"rank": "Pte"})
    assert c.get("/auth/me").json()["status"] == "pending"


def test_resubmitting_while_pending_updates_rather_than_duplicates(as_trainee, db_session, tenant):
    c = as_trainee(_identity())
    first = c.post("/registration", json={"rank": "Pte"}).json()
    second = c.post("/registration", json={"rank": "Cpl", "unit": "2 CMBG"}).json()
    assert first["id"] == second["id"]
    assert second["rank"] == "Cpl"
    assert db_session.query(RegistrationRequest).count() == 1


def test_submit_rejected_when_ad_groups_not_allowed(as_trainee, tenant, monkeypatch):
    monkeypatch.setenv("REGISTRATION_ALLOWED_GROUPS", "TN-Students,TN-Instructors")
    c = as_trainee(_identity(groups=["Domain Users"]))
    r = c.post("/registration", json={})
    assert r.status_code == 403
    assert "TN-Students" in r.json()["detail"]


def test_submit_requires_an_email_claim(as_trainee, tenant):
    c = as_trainee(_identity(email=""))
    r = c.post("/registration", json={})
    assert r.status_code == 400
    assert "email" in r.json()["detail"].lower()


def test_withdraw_allows_resubmission(as_trainee, db_session, tenant):
    c = as_trainee(_identity())
    c.post("/registration", json={"rank": "Pte"})
    assert c.delete("/registration/mine").status_code == 204
    assert c.get("/auth/me").json()["status"] == "unregistered"
    assert c.post("/registration", json={"rank": "Pte"}).status_code == 201


# ── Approval: the only path that creates a trainee ─────────────────────
def _submit(as_trainee, identity=None) -> str:
    c = as_trainee(identity or _identity())
    return c.post("/registration", json={"rank": "Pte", "unit": "1 CMBG"}).json()["id"]


def test_approve_creates_user_with_approver_chosen_role(as_trainee, approver, db_session, tenant, client):
    rid = _submit(as_trainee)
    fastapi_app.dependency_overrides.pop(get_token_identity, None)

    r = client.post(
        f"/registration/requests/{rid}/approve",
        json={"role": "student", "tenant_id": str(tenant.id)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"

    created = db_session.query(User).filter(User.keycloak_id == TRAINEE_SUB).one()
    assert created.role == UserRole.student
    assert created.tenant_id == tenant.id
    assert created.source == "ad"
    assert created.rank == "Pte"
    assert created.ad_object_guid == str(uuid.uuid5(uuid.NAMESPACE_OID, TRAINEE_SUB))
    assert created.onboarding_state == "not_started"


def test_ad_admin_group_does_not_grant_admin(as_trainee, approver, db_session, tenant, client):
    """The whole point: a TN-Platform-Admins member still gets what the approver picks."""
    rid = _submit(as_trainee, _identity(groups=["TN-Platform-Admins"]))
    fastapi_app.dependency_overrides.pop(get_token_identity, None)

    req = db_session.query(RegistrationRequest).filter(RegistrationRequest.id == uuid.UUID(rid)).one()
    assert req.suggested_role == "admin"  # suggested...

    client.post(
        f"/registration/requests/{rid}/approve",
        json={"role": "student", "tenant_id": str(tenant.id)},
    )
    created = db_session.query(User).filter(User.keycloak_id == TRAINEE_SUB).one()
    assert created.role == UserRole.student  # ...but not granted


def test_approve_twice_is_rejected(as_trainee, approver, tenant, client):
    rid = _submit(as_trainee)
    fastapi_app.dependency_overrides.pop(get_token_identity, None)
    body = {"role": "student", "tenant_id": str(tenant.id)}
    assert client.post(f"/registration/requests/{rid}/approve", json=body).status_code == 200
    assert client.post(f"/registration/requests/{rid}/approve", json=body).status_code == 409


def test_approve_enrolls_on_requested_qualification(as_trainee, approver, db_session, tenant, client):
    qual = Qualification(id=uuid.uuid4(), qsp_code="ALJQ", nqual="NQ-ALJQ", title="Cyber Op DP1")
    db_session.add(qual)
    course = Course(id=uuid.uuid4(), name="C101", qualification_id=qual.id, tenant_id=tenant.id)
    db_session.add(course)
    db_session.flush()

    c = as_trainee(_identity())
    rid = c.post("/registration", json={"requested_qualification_id": str(qual.id)}).json()["id"]
    fastapi_app.dependency_overrides.pop(get_token_identity, None)

    client.post(
        f"/registration/requests/{rid}/approve",
        json={"role": "student", "tenant_id": str(tenant.id)},
    )
    created = db_session.query(User).filter(User.keycloak_id == TRAINEE_SUB).one()
    assert db_session.query(Enrollment).filter(Enrollment.user_id == created.id).count() == 1


def test_reject_records_reason_and_creates_no_user(as_trainee, approver, db_session, tenant, client):
    rid = _submit(as_trainee)
    fastapi_app.dependency_overrides.pop(get_token_identity, None)

    r = client.post(f"/registration/requests/{rid}/reject", json={"reason": "Not on this course"})
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    assert r.json()["decision_reason"] == "Not on this course"
    assert db_session.query(User).filter(User.keycloak_id == TRAINEE_SUB).count() == 0


def test_rejected_applicant_sees_the_reason(as_trainee, approver, tenant, client):
    identity = _identity()
    rid = _submit(as_trainee, identity)
    fastapi_app.dependency_overrides.pop(get_token_identity, None)
    client.post(f"/registration/requests/{rid}/reject", json={"reason": "Wrong cohort"})

    c = as_trainee(identity)
    body = c.get("/auth/me").json()
    assert body["status"] == "rejected"
    assert body["request"]["decision_reason"] == "Wrong cohort"


def test_queue_lists_pending_requests(as_trainee, approver, tenant, client):
    _submit(as_trainee)
    fastapi_app.dependency_overrides.pop(get_token_identity, None)
    rows = client.get("/registration/requests?status=pending").json()
    assert len(rows) == 1
    assert rows[0]["email"] == "pte.bloggs@corp.tnrange.lab"


def test_bulk_approve_processes_a_cohort(as_trainee, approver, db_session, tenant, client):
    ids = []
    for n in range(3):
        c = as_trainee(_identity(sub=f"ad-sub-{n}", email=f"trainee{n}@corp.tnrange.lab"))
        ids.append(c.post("/registration", json={"rank": "Pte"}).json()["id"])
    fastapi_app.dependency_overrides.pop(get_token_identity, None)

    r = client.post(
        "/registration/requests/bulk-approve",
        json={"request_ids": ids, "role": "student", "tenant_id": str(tenant.id)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["approved"] == 3
    assert r.json()["failed"] == 0
    assert db_session.query(User).filter(User.role == UserRole.student).count() == 3


def test_bulk_approve_reports_per_item_failure(approver, tenant, client):
    missing = str(uuid.uuid4())
    r = client.post(
        "/registration/requests/bulk-approve",
        json={"request_ids": [missing], "role": "student", "tenant_id": str(tenant.id)},
    ).json()
    assert r["approved"] == 0 and r["failed"] == 1
    assert r["results"][0]["error"] == "not found"


# ── Pre-provisioned roster adoption ────────────────────────────────────
def test_approval_adopts_a_csv_imported_row_instead_of_duplicating(
    as_trainee, approver, db_session, tenant, client
):
    pre = User(
        id=uuid.uuid4(),
        keycloak_id="placeholder-import-1",
        email="pte.bloggs@corp.tnrange.lab",
        display_name="Joe Bloggs",
        role=UserRole.student,
        tenant_id=tenant.id,
        source="csv_import",
    )
    db_session.add(pre)
    db_session.flush()

    rid = _submit(as_trainee)
    fastapi_app.dependency_overrides.pop(get_token_identity, None)
    client.post(
        f"/registration/requests/{rid}/approve",
        json={"role": "student", "tenant_id": str(tenant.id)},
    )

    rows = db_session.query(User).filter(User.email == "pte.bloggs@corp.tnrange.lab").all()
    assert len(rows) == 1, "adoption should not create a second row for the same person"
    assert rows[0].id == pre.id
    assert rows[0].keycloak_id == TRAINEE_SUB
    assert rows[0].source == "ad"


def test_approval_refuses_to_adopt_a_row_from_another_idp(
    as_trainee, approver, db_session, tenant, client
):
    """Adoption is only safe for sources that could not choose their own email."""
    other = User(
        id=uuid.uuid4(),
        keycloak_id="some-other-idp-sub",
        email="pte.bloggs@corp.tnrange.lab",
        display_name="Someone Else",
        role=UserRole.admin,
        tenant_id=tenant.id,
        source="ad",
    )
    db_session.add(other)
    db_session.flush()

    rid = _submit(as_trainee)
    fastapi_app.dependency_overrides.pop(get_token_identity, None)
    r = client.post(
        f"/registration/requests/{rid}/approve",
        json={"role": "student", "tenant_id": str(tenant.id)},
    )
    assert r.status_code == 409
    assert db_session.query(User).filter(User.keycloak_id == "some-other-idp-sub").one().role == UserRole.admin


# ── Status enum sanity ─────────────────────────────────────────────────
def test_registration_status_values():
    assert [s.value for s in RegistrationStatus] == ["pending", "approved", "rejected", "withdrawn"]
