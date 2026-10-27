"""Signed grade-free receipts returned from Secret to Unclassified."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contract import ResultTransferError

RECEIPT_FORMAT = "truenorth.result-receipt.v1"
ReceiptStatus = Literal["accepted", "rejected"]
ReceiptError = Literal[
    "unknown_subject",
    "untrusted_source",
    "invalid_package",
    "unknown_release",
    "duplicate_conflict",
    "policy_denied",
]
_FIELDS = frozenset(
    {
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
)
_SIGNATURE_FIELDS = frozenset({"alg", "key_id", "signed_sha256", "value"})


class ResultReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    package_id: uuid.UUID
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_enclave: str = Field(min_length=1, max_length=100)
    destination_enclave: str = Field(min_length=1, max_length=100)
    status: ReceiptStatus
    error_category: ReceiptError | None = None
    created_at: datetime

    @model_validator(mode="after")
    def valid_status(self) -> ResultReceipt:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        if (self.status == "accepted") != (self.error_category is None):
            raise ValueError("accepted receipts have no error; rejected receipts require one")
        if self.source_enclave == self.destination_enclave:
            raise ValueError("receipt source and destination must differ")
        return self


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _unb64(value: object) -> bytes:
    if not isinstance(value, str):
        raise ResultTransferError("receipt signature is not valid base64")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ResultTransferError("receipt signature is not valid base64") from exc


def _public_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _key_id(key: Ed25519PublicKey) -> str:
    return hashlib.sha256(_public_bytes(key)).hexdigest()[:16]


def create_receipt(receipt: ResultReceipt, *, signing_private_key: str) -> dict:
    try:
        private = Ed25519PrivateKey.from_private_bytes(_unb64(signing_private_key))
    except ValueError as exc:
        raise ResultTransferError("receipt signing key is invalid") from exc
    body = {
        "format": RECEIPT_FORMAT,
        **receipt.model_dump(mode="json"),
        "created_at": receipt.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    encoded = _canonical(body)
    public = private.public_key()
    return {
        **body,
        "signature": {
            "alg": "Ed25519",
            "key_id": _key_id(public),
            "signed_sha256": hashlib.sha256(encoded).hexdigest(),
            "value": base64.b64encode(private.sign(encoded)).decode(),
        },
    }


def verify_receipt(
    value: dict,
    *,
    trusted_signing_public_key: str,
    source_enclave: str,
    destination_enclave: str,
) -> ResultReceipt:
    """Return the validated receipt or raise without exposing arbitrary error text."""
    try:
        if set(value) != _FIELDS or set(value.get("signature", {})) != _SIGNATURE_FIELDS:
            raise ResultTransferError("receipt fields do not match the public contract")
        if value.get("format") != RECEIPT_FORMAT:
            raise ResultTransferError("receipt format is unsupported")
        if value.get("source_enclave") != source_enclave or value.get("destination_enclave") != destination_enclave:
            raise ResultTransferError("receipt is from or for another enclave")
        signature = value["signature"]
        if signature.get("alg") != "Ed25519":
            raise ResultTransferError("receipt signature algorithm is unsupported")
        public = Ed25519PublicKey.from_public_bytes(_unb64(trusted_signing_public_key))
        if signature.get("key_id") != _key_id(public):
            raise ResultTransferError("receipt signing key is untrusted")
        body = {key: nested for key, nested in value.items() if key != "signature"}
        encoded = _canonical(body)
        if signature.get("signed_sha256") != hashlib.sha256(encoded).hexdigest():
            raise ResultTransferError("receipt digest does not match")
        public.verify(_unb64(signature.get("value")), encoded)
        return ResultReceipt.model_validate({key: nested for key, nested in body.items() if key != "format"})
    except ResultTransferError:
        raise
    except (InvalidSignature, KeyError, TypeError, ValueError) as exc:
        raise ResultTransferError("receipt signature or content is invalid") from exc
