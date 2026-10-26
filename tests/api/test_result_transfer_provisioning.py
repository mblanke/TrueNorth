from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.result_transfer.contract import ResultTransferError, generate_signing_keypair
from app.result_transfer.provisioning import create_subject_credential, verify_subject_credential


def credential(**changes):
    private, public = generate_signing_keypair()
    values = {
        "opaque_subject_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "source_enclave": "secret",
        "destination_enclave": "unclass",
        "signing_private_key": private,
        "issued_at": datetime(2026, 10, 24, 12, tzinfo=UTC),
    }
    values.update(changes)
    return create_subject_credential(**values), public


def test_credential_contains_only_opaque_subject_and_route() -> None:
    value, public = credential()
    text = json.dumps(value)
    assert not any(field in text for field in ("email", "display_name", "service_number", "grade", "score"))
    verified = verify_subject_credential(
        value,
        trusted_signing_public_key=public,
        source_enclave="secret",
        destination_enclave="unclass",
        now=datetime(2026, 10, 24, 12, 1, tzinfo=UTC),
    )
    assert str(verified.opaque_subject_id) == value["subject"]["opaque_subject_id"]


@pytest.mark.parametrize("change", ["destination", "subject", "signature"])
def test_tampering_is_refused(change: str) -> None:
    value, public = credential()
    if change == "destination":
        value["destination_enclave"] = "other"
    elif change == "subject":
        value["subject"]["opaque_subject_id"] = str(uuid.uuid4())
    else:
        value["signature"]["value"] = value["signature"]["value"][:-2] + "AA"
    with pytest.raises(ResultTransferError):
        verify_subject_credential(
            value,
            trusted_signing_public_key=public,
            source_enclave="secret",
            destination_enclave="unclass",
            now=datetime(2026, 10, 24, 12, 1, tzinfo=UTC),
        )


def test_expired_wrong_signer_and_extra_identity_are_refused() -> None:
    value, public = credential(lifetime=timedelta(minutes=1))
    _, other_public = generate_signing_keypair()
    for candidate, key, now in (
        (value, public, datetime(2026, 10, 24, 12, 2, tzinfo=UTC)),
        (value, other_public, datetime(2026, 10, 24, 12, tzinfo=UTC)),
        ({**value, "email": "person@example.test"}, public, datetime(2026, 10, 24, 12, tzinfo=UTC)),
    ):
        with pytest.raises(ResultTransferError):
            verify_subject_credential(
                candidate,
                trusted_signing_public_key=key,
                source_enclave="secret",
                destination_enclave="unclass",
                now=now,
            )
