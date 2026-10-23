"""Persistence rules for the Unclassified outbox and Secret authoritative ledger."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..course_releases.models import CourseRelease
from ..models import User
from ..settings import UnsafeSettingsError, enclave_id
from ..settings import classification_level as configured_classification_level
from .contract import DecryptedResult, ResultClaim, ResultProjection, ResultTransferError, create_package
from .media import encode_package
from .models import ImportedResult, ResultSubjectBinding, ResultTransferOutbox
from .receipt import verify_receipt

SECRET_LEVELS = frozenset({"secret", "top_secret"})


@dataclass(frozen=True)
class QueuedResult:
    package: dict
    projection: ResultProjection
    row: ResultTransferOutbox


def classification_level() -> str:
    try:
        return configured_classification_level()
    except UnsafeSettingsError as exc:
        raise ResultTransferError(str(exc)) from exc


def _enclave_id() -> str:
    try:
        return enclave_id()
    except UnsafeSettingsError as exc:
        raise ResultTransferError(str(exc)) from exc


def _require_secret() -> None:
    if classification_level() not in SECRET_LEVELS:
        raise ResultTransferError("subject mappings and readable grades require a Secret-side deployment")


def queue_result(
    db: Session,
    claim: ResultClaim,
    *,
    source_enclave: str,
    destination_enclave: str,
    signing_private_key: str,
    recipient_public_key: str,
    package_id: uuid.UUID | None = None,
) -> QueuedResult:
    """Seal a numeric result before adding its grade-free Unclassified outbox row."""
    if classification_level() != "unclassified":
        raise ResultTransferError("the low-to-high result outbox is enabled only on Unclassified")
    if source_enclave != _enclave_id():
        raise ResultTransferError("source_enclave does not match this deployment's TN_ENCLAVE_ID")
    package, projection = create_package(
        claim,
        source_enclave=source_enclave,
        destination_enclave=destination_enclave,
        signing_private_key=signing_private_key,
        recipient_public_key=recipient_public_key,
        package_id=package_id,
    )
    content = encode_package(package)
    row = ResultTransferOutbox(
        package_id=uuid.UUID(package["package_id"]),
        tenant_id=claim.tenant_id,
        opaque_subject_id=projection.opaque_subject_id,
        course_release_id=projection.course_release_id,
        assessment_id=projection.assessment_id,
        attempt_id=projection.attempt_id,
        outcome=projection.outcome,
        source_enclave=package["source_enclave"],
        destination_enclave=package["destination_enclave"],
        package_json=content.decode("utf-8"),
        package_sha256=hashlib.sha256(content).hexdigest(),
        state="pending",
    )
    db.add(row)
    db.flush()
    return QueuedResult(package=package, projection=projection, row=row)


def bind_subject(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    source_enclave: str,
    opaque_subject_id: uuid.UUID,
    user_id: uuid.UUID,
) -> ResultSubjectBinding:
    """Create the person mapping on Secret; an existing token can never be rebound."""
    _require_secret()
    user = db.query(User).filter(User.id == user_id, User.tenant_id == tenant_id).one_or_none()
    if user is None:
        raise ResultTransferError("subject binding user does not belong to the destination tenant")
    existing = (
        db.query(ResultSubjectBinding)
        .filter_by(
            tenant_id=tenant_id,
            source_enclave=source_enclave,
            opaque_subject_id=opaque_subject_id,
        )
        .one_or_none()
    )
    if existing:
        if existing.user_id != user_id:
            raise ResultTransferError("opaque subject token is already bound to another user")
        if not existing.active:
            raise ResultTransferError("revoked opaque subject token cannot be reused")
        return existing
    binding = ResultSubjectBinding(
        tenant_id=tenant_id,
        source_enclave=source_enclave,
        opaque_subject_id=opaque_subject_id,
        user_id=user_id,
    )
    db.add(binding)
    db.flush()
    return binding


def import_result(
    db: Session,
    result: DecryptedResult,
    *,
    package_sha256: str,
) -> tuple[ImportedResult, bool]:
    """Record one verified result on Secret; return ``(record, created)``."""
    _require_secret()
    if result.destination_enclave != _enclave_id():
        raise ResultTransferError("result destination does not match this deployment's TN_ENCLAVE_ID")
    if len(package_sha256) != 64 or any(c not in "0123456789abcdef" for c in package_sha256):
        raise ResultTransferError("package_sha256 must be lowercase SHA-256 hex")
    claim = result.claim
    existing = (
        db.query(ImportedResult)
        .filter(ImportedResult.package_id == result.package_id, ImportedResult.tenant_id == claim.tenant_id)
        .one_or_none()
    )
    if existing:
        if existing.package_sha256 != package_sha256:
            raise ResultTransferError("package_id was already imported with different content")
        return existing, False

    binding = (
        db.query(ResultSubjectBinding)
        .filter_by(
            tenant_id=claim.tenant_id,
            source_enclave=result.source_enclave,
            opaque_subject_id=claim.opaque_subject_id,
            active=True,
        )
        .one_or_none()
    )
    if binding is None:
        raise ResultTransferError("no active Secret subject binding exists for this source token")
    release = (
        db.query(CourseRelease)
        .filter(
            CourseRelease.id == claim.course_release_id,
            or_(CourseRelease.tenant_id.is_(None), CourseRelease.tenant_id == claim.tenant_id),
        )
        .one_or_none()
    )
    if release is None:
        raise ResultTransferError("course release is not approved for the destination tenant")

    same_attempt = (
        db.query(ImportedResult)
        .filter_by(
            tenant_id=claim.tenant_id,
            source_enclave=result.source_enclave,
            attempt_id=claim.attempt_id,
        )
        .first()
    )
    if claim.supersedes_package_id:
        prior = (
            db.query(ImportedResult)
            .filter_by(
                package_id=claim.supersedes_package_id,
                tenant_id=claim.tenant_id,
                source_enclave=result.source_enclave,
            )
            .one_or_none()
        )
        if prior is None:
            raise ResultTransferError("superseded result does not exist for this source enclave")
        if prior.attempt_id != claim.attempt_id:
            raise ResultTransferError("a correction must keep the original attempt_id")
        if (
            prior.subject_binding_id != binding.id
            or prior.course_release_id != claim.course_release_id
            or prior.assessment_id != claim.assessment_id
        ):
            raise ResultTransferError("a correction cannot change subject, course release or assessment")
        successor = (
            db.query(ImportedResult.package_id).filter(ImportedResult.supersedes_package_id == prior.package_id).first()
        )
        if successor:
            raise ResultTransferError("correction does not supersede the latest result for this attempt")
    elif same_attempt:
        raise ResultTransferError("a new package for an imported attempt must declare supersedes_package_id")

    record = ImportedResult(
        package_id=result.package_id,
        package_sha256=package_sha256,
        tenant_id=claim.tenant_id,
        subject_binding_id=binding.id,
        user_id=binding.user_id,
        source_enclave=result.source_enclave,
        course_release_id=claim.course_release_id,
        assessment_id=claim.assessment_id,
        attempt_id=claim.attempt_id,
        raw_score=claim.raw_score,
        max_score=claim.max_score,
        outcome=claim.outcome,
        scorer_version=claim.scorer_version,
        evidence_sha256_json=json.dumps(claim.evidence_sha256, separators=(",", ":")),
        supersedes_package_id=claim.supersedes_package_id,
        occurred_at=claim.occurred_at,
    )
    db.add(record)
    db.flush()
    return record, True


def apply_receipt(
    db: Session,
    value: dict,
    *,
    trusted_signing_public_key: str,
    source_enclave: str,
    destination_enclave: str,
) -> ResultTransferOutbox:
    """Verify a Secret receipt and advance exactly its matching low-side outbox row."""
    if classification_level() != "unclassified":
        raise ResultTransferError("result receipts are applied only to the Unclassified outbox")
    if destination_enclave != _enclave_id():
        raise ResultTransferError("receipt destination does not match this deployment's TN_ENCLAVE_ID")
    receipt = verify_receipt(
        value,
        trusted_signing_public_key=trusted_signing_public_key,
        source_enclave=source_enclave,
        destination_enclave=destination_enclave,
    )
    row = (
        db.query(ResultTransferOutbox)
        .filter_by(
            package_id=receipt.package_id,
            package_sha256=receipt.package_sha256,
            source_enclave=receipt.destination_enclave,
            destination_enclave=receipt.source_enclave,
        )
        .one_or_none()
    )
    if row is None:
        raise ResultTransferError("receipt does not match an outbox package and route")
    target = "acknowledged" if receipt.status == "accepted" else "rejected"
    if row.state in {"acknowledged", "rejected"} and row.state != target:
        raise ResultTransferError("receipt conflicts with the terminal outbox state")
    row.state = target
    row.acknowledged_at = datetime.now(UTC)
    db.flush()
    return row
