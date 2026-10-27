from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime

import pytest
from app.result_transfer.contract import (
    ResultClaim,
    ResultTransferError,
    assert_public_package_minimal,
    create_package,
    decrypt_package,
    generate_recipient_keypair,
    generate_signing_keypair,
    verify_package,
)
from pydantic import ValidationError


@pytest.fixture
def signing_keys() -> tuple[str, str]:
    return generate_signing_keypair()


@pytest.fixture
def recipient_keys() -> tuple[str, str]:
    return generate_recipient_keypair()


@pytest.fixture
def claim() -> ResultClaim:
    return ResultClaim(
        opaque_subject_id=uuid.UUID("f52d4adf-838b-4dc8-97dc-76205f18d3ab"),
        tenant_id=uuid.UUID("3d26e7c9-075f-434a-ada0-c1c40ecdd52e"),
        course_release_id=uuid.UUID("a634f029-21a6-40d4-bca0-3c204ce5ece0"),
        assessment_id="exercise-final",
        attempt_id=uuid.UUID("a160ab37-dca4-4192-bdc6-587b7455ac2e"),
        raw_score=87,
        max_score=100,
        outcome="pass",
        scorer_version="scenario-v3",
        evidence_sha256=["a" * 64],
        occurred_at=datetime(2026, 10, 23, 14, 30, tzinfo=UTC),
    )


def make_package(claim: ResultClaim, signing_keys: tuple[str, str], recipient_keys: tuple[str, str]) -> dict:
    signing_private, _ = signing_keys
    _, recipient_public = recipient_keys
    package, _ = create_package(
        claim,
        source_enclave="UNCLASS",
        destination_enclave="SECRET",
        signing_private_key=signing_private,
        recipient_public_key=recipient_public,
        package_id=uuid.UUID("294a9fd2-c59d-49b8-be73-605a6823c07d"),
        created_at=datetime(2026, 10, 23, 15, tzinfo=UTC),
    )
    return package


def test_public_package_exposes_only_opaque_identity_and_pass_fail(
    claim: ResultClaim, signing_keys: tuple[str, str], recipient_keys: tuple[str, str]
) -> None:
    package = make_package(claim, signing_keys, recipient_keys)

    public_text = json.dumps(package, sort_keys=True)
    assert '"outcome": "pass"' in public_text
    assert str(claim.opaque_subject_id) in public_text
    assert '"raw_score"' not in public_text
    assert '"max_score"' not in public_text
    assert '"tenant_id"' not in public_text
    assert_public_package_minimal(package)


def test_secret_recipient_recovers_authoritative_result(
    claim: ResultClaim, signing_keys: tuple[str, str], recipient_keys: tuple[str, str]
) -> None:
    package = make_package(claim, signing_keys, recipient_keys)
    _, signing_public = signing_keys
    recipient_private, _ = recipient_keys

    result = decrypt_package(
        package,
        trusted_signing_public_key=signing_public,
        recipient_private_key=recipient_private,
        destination_enclave="SECRET",
    )

    assert result.claim == claim
    assert result.destination_enclave == "SECRET"


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("projection", "outcome"), "fail"),
        (("destination_enclave",), "TOP-SECRET"),
        (("ciphertext",), "AAAA"),
    ],
)
def test_any_public_tampering_invalidates_signature(
    claim: ResultClaim,
    signing_keys: tuple[str, str],
    recipient_keys: tuple[str, str],
    path: tuple[str, ...],
    replacement: str,
) -> None:
    package = copy.deepcopy(make_package(claim, signing_keys, recipient_keys))
    target = package
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement
    _, signing_public = signing_keys

    assert not verify_package(
        package,
        trusted_signing_public_key=signing_public,
        destination_enclave="SECRET",
    )


def test_wrong_recipient_cannot_decrypt(
    claim: ResultClaim, signing_keys: tuple[str, str], recipient_keys: tuple[str, str]
) -> None:
    package = make_package(claim, signing_keys, recipient_keys)
    _, signing_public = signing_keys
    other_recipient_private, _ = generate_recipient_keypair()

    with pytest.raises(ResultTransferError, match="another recipient"):
        decrypt_package(
            package,
            trusted_signing_public_key=signing_public,
            recipient_private_key=other_recipient_private,
            destination_enclave="SECRET",
        )


def test_untrusted_signer_is_rejected(
    claim: ResultClaim, signing_keys: tuple[str, str], recipient_keys: tuple[str, str]
) -> None:
    package = make_package(claim, signing_keys, recipient_keys)
    _, other_signing_public = generate_signing_keypair()

    assert not verify_package(
        package,
        trusted_signing_public_key=other_signing_public,
        destination_enclave="SECRET",
    )


def test_claim_rejects_impossible_score_and_bad_digest(claim: ResultClaim) -> None:
    with pytest.raises(ValidationError, match="raw_score cannot exceed max_score"):
        claim.model_copy(update={"raw_score": 101}).model_validate({**claim.model_dump(), "raw_score": 101})
    with pytest.raises(ValidationError, match="lowercase SHA-256"):
        ResultClaim.model_validate({**claim.model_dump(), "evidence_sha256": ["A" * 64]})


def test_claim_rejects_timezone_free_timestamp(claim: ResultClaim) -> None:
    with pytest.raises(ValidationError, match="must include a timezone"):
        ResultClaim.model_validate({**claim.model_dump(), "occurred_at": datetime(2026, 10, 23)})


def test_package_rejects_timezone_free_creation_time(
    claim: ResultClaim, signing_keys: tuple[str, str], recipient_keys: tuple[str, str]
) -> None:
    signing_private, _ = signing_keys
    _, recipient_public = recipient_keys
    with pytest.raises(ResultTransferError, match="created_at must include a timezone"):
        create_package(
            claim,
            source_enclave="UNCLASS",
            destination_enclave="SECRET",
            signing_private_key=signing_private,
            recipient_public_key=recipient_public,
            created_at=datetime(2026, 10, 23),
        )


@pytest.mark.parametrize("field", ["email", "user_id", "notes"])
def test_minimality_guard_blocks_any_future_public_fields(
    claim: ResultClaim, signing_keys: tuple[str, str], recipient_keys: tuple[str, str], field: str
) -> None:
    package = make_package(claim, signing_keys, recipient_keys)
    package["projection"][field] = "should-not-be-public"

    with pytest.raises(ResultTransferError, match="projection fields do not match"):
        assert_public_package_minimal(package)
