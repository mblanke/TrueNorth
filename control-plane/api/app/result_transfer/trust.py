"""Purpose-bound public-key enrollment for independent enclave result transfer."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
from sqlalchemy.orm import Session

from ..settings import ENCLAVE_ID_RE, UnsafeSettingsError, classification_level, enclave_id
from .contract import ResultTransferError
from .models import ResultTransferTrustKey

TrustPurpose = Literal["result-signing", "subject-signing", "receipt-signing", "result-recipient"]
PURPOSES = frozenset({"result-signing", "subject-signing", "receipt-signing", "result-recipient"})
SIGNING_PURPOSES = frozenset({"result-signing", "subject-signing", "receipt-signing"})
ALLOWED_BY_CLASSIFICATION = {
    "unclassified": frozenset({"subject-signing", "receipt-signing", "result-recipient"}),
    "secret": frozenset({"result-signing"}),
    "top_secret": frozenset({"result-signing"}),
}


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ResultTransferError("trust-key time must include a timezone")
    return current


def _route_and_purpose(purpose: str, remote_enclave: str) -> tuple[str, str]:
    if purpose not in PURPOSES:
        raise ResultTransferError("unknown result-transfer key purpose")
    try:
        level, local = classification_level(), enclave_id()
    except UnsafeSettingsError as exc:
        raise ResultTransferError(str(exc)) from exc
    if purpose not in ALLOWED_BY_CLASSIFICATION[level]:
        raise ResultTransferError(f"{purpose} keys cannot be enrolled on {level}")
    if remote_enclave == local:
        raise ResultTransferError("trusted result-transfer key must belong to a remote enclave")
    if not ENCLAVE_ID_RE.fullmatch(remote_enclave):
        raise ResultTransferError("remote enclave ID is invalid")
    return purpose, remote_enclave


def _validated_public_key(purpose: str, value: str) -> tuple[str, str]:
    try:
        raw = base64.b64decode(value, validate=True)
        if len(raw) != 32:
            raise ValueError
        if purpose in SIGNING_PURPOSES:
            Ed25519PublicKey.from_public_bytes(raw)
        else:
            X25519PublicKey.from_public_bytes(raw)
    except (TypeError, ValueError) as exc:
        raise ResultTransferError("trusted public key must be one base64-encoded 32-byte key") from exc
    canonical = base64.b64encode(raw).decode("ascii")
    return canonical, hashlib.sha256(raw).hexdigest()[:16]


def enroll_trusted_key(
    db: Session,
    *,
    purpose: TrustPurpose,
    remote_enclave: str,
    public_key: str,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
) -> tuple[ResultTransferTrustKey, bool]:
    """Enroll one remote public key, idempotently, without accepting private material."""
    purpose, remote_enclave = _route_and_purpose(purpose, remote_enclave)
    canonical, key_id = _validated_public_key(purpose, public_key)
    starts = _now(not_before)
    ends = _now(not_after) if not_after is not None else None
    if ends is not None and ends <= starts:
        raise ResultTransferError("trust-key not_after must be later than not_before")
    existing = (
        db.query(ResultTransferTrustKey)
        .filter_by(purpose=purpose, remote_enclave=remote_enclave, key_id=key_id)
        .one_or_none()
    )
    if existing:
        if existing.public_key != canonical:
            raise ResultTransferError("trusted key ID is already enrolled with different material")
        return existing, False
    row = ResultTransferTrustKey(
        purpose=purpose,
        remote_enclave=remote_enclave,
        key_id=key_id,
        public_key=canonical,
        not_before=starts,
        not_after=ends,
    )
    db.add(row)
    db.flush()
    return row, True


def resolve_trusted_key(
    db: Session,
    *,
    purpose: TrustPurpose,
    remote_enclave: str,
    key_id: str,
    at: datetime | None = None,
) -> str:
    """Return exactly one active public key valid at ``at``; never guess another key."""
    purpose, remote_enclave = _route_and_purpose(purpose, remote_enclave)
    current = _now(at)
    row = (
        db.query(ResultTransferTrustKey)
        .filter_by(purpose=purpose, remote_enclave=remote_enclave, key_id=key_id, active=True)
        .one_or_none()
    )
    if row is None:
        raise ResultTransferError("no active trusted key matches this route, purpose and key ID")
    starts = row.not_before if row.not_before.tzinfo else row.not_before.replace(tzinfo=UTC)
    ends = row.not_after
    if ends is not None and ends.tzinfo is None:
        ends = ends.replace(tzinfo=UTC)
    if current < starts or (ends is not None and current >= ends):
        raise ResultTransferError("trusted key is outside its validity window")
    return row.public_key


def resolve_signed_package_key(
    db: Session,
    value: dict,
    *,
    purpose: TrustPurpose,
    remote_enclave: str,
    at: datetime | None = None,
) -> str:
    """Resolve the enrolled key named by an untrusted package; verification follows."""
    signature = value.get("signature") if isinstance(value, dict) else None
    key_id = signature.get("key_id") if isinstance(signature, dict) else None
    if (
        not isinstance(key_id, str)
        or len(key_id) != 16
        or any(character not in "0123456789abcdef" for character in key_id)
    ):
        raise ResultTransferError("signed package key ID is missing or invalid")
    return resolve_trusted_key(
        db,
        purpose=purpose,
        remote_enclave=remote_enclave,
        key_id=key_id,
        at=at,
    )


def retire_trusted_key(
    db: Session,
    *,
    purpose: TrustPurpose,
    remote_enclave: str,
    key_id: str,
    at: datetime | None = None,
) -> ResultTransferTrustKey:
    """Retire a public key without deleting its audit history."""
    purpose, remote_enclave = _route_and_purpose(purpose, remote_enclave)
    row = (
        db.query(ResultTransferTrustKey)
        .filter_by(purpose=purpose, remote_enclave=remote_enclave, key_id=key_id)
        .one_or_none()
    )
    if row is None:
        raise ResultTransferError("trusted key was not found")
    if row.active:
        row.active = False
        row.retired_at = _now(at)
        db.flush()
    return row
