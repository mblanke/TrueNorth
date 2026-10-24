from __future__ import annotations

import uuid

import pytest
from app.models import UserRole
from app.result_transfer.contract import ResultTransferError, generate_signing_keypair
from app.result_transfer.pseudonymous import assign_account
from app.result_transfer.service import import_subject_credential, issue_subject_credential

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER = TENANT


def _available(db, monkeypatch):
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "secret")
    monkeypatch.setenv("TN_ENCLAVE_ID", "secret")
    private, public = generate_signing_keypair()
    package, _ = issue_subject_credential(
        db, tenant_id=TENANT, user_id=USER, destination_enclave="unclass", signing_private_key=private
    )
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")
    monkeypatch.setenv("TN_ENCLAVE_ID", "unclass")
    row, _ = import_subject_credential(
        db, package, trusted_signing_public_key=public, source_enclave="secret"
    )
    return row


def test_assignment_creates_only_an_identity_free_student(db_session, monkeypatch) -> None:
    available = _available(db_session, monkeypatch)
    row, user = assign_account(
        db_session,
        credential_id=available.credential_id,
        tenant_id=TENANT,
        keycloak_subject=str(uuid.uuid4()),
    )

    assert row.state == "assigned" and row.user_id == user.id and row.assigned_at is not None
    assert user.role == UserRole.student and user.source == "subject"
    assert user.email.endswith("@subjects.invalid") and user.display_name.startswith("Learner ")
    assert all(
        value is None
        for value in (
            user.first_name,
            user.last_name,
            user.rank,
            user.service_branch,
            user.unit,
            user.callsign,
            user.nation_id,
            user.ad_object_guid,
            user.ad_distinguished_name,
        )
    )
    assert str(available.opaque_subject_id) not in user.email + user.display_name


def test_assignment_is_single_use_and_unclassified_only(db_session, monkeypatch) -> None:
    available = _available(db_session, monkeypatch)
    assign_account(
        db_session,
        credential_id=available.credential_id,
        tenant_id=TENANT,
        keycloak_subject=str(uuid.uuid4()),
    )
    with pytest.raises(ResultTransferError, match="available"):
        assign_account(
            db_session,
            credential_id=available.credential_id,
            tenant_id=TENANT,
            keycloak_subject=str(uuid.uuid4()),
        )
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "secret")
    with pytest.raises(ResultTransferError, match="only on Unclassified"):
        assign_account(
            db_session,
            credential_id=uuid.uuid4(),
            tenant_id=TENANT,
            keycloak_subject=str(uuid.uuid4()),
        )


def test_assignment_refuses_subject_token_reuse_and_foreign_tenant(db_session, monkeypatch) -> None:
    available = _available(db_session, monkeypatch)

    with pytest.raises(ResultTransferError, match="must not reuse"):
        assign_account(
            db_session,
            credential_id=available.credential_id,
            tenant_id=TENANT,
            keycloak_subject=str(available.opaque_subject_id),
        )
    with pytest.raises(ResultTransferError, match="in this tenant"):
        assign_account(
            db_session,
            credential_id=available.credential_id,
            tenant_id=uuid.uuid4(),
            keycloak_subject=str(uuid.uuid4()),
        )
