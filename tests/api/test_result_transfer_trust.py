from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.result_transfer.contract import ResultTransferError, generate_recipient_keypair, generate_signing_keypair
from app.result_transfer.service import import_subject_credential_from_trust, issue_subject_credential
from app.result_transfer.trust import enroll_trusted_key, resolve_trusted_key, retire_trusted_key

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def test_unclassified_enrolls_rotatable_remote_public_keys(db_session, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")
    monkeypatch.setenv("TN_ENCLAVE_ID", "unclass")
    _, first = generate_signing_keypair()
    _, second = generate_signing_keypair()

    row, created = enroll_trusted_key(
        db_session,
        purpose="subject-signing",
        remote_enclave="secret",
        public_key=first,
        not_before=NOW,
    )
    duplicate, created_again = enroll_trusted_key(
        db_session,
        purpose="subject-signing",
        remote_enclave="secret",
        public_key=first,
        not_before=NOW,
    )
    next_row, _ = enroll_trusted_key(
        db_session,
        purpose="subject-signing",
        remote_enclave="secret",
        public_key=second,
        not_before=NOW,
    )

    assert created is True and created_again is False and duplicate.id == row.id
    assert resolve_trusted_key(
        db_session,
        purpose="subject-signing",
        remote_enclave="secret",
        key_id=row.key_id,
        at=NOW,
    ) == first
    assert next_row.key_id != row.key_id
    retire_trusted_key(
        db_session,
        purpose="subject-signing",
        remote_enclave="secret",
        key_id=row.key_id,
        at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(ResultTransferError, match="no active"):
        resolve_trusted_key(
            db_session,
            purpose="subject-signing",
            remote_enclave="secret",
            key_id=row.key_id,
            at=NOW + timedelta(minutes=2),
        )


def test_trust_purposes_are_direction_and_classification_bound(db_session, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")
    monkeypatch.setenv("TN_ENCLAVE_ID", "unclass")
    _, signing_public = generate_signing_keypair()
    with pytest.raises(ResultTransferError, match="cannot be enrolled"):
        enroll_trusted_key(
            db_session,
            purpose="result-signing",
            remote_enclave="secret",
            public_key=signing_public,
            not_before=NOW,
        )
    with pytest.raises(ResultTransferError, match="remote enclave"):
        enroll_trusted_key(
            db_session,
            purpose="subject-signing",
            remote_enclave="unclass",
            public_key=signing_public,
            not_before=NOW,
        )

    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "secret")
    monkeypatch.setenv("TN_ENCLAVE_ID", "secret")
    row, _ = enroll_trusted_key(
        db_session,
        purpose="result-signing",
        remote_enclave="unclass",
        public_key=signing_public,
        not_before=NOW,
    )
    assert row.public_key == signing_public
    _, recipient_public = generate_recipient_keypair()
    with pytest.raises(ResultTransferError, match="cannot be enrolled"):
        enroll_trusted_key(
            db_session,
            purpose="result-recipient",
            remote_enclave="unclass",
            public_key=recipient_public,
            not_before=NOW,
        )


def test_trust_key_validity_window_is_enforced(db_session, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")
    monkeypatch.setenv("TN_ENCLAVE_ID", "unclass")
    _, recipient_public = generate_recipient_keypair()
    row, _ = enroll_trusted_key(
        db_session,
        purpose="result-recipient",
        remote_enclave="secret",
        public_key=recipient_public,
        not_before=NOW,
        not_after=NOW + timedelta(hours=1),
    )
    with pytest.raises(ResultTransferError, match="validity window"):
        resolve_trusted_key(
            db_session,
            purpose="result-recipient",
            remote_enclave="secret",
            key_id=row.key_id,
            at=NOW + timedelta(hours=1),
        )


def test_subject_import_uses_exact_enrolled_signer(db_session, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "secret")
    monkeypatch.setenv("TN_ENCLAVE_ID", "secret")
    private, public = generate_signing_keypair()
    credential, _ = issue_subject_credential(
        db_session,
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        destination_enclave="unclass",
        signing_private_key=private,
    )
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")
    monkeypatch.setenv("TN_ENCLAVE_ID", "unclass")
    with pytest.raises(ResultTransferError, match="no active trusted key"):
        import_subject_credential_from_trust(db_session, credential, source_enclave="secret")
    enroll_trusted_key(
        db_session,
        purpose="subject-signing",
        remote_enclave="secret",
        public_key=public,
        not_before=NOW - timedelta(minutes=1),
    )

    imported, created = import_subject_credential_from_trust(
        db_session,
        credential,
        source_enclave="secret",
    )

    assert created is True and imported.credential_id == uuid.UUID(credential["credential_id"])
