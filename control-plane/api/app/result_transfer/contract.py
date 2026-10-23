"""Signed, destination-bound and encrypted low-to-high result packages.

The outer document contains only routing data, an opaque subject id and pass/fail. The
numeric result exists only inside the X25519/AES-256-GCM ciphertext. Source authenticity is
Ed25519. Callers provide keys explicitly so an enclave's key custody remains outside this
portable contract.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FORMAT = "truenorth.result-transfer.v1"
_INFO = b"truenorth/result-transfer/v1"
_SCORE_KEYS = frozenset({"score", "raw_score", "max_score", "grade", "percentage"})
_IDENTITY_KEYS = frozenset({"name", "display_name", "email", "service_number", "unit", "rank"})
_PUBLIC_FIELDS = frozenset(
    {
        "format",
        "package_id",
        "source_enclave",
        "destination_enclave",
        "created_at",
        "projection",
        "encryption",
        "ciphertext",
        "signature",
    }
)
_PROJECTION_FIELDS = frozenset({"opaque_subject_id", "course_release_id", "assessment_id", "attempt_id", "outcome"})
_ENCRYPTION_FIELDS = frozenset({"alg", "recipient_key_id", "ephemeral_public_key", "nonce"})
_SIGNATURE_FIELDS = frozenset({"alg", "key_id", "signed_sha256", "value"})


class ResultTransferError(ValueError):
    """The package is malformed, untrusted, misdirected or cannot be decrypted."""


class ResultClaim(BaseModel):
    """The private result. It is serialized only inside the encrypted payload."""

    model_config = ConfigDict(extra="forbid")

    opaque_subject_id: uuid.UUID
    tenant_id: uuid.UUID
    course_release_id: uuid.UUID
    assessment_id: str = Field(min_length=1, max_length=255)
    attempt_id: uuid.UUID
    raw_score: int = Field(ge=0)
    max_score: int = Field(gt=0)
    outcome: Literal["pass", "fail"]
    scorer_version: str = Field(min_length=1, max_length=100)
    evidence_sha256: list[str] = Field(default_factory=list, max_length=1000)
    supersedes_package_id: uuid.UUID | None = None
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value

    @model_validator(mode="after")
    def valid_score_and_digests(self) -> ResultClaim:
        if self.raw_score > self.max_score:
            raise ValueError("raw_score cannot exceed max_score")
        if any(len(value) != 64 or any(c not in "0123456789abcdef" for c in value) for value in self.evidence_sha256):
            raise ValueError("evidence_sha256 values must be lowercase SHA-256 hex")
        return self


class ResultProjection(BaseModel):
    """The only result fields Unclassified may retain in readable form."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    opaque_subject_id: uuid.UUID
    course_release_id: uuid.UUID
    assessment_id: str
    attempt_id: uuid.UUID
    outcome: Literal["pass", "fail"]


class DecryptedResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    package_id: uuid.UUID
    source_enclave: str
    destination_enclave: str
    claim: ResultClaim


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode()


def _unb64(value: object, field: str) -> bytes:
    try:
        if not isinstance(value, str):
            raise ValueError
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ResultTransferError(f"{field} is not valid base64") from exc


