from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime

import pytest
from app.result_transfer.contract import ResultTransferError, generate_signing_keypair
from app.result_transfer.receipt import ResultReceipt, create_receipt, verify_receipt
from pydantic import ValidationError


def accepted_receipt() -> ResultReceipt:
    return ResultReceipt(
        package_id=uuid.uuid4(),
        package_sha256="a" * 64,
        source_enclave="SECRET",
        destination_enclave="UNCLASS",
        status="accepted",
        created_at=datetime(2026, 10, 23, 16, tzinfo=UTC),
    )


def test_signed_receipt_contains_no_grade_or_identity() -> None:
    private, public = generate_signing_keypair()
    signed = create_receipt(accepted_receipt(), signing_private_key=private)

    result = verify_receipt(
        signed,
        trusted_signing_public_key=public,
        source_enclave="SECRET",
        destination_enclave="UNCLASS",
    )

    assert result.status == "accepted"
    assert set(signed) == {
        "format",
        "package_id",
        "package_sha256",
        "source_enclave",
        "destination_enclave",
        "status",
        "error_category",
        "created_at",
        "signature",
    }


@pytest.mark.parametrize("field", ["status", "package_sha256", "destination_enclave"])
def test_tampered_receipt_is_rejected(field: str) -> None:
    private, public = generate_signing_keypair()
    signed = create_receipt(accepted_receipt(), signing_private_key=private)
    tampered = copy.deepcopy(signed)
    tampered[field] = "changed"

    with pytest.raises(ResultTransferError):
        verify_receipt(
            tampered,
            trusted_signing_public_key=public,
            source_enclave="SECRET",
            destination_enclave="UNCLASS",
        )


def test_receipt_rejects_arbitrary_error_text() -> None:
    values = accepted_receipt().model_dump()
    values.update(status="rejected", error_category="student Jane received 88")

    with pytest.raises(ValidationError):
        ResultReceipt.model_validate(values)


def test_rejected_receipt_requires_safe_error_category() -> None:
    receipt = ResultReceipt(
        package_id=uuid.uuid4(),
        package_sha256="b" * 64,
        source_enclave="SECRET",
        destination_enclave="UNCLASS",
        status="rejected",
        error_category="unknown_subject",
        created_at=datetime.now(UTC),
    )

    assert receipt.error_category == "unknown_subject"
