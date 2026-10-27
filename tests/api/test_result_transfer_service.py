from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from app.course_releases.models import ACCEPTED, CourseRelease, CourseReleaseBlob
from app.models import Course, Tenant, User, UserRole
from app.result_transfer.contract import (
    ResultClaim,
    ResultTransferError,
    create_package,
    decrypt_package,
    generate_recipient_keypair,
    generate_signing_keypair,
)
from app.result_transfer.provisioning import verify_subject_credential
from app.result_transfer.receipt import ResultReceipt, create_receipt
from app.result_transfer.service import (
    apply_receipt,
    bind_subject,
    import_result,
    import_subject_credential,
    issue_subject_credential,
    queue_result,
)

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER = TENANT


def release(db, *, tenant_id: uuid.UUID = TENANT, marker: str = "b") -> CourseRelease:
    course = Course(name="Cross-enclave assessment", tenant_id=tenant_id)
    db.add(course)
    db.flush()
    digest = marker * 64
    blob = CourseReleaseBlob(sha256=digest, size=2, data=b"{}")
    db.add(blob)
    db.flush()
    row = CourseRelease(
        tenant_id=tenant_id,
        course_id=course.id,
        catalogue_code="COTE-101",
        arc2_code="COTE-101",
        run_id=f"result-transfer-{marker}",
        slug=f"cross-enclave-{marker}",
        title="Cross-enclave assessment",
        version=1,
        release_digest=digest,
        learner_digest=marker * 64,
        platform_digest=marker * 64,
        instructor_digest=marker * 64,
        blob_sha256=digest,
        meta="{}",
        state=ACCEPTED,
    )
    db.add(row)
    db.flush()
    return row


def claim(release_id: uuid.UUID, token: uuid.UUID, attempt: uuid.UUID, **changes) -> ResultClaim:
    values = {
        "opaque_subject_id": token,
        "tenant_id": TENANT,
        "course_release_id": release_id,
        "assessment_id": "final",
        "attempt_id": attempt,
        "raw_score": 88,
        "max_score": 100,
        "outcome": "pass",
        "scorer_version": "scenario-v4",
        "evidence_sha256": ["a" * 64],
        "occurred_at": datetime(2026, 10, 23, 12, tzinfo=UTC),
    }
    values.update(changes)
    return ResultClaim.model_validate(values)


