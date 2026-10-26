from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.result_transfer import (
    FileConsumptionLedger,
    ResultClaim,
    ResultTransferError,
    consume_package,
    create_package,
    export_package,
    generate_recipient_keypair,
    generate_signing_keypair,
    load_package,
)
from app.result_transfer.media import MAX_PACKAGE_BYTES


@pytest.fixture
def transfer() -> tuple[dict, str, str]:
    signing_private, signing_public = generate_signing_keypair()
    recipient_private, recipient_public = generate_recipient_keypair()
    claim = ResultClaim(
        opaque_subject_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        course_release_id=uuid.uuid4(),
        assessment_id="final",
        attempt_id=uuid.uuid4(),
        raw_score=42,
        max_score=50,
        outcome="pass",
        scorer_version="scenario-v1",
        occurred_at=datetime.now(UTC),
    )
    package, _ = create_package(
        claim,
        source_enclave="UNCLASS",
        destination_enclave="SECRET",
        signing_private_key=signing_private,
        recipient_public_key=recipient_public,
    )
    return package, signing_public, recipient_private


def test_export_load_and_consume_once(tmp_path: Path, transfer: tuple[dict, str, str]) -> None:
    package, signing_public, recipient_private = transfer
    media = export_package(
        package,
        tmp_path / "media",
        trusted_signing_public_key=signing_public,
        destination_enclave="SECRET",
    )
    loaded = load_package(
        media.path,
        trusted_signing_public_key=signing_public,
        destination_enclave="SECRET",
    )
    applied = []
    ledger = FileConsumptionLedger(tmp_path / "secret-ledger")

    first = consume_package(
        loaded.path,
        trusted_signing_public_key=signing_public,
        recipient_private_key=recipient_private,
        destination_enclave="SECRET",
        ledger=ledger,
        apply=applied.append,
    )
    second = consume_package(
        loaded.path,
        trusted_signing_public_key=signing_public,
        recipient_private_key=recipient_private,
        destination_enclave="SECRET",
        ledger=ledger,
        apply=applied.append,
    )

    assert first.status == "applied"
    assert second.status == "duplicate"
    assert len(applied) == 1
    assert applied[0].claim.raw_score == 42
    assert oct(media.path.stat().st_mode & 0o777) == "0o600"


def test_export_never_overwrites_existing_package(tmp_path: Path, transfer: tuple[dict, str, str]) -> None:
    package, signing_public, _ = transfer
    arguments = {
        "trusted_signing_public_key": signing_public,
        "destination_enclave": "SECRET",
    }
    export_package(package, tmp_path, **arguments)

    with pytest.raises(ResultTransferError, match="refusing to overwrite"):
        export_package(package, tmp_path, **arguments)


def test_load_rejects_symlink(tmp_path: Path, transfer: tuple[dict, str, str]) -> None:
    package, signing_public, _ = transfer
    media = export_package(
        package,
        tmp_path,
        trusted_signing_public_key=signing_public,
        destination_enclave="SECRET",
    )
    link = tmp_path / "linked.tnresult.json"
    os.symlink(media.path, link)

    with pytest.raises(ResultTransferError, match="regular file"):
        load_package(
            link,
            trusted_signing_public_key=signing_public,
            destination_enclave="SECRET",
        )


def test_export_rejects_symlinked_destination(tmp_path: Path, transfer: tuple[dict, str, str]) -> None:
    package, signing_public, _ = transfer
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    os.symlink(real, linked)

    with pytest.raises(ResultTransferError, match="real directory"):
        export_package(
            package,
            linked,
            trusted_signing_public_key=signing_public,
            destination_enclave="SECRET",
        )


def test_load_rejects_oversized_file(tmp_path: Path, transfer: tuple[dict, str, str]) -> None:
    _, signing_public, _ = transfer
    path = tmp_path / f"{uuid.uuid4()}.tnresult.json"
    path.write_bytes(b"x" * (MAX_PACKAGE_BYTES + 1))

    with pytest.raises(ResultTransferError, match="size limit"):
        load_package(
            path,
            trusted_signing_public_key=signing_public,
            destination_enclave="SECRET",
        )


def test_load_rejects_filename_that_does_not_match_signed_id(tmp_path: Path, transfer: tuple[dict, str, str]) -> None:
    package, signing_public, _ = transfer
    media = export_package(
        package,
        tmp_path,
        trusted_signing_public_key=signing_public,
        destination_enclave="SECRET",
    )
    renamed = tmp_path / f"{uuid.uuid4()}.tnresult.json"
    media.path.rename(renamed)

    with pytest.raises(ResultTransferError, match="filename does not match"):
        load_package(
            renamed,
            trusted_signing_public_key=signing_public,
            destination_enclave="SECRET",
        )


def test_consumption_ledger_rejects_same_id_with_different_content(tmp_path: Path) -> None:
    ledger = FileConsumptionLedger(tmp_path)
    package_id = uuid.uuid4()
    ledger.record(package_id, "a" * 64)

    with pytest.raises(ResultTransferError, match="different content"):
        ledger.check(package_id, "b" * 64)


def test_consumption_ledger_never_follows_receipt_symlinks(tmp_path: Path) -> None:
    ledger = FileConsumptionLedger(tmp_path / "ledger")
    ledger.directory.mkdir()
    package_id = uuid.uuid4()
    target = tmp_path / "outside"
    target.write_text(f"{'a' * 64}\n", encoding="ascii")
    os.symlink(target, ledger.directory / f"{package_id}.sha256")

    with pytest.raises(ResultTransferError, match="regular file"):
        ledger.check(package_id, "a" * 64)