def _public_bytes(key: Ed25519PublicKey | X25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _private_bytes(key: Ed25519PrivateKey | X25519PrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())


def _key_id(key: Ed25519PublicKey | X25519PublicKey) -> str:
    return hashlib.sha256(_public_bytes(key)).hexdigest()[:16]


def generate_signing_keypair() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    return _b64(_private_bytes(private)), _b64(_public_bytes(private.public_key()))


def generate_recipient_keypair() -> tuple[str, str]:
    private = X25519PrivateKey.generate()
    return _b64(_private_bytes(private)), _b64(_public_bytes(private.public_key()))


def _derive(shared: bytes, package_id: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=package_id.encode(), info=_INFO).derive(shared)


def _public_part(package: dict) -> dict:
    return {k: v for k, v in package.items() if k != "signature"}


def create_package(
    claim: ResultClaim,
    *,
    source_enclave: str,
    destination_enclave: str,
    signing_private_key: str,
    recipient_public_key: str,
    package_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> tuple[dict, ResultProjection]:
    """Return the signed encrypted package and its safe Unclassified projection."""
    if not source_enclave.strip() or not destination_enclave.strip() or source_enclave == destination_enclave:
        raise ResultTransferError("source and destination enclaves must be distinct, non-empty names")
    pid = package_id or uuid.uuid4()
    when = created_at or datetime.now(UTC)
    if when.tzinfo is None or when.utcoffset() is None:
        raise ResultTransferError("created_at must include a timezone")
    signing = Ed25519PrivateKey.from_private_bytes(_unb64(signing_private_key, "signing_private_key"))
    recipient = X25519PublicKey.from_public_bytes(_unb64(recipient_public_key, "recipient_public_key"))
    ephemeral = X25519PrivateKey.generate()
    nonce = os.urandom(12)
    projection = ResultProjection(
        opaque_subject_id=claim.opaque_subject_id,
        course_release_id=claim.course_release_id,
        assessment_id=claim.assessment_id,
        attempt_id=claim.attempt_id,
        outcome=claim.outcome,
    )
    header = {
        "format": FORMAT,
        "package_id": str(pid),
        "source_enclave": source_enclave.strip(),
        "destination_enclave": destination_enclave.strip(),
        "created_at": when.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "projection": projection.model_dump(mode="json"),
        "encryption": {
            "alg": "X25519-HKDF-SHA256+A256GCM",
            "recipient_key_id": _key_id(recipient),
            "ephemeral_public_key": _b64(_public_bytes(ephemeral.public_key())),
            "nonce": _b64(nonce),
        },
    }
    plaintext = _canonical(claim.model_dump(mode="json"))
    key = _derive(ephemeral.exchange(recipient), str(pid))
    package = {**header, "ciphertext": _b64(AESGCM(key).encrypt(nonce, plaintext, _canonical(header)))}
    body = _canonical(package)
    public = signing.public_key()
    package["signature"] = {
        "alg": "Ed25519",
        "key_id": _key_id(public),
        "signed_sha256": hashlib.sha256(body).hexdigest(),
        "value": _b64(signing.sign(body)),
    }
    assert_public_package_minimal(package)
    return package, projection


def verify_package(package: dict, *, trusted_signing_public_key: str, destination_enclave: str) -> bool:
    """Verify source authenticity and destination binding without decrypting the grade."""
    try:
        if package.get("format") != FORMAT or package.get("destination_enclave") != destination_enclave:
            return False
        signature = package.get("signature")
        if not isinstance(signature, dict) or signature.get("alg") != "Ed25519":
            return False
        public = Ed25519PublicKey.from_public_bytes(_unb64(trusted_signing_public_key, "trusted_signing_public_key"))
        body = _canonical(_public_part(package))
        if signature.get("key_id") != _key_id(public):
            return False
        if signature.get("signed_sha256") != hashlib.sha256(body).hexdigest():
            return False
        public.verify(_unb64(signature.get("value"), "signature.value"), body)
        assert_public_package_minimal(package)
        return True
    except (InvalidSignature, ResultTransferError, ValueError):
        return False


def decrypt_package(
    package: dict,
    *,
    trusted_signing_public_key: str,
    recipient_private_key: str,
    destination_enclave: str,
) -> DecryptedResult:
    """Verify, decrypt and prove that the public pass/fail projection matches the claim."""
    if not verify_package(
        package, trusted_signing_public_key=trusted_signing_public_key, destination_enclave=destination_enclave
    ):
        raise ResultTransferError("package signature or destination is invalid")
    try:
        pid = uuid.UUID(str(package["package_id"]))
        encryption = package["encryption"]
        private = X25519PrivateKey.from_private_bytes(_unb64(recipient_private_key, "recipient_private_key"))
        if encryption.get("recipient_key_id") != _key_id(private.public_key()):
            raise ResultTransferError("package is encrypted to another recipient key")
        ephemeral = X25519PublicKey.from_public_bytes(
            _unb64(encryption.get("ephemeral_public_key"), "encryption.ephemeral_public_key")
        )
        nonce = _unb64(encryption.get("nonce"), "encryption.nonce")
        header = {k: v for k, v in _public_part(package).items() if k != "ciphertext"}
        plain = AESGCM(_derive(private.exchange(ephemeral), str(pid))).decrypt(
            nonce, _unb64(package.get("ciphertext"), "ciphertext"), _canonical(header)
        )
        claim = ResultClaim.model_validate(json.loads(plain))
        projection = ResultProjection.model_validate(package["projection"])
        expected = ResultProjection(
            opaque_subject_id=claim.opaque_subject_id,
            course_release_id=claim.course_release_id,
            assessment_id=claim.assessment_id,
            attempt_id=claim.attempt_id,
            outcome=claim.outcome,
        )
        if projection != expected:
            raise ResultTransferError("public pass/fail projection does not match the encrypted result")
        return DecryptedResult(
            package_id=pid,
            source_enclave=package["source_enclave"],
            destination_enclave=package["destination_enclave"],
            claim=claim,
        )
    except ResultTransferError:
        raise
    except Exception as exc:
        raise ResultTransferError("package ciphertext or result claim is invalid") from exc


def assert_public_package_minimal(package: dict) -> None:
    """Fail if a future caller adds an identity or readable score outside ciphertext."""

    def exact_fields(value: object, expected: frozenset[str], location: str) -> None:
        if not isinstance(value, dict) or set(value) != expected:
            raise ResultTransferError(f"{location} fields do not match the public result contract")

    exact_fields(package, _PUBLIC_FIELDS, "package")
    exact_fields(package.get("projection"), _PROJECTION_FIELDS, "projection")
    exact_fields(package.get("encryption"), _ENCRYPTION_FIELDS, "encryption")
    exact_fields(package.get("signature"), _SIGNATURE_FIELDS, "signature")

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key.casefold() in _SCORE_KEYS | _IDENTITY_KEYS:
                    raise ResultTransferError(f"prohibited public result field: {key}")
                if key != "ciphertext":
                    walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(package)
