"""Signed Secret-to-Unclassified opaque subject credential."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contract import ResultTransferError

FORMAT = "truenorth.subject-provisioning.v1"
PUBLIC_FIELDS = frozenset(
    {"format", "credential_id", "source_enclave", "destination_enclave", "issued_at", "expires_at", "subject", "signature"}
)
SUBJECT_FIELDS = frozenset({"opaque_subject_id", "tenant_id"})


class SubjectCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    credential_id: uuid.UUID
    source_enclave: str = Field(min_length=1, max_length=100)
    destination_enclave: str = Field(min_length=1, max_length=100)
    opaque_subject_id: uuid.UUID
    tenant_id: uuid.UUID
    issued_at: datetime
    expires_at: datetime

    @field_validator("issued_at", "expires_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("credential timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def bounded_validity(self) -> SubjectCredential:
        if self.expires_at <= self.issued_at or self.expires_at - self.issued_at > timedelta(days=90):
            raise ValueError("credential validity must be positive and no longer than 90 days")
        if self.source_enclave == self.destination_enclave:
            raise ValueError("credential source and destination must differ")
        return self


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode()


def _unb64(value: object) -> bytes:
    if not isinstance(value, str):
        raise ResultTransferError("credential key or signature is not valid base64")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ResultTransferError("credential key or signature is not valid base64") from exc


def _public_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def create_subject_credential(
    *,
    opaque_subject_id: uuid.UUID,
    tenant_id: uuid.UUID,
    source_enclave: str,
    destination_enclave: str,
    signing_private_key: str,
    credential_id: uuid.UUID | None = None,
    issued_at: datetime | None = None,
    lifetime: timedelta = timedelta(days=30),
) -> dict:
    if source_enclave == destination_enclave or not source_enclave or not destination_enclave:
        raise ResultTransferError("subject credential enclaves must be distinct and non-empty")
    if lifetime <= timedelta() or lifetime > timedelta(days=90):
        raise ResultTransferError("subject credential lifetime must be between zero and 90 days")
    when = issued_at or datetime.now(UTC)
    if when.tzinfo is None or when.utcoffset() is None:
        raise ResultTransferError("issued_at must include a timezone")
    body = {
        "format": FORMAT,
        "credential_id": str(credential_id or uuid.uuid4()),
        "source_enclave": source_enclave,
        "destination_enclave": destination_enclave,
        "issued_at": when.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "expires_at": (when + lifetime).astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "subject": {"opaque_subject_id": str(opaque_subject_id), "tenant_id": str(tenant_id)},
    }
    private = Ed25519PrivateKey.from_private_bytes(_unb64(signing_private_key))
    public = private.public_key()
    encoded = _canonical(body)
    return {
        **body,
        "signature": {
            "alg": "Ed25519",
            "key_id": hashlib.sha256(_public_bytes(public)).hexdigest()[:16],
            "signed_sha256": hashlib.sha256(encoded).hexdigest(),
            "value": _b64(private.sign(encoded)),
        },
    }


def verify_subject_credential(
    value: dict,
    *,
    trusted_signing_public_key: str,
    source_enclave: str,
    destination_enclave: str,
    now: datetime | None = None,
) -> SubjectCredential:
    try:
        if set(value) != PUBLIC_FIELDS or set(value.get("subject", {})) != SUBJECT_FIELDS:
            raise ResultTransferError("subject credential contains unexpected fields")
        if (
            value.get("format") != FORMAT
            or value.get("source_enclave") != source_enclave
            or value.get("destination_enclave") != destination_enclave
        ):
            raise ResultTransferError("subject credential format or route is invalid")
        signature = value.get("signature")
        if not isinstance(signature, dict) or set(signature) != {"alg", "key_id", "signed_sha256", "value"}:
            raise ResultTransferError("subject credential signature is malformed")
        body = {key: item for key, item in value.items() if key != "signature"}
        encoded = _canonical(body)
        public = Ed25519PublicKey.from_public_bytes(_unb64(trusted_signing_public_key))
        if signature["alg"] != "Ed25519":
            raise ResultTransferError("subject credential signature algorithm is invalid")
        if signature["key_id"] != hashlib.sha256(_public_bytes(public)).hexdigest()[:16]:
            raise ResultTransferError("subject credential signer is not trusted")
        if signature["signed_sha256"] != hashlib.sha256(encoded).hexdigest():
            raise ResultTransferError("subject credential digest is invalid")
        public.verify(_unb64(signature["value"]), encoded)
        subject = value["subject"]
        credential = SubjectCredential.model_validate(
            {
                "credential_id": body["credential_id"],
                "source_enclave": body["source_enclave"],
                "destination_enclave": body["destination_enclave"],
                "opaque_subject_id": subject["opaque_subject_id"],
                "tenant_id": subject["tenant_id"],
                "issued_at": body["issued_at"],
                "expires_at": body["expires_at"],
            }
        )
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ResultTransferError("verification time must include a timezone")
        if credential.expires_at <= current or credential.issued_at > current + timedelta(minutes=5):
            raise ResultTransferError("subject credential is expired or not yet valid")
        return credential
    except (InvalidSignature, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ResultTransferError):
            raise
        raise ResultTransferError("subject credential signature or fields are invalid") from exc