def test_unclassified_outbox_persists_no_readable_grade(db_session, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")
    monkeypatch.setenv("TN_ENCLAVE_ID", "unclass")
    course_release = release(db_session)
    signing_private, _ = generate_signing_keypair()
    _, recipient_public = generate_recipient_keypair()

    queued = queue_result(
        db_session,
        claim(course_release.id, uuid.uuid4(), uuid.uuid4()),
        source_enclave="unclass",
        destination_enclave="secret",
        signing_private_key=signing_private,
        recipient_public_key=recipient_public,
    )

    stored = json.loads(queued.row.package_json)
    assert queued.row.outcome == "pass"
    assert queued.row.state == "pending"
    assert "raw_score" not in queued.row.package_json
    assert "max_score" not in queued.row.package_json
    assert "tenant_id" not in queued.row.package_json
    assert set(stored["projection"]) == {
        "opaque_subject_id",
        "course_release_id",
        "assessment_id",
        "attempt_id",
        "outcome",
    }


def test_subject_binding_is_refused_on_unclassified(db_session, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")
    monkeypatch.setenv("TN_ENCLAVE_ID", "unclass")

    with pytest.raises(ResultTransferError, match="Secret-side"):
        bind_subject(
            db_session,
            tenant_id=TENANT,
            source_enclave="unclass",
            opaque_subject_id=uuid.uuid4(),
            user_id=USER,
        )


def test_secret_issues_random_subject_credential_and_keeps_mapping(db_session, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "secret")
    monkeypatch.setenv("TN_ENCLAVE_ID", "secret")
    signing_private, signing_public = generate_signing_keypair()

    package, binding = issue_subject_credential(
        db_session,
        tenant_id=TENANT,
        user_id=USER,
        destination_enclave="unclass",
        signing_private_key=signing_private,
    )

    assert binding.user_id == USER
    assert package["subject"]["opaque_subject_id"] == str(binding.opaque_subject_id)
    assert package["destination_enclave"] == "unclass"
    verified = verify_subject_credential(
        package,
        trusted_signing_public_key=signing_public,
        source_enclave="secret",
        destination_enclave="unclass",
    )
    assert verified.opaque_subject_id == binding.opaque_subject_id


def test_unclassified_imports_subject_credential_idempotently(db_session, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "secret")
    monkeypatch.setenv("TN_ENCLAVE_ID", "secret")
    signing_private, signing_public = generate_signing_keypair()
    package, binding = issue_subject_credential(
        db_session,
        tenant_id=TENANT,
        user_id=USER,
        destination_enclave="unclass",
        signing_private_key=signing_private,
    )
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")
    monkeypatch.setenv("TN_ENCLAVE_ID", "unclass")

    imported, created = import_subject_credential(
        db_session, package, trusted_signing_public_key=signing_public, source_enclave="secret"
    )
    duplicate, created_again = import_subject_credential(
        db_session, package, trusted_signing_public_key=signing_public, source_enclave="secret"
    )

    assert created is True and created_again is False
    assert duplicate.credential_id == imported.credential_id
    assert imported.opaque_subject_id == binding.opaque_subject_id
    assert imported.state == "available"
    assert not any(field in imported.credential_json for field in ("email", "display_name", "grade", "score"))


def test_unclassified_outbox_refuses_a_forged_source_route(db_session, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")
    monkeypatch.setenv("TN_ENCLAVE_ID", "unclass")
    course_release = release(db_session)
    signing_private, _ = generate_signing_keypair()
    _, recipient_public = generate_recipient_keypair()

    with pytest.raises(ResultTransferError, match="TN_ENCLAVE_ID"):
        queue_result(
            db_session,
            claim(course_release.id, uuid.uuid4(), uuid.uuid4()),
            source_enclave="different-unclass",
            destination_enclave="secret",
            signing_private_key=signing_private,
            recipient_public_key=recipient_public,
        )


def test_secret_subject_and_release_checks_never_cross_tenants(db_session, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "secret")
    monkeypatch.setenv("TN_ENCLAVE_ID", "secret")
    other_tenant, other_user = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=other_tenant, name="Other enclave tenant", slug=f"other-{other_tenant.hex[:8]}"))
    db_session.flush()
    db_session.add(
        User(
            id=other_user,
            keycloak_id=f"other-{other_user}",
            email=f"{other_user}@example.test",
            display_name="Other learner",
            role=UserRole.student,
            tenant_id=other_tenant,
        )
    )
    db_session.flush()
    other_release = release(db_session, tenant_id=other_tenant, marker="9")

    with pytest.raises(ResultTransferError, match="does not belong"):
        bind_subject(
            db_session,
            tenant_id=TENANT,
            source_enclave="unclass",
            opaque_subject_id=uuid.uuid4(),
            user_id=other_user,
        )

    token = uuid.uuid4()
    bind_subject(
        db_session,
        tenant_id=TENANT,
        source_enclave="unclass",
        opaque_subject_id=token,
        user_id=USER,
    )
    signing_private, signing_public = generate_signing_keypair()
    recipient_private, recipient_public = generate_recipient_keypair()
    package, _ = create_package(
        claim(other_release.id, token, uuid.uuid4()),
        source_enclave="unclass",
        destination_enclave="secret",
        signing_private_key=signing_private,
        recipient_public_key=recipient_public,
    )
    decrypted = decrypt_package(
        package,
        trusted_signing_public_key=signing_public,
        recipient_private_key=recipient_private,
        destination_enclave="secret",
    )

    with pytest.raises(ResultTransferError, match="not approved"):
        import_result(db_session, decrypted, package_sha256="8" * 64)


def test_signed_secret_receipt_advances_only_matching_outbox_row(db_session, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "unclassified")
    monkeypatch.setenv("TN_ENCLAVE_ID", "unclass")
    course_release = release(db_session)
    low_private, _ = generate_signing_keypair()
    high_private, high_public = generate_signing_keypair()
    _, recipient_public = generate_recipient_keypair()
    queued = queue_result(
        db_session,
        claim(course_release.id, uuid.uuid4(), uuid.uuid4()),
        source_enclave="unclass",
        destination_enclave="secret",
        signing_private_key=low_private,
        recipient_public_key=recipient_public,
    )
    signed = create_receipt(
        ResultReceipt(
            package_id=queued.row.package_id,
            package_sha256=queued.row.package_sha256,
            source_enclave="secret",
            destination_enclave="unclass",
            status="accepted",
            created_at=datetime.now(UTC),
        ),
        signing_private_key=high_private,
    )

    row = apply_receipt(
        db_session,
        signed,
        trusted_signing_public_key=high_public,
        source_enclave="secret",
        destination_enclave="unclass",
    )

    assert row.state == "acknowledged"
    assert row.acknowledged_at is not None


def test_secret_import_maps_subject_and_is_idempotent(db_session, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "secret")
    monkeypatch.setenv("TN_ENCLAVE_ID", "secret")
    course_release = release(db_session)
    token, attempt = uuid.uuid4(), uuid.uuid4()
    bind_subject(
        db_session,
        tenant_id=TENANT,
        source_enclave="unclass",
        opaque_subject_id=token,
        user_id=USER,
    )
    signing_private, signing_public = generate_signing_keypair()
    recipient_private, recipient_public = generate_recipient_keypair()
    package, _ = create_package(
        claim(course_release.id, token, attempt),
        source_enclave="unclass",
        destination_enclave="secret",
        signing_private_key=signing_private,
        recipient_public_key=recipient_public,
    )
    decrypted = decrypt_package(
        package,
        trusted_signing_public_key=signing_public,
        recipient_private_key=recipient_private,
        destination_enclave="secret",
    )

    imported, created = import_result(db_session, decrypted, package_sha256="f" * 64)
    duplicate, created_again = import_result(db_session, decrypted, package_sha256="f" * 64)

    assert created is True
    assert created_again is False
    assert duplicate.package_id == imported.package_id
    assert imported.user_id == USER
    assert imported.raw_score == 88
    assert imported.max_score == 100


def test_secret_import_refuses_a_package_for_another_enclave(db_session, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "secret")
    monkeypatch.setenv("TN_ENCLAVE_ID", "secret-east")
    signing_private, signing_public = generate_signing_keypair()
    recipient_private, recipient_public = generate_recipient_keypair()
    course_release = release(db_session)
    package, _ = create_package(
        claim(course_release.id, uuid.uuid4(), uuid.uuid4()),
        source_enclave="unclass",
        destination_enclave="secret-west",
        signing_private_key=signing_private,
        recipient_public_key=recipient_public,
    )
    decrypted = decrypt_package(
        package,
        trusted_signing_public_key=signing_public,
        recipient_private_key=recipient_private,
        destination_enclave="secret-west",
    )

    with pytest.raises(ResultTransferError, match="TN_ENCLAVE_ID"):
        import_result(db_session, decrypted, package_sha256="9" * 64)


def test_secret_import_requires_explicit_linear_correction(db_session, monkeypatch) -> None:
    monkeypatch.setenv("TN_CLASSIFICATION_LEVEL", "secret")
    monkeypatch.setenv("TN_ENCLAVE_ID", "secret")
    course_release = release(db_session)
    token, attempt = uuid.uuid4(), uuid.uuid4()
    bind_subject(
        db_session,
        tenant_id=TENANT,
        source_enclave="unclass",
        opaque_subject_id=token,
        user_id=USER,
    )
    signing_private, signing_public = generate_signing_keypair()
    recipient_private, recipient_public = generate_recipient_keypair()

    def decrypt(value: ResultClaim, package_id: uuid.UUID):
        package, _ = create_package(
            value,
            source_enclave="unclass",
            destination_enclave="secret",
            signing_private_key=signing_private,
            recipient_public_key=recipient_public,
            package_id=package_id,
        )
        return decrypt_package(
            package,
            trusted_signing_public_key=signing_public,
            recipient_private_key=recipient_private,
            destination_enclave="secret",
        )

    first_id = uuid.uuid4()
    first = decrypt(claim(course_release.id, token, attempt), first_id)
    import_result(db_session, first, package_sha256="1" * 64)

    unrelated = decrypt(claim(course_release.id, token, attempt, raw_score=90), uuid.uuid4())
    with pytest.raises(ResultTransferError, match="must declare supersedes"):
        import_result(db_session, unrelated, package_sha256="2" * 64)

    corrected = decrypt(
        claim(course_release.id, token, attempt, raw_score=90, supersedes_package_id=first_id),
        uuid.uuid4(),
    )
    record, created = import_result(db_session, corrected, package_sha256="3" * 64)

    assert created is True
    assert record.raw_score == 90
    assert record.supersedes_package_id == first_id
