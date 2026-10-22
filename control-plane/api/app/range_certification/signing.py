"""Sign and verify a certification export (Ed25519 over canonical JSON).

The audit-log exports (ADR 0011) are signed with ``AUDIT_SIGNING_SEED``, which by that
ADR reaches the export worker and **no other service**, so the API cannot reuse it. This
uses the same construction with the API's own platform secret: an Ed25519 key from
HKDF-SHA256(``TN_SECRETS_KEY``, info ``truenorth/range-certification-export/v1``), its
``key_id`` the first 16 hex characters of SHA-256 of the raw public key. A purpose-bound
derivation, like ``range_credentials/seal.py``: nothing else signs or seals with it.

Rotation follows ``TN_SECRETS_KEY`` (comma-separated, newest first): the first key signs,
any of them verifies. Without the key nothing is signed (``SecretKeyMissingError``).

What is signed: the canonical JSON (sorted keys, no whitespace, UTF-8) of the export
without its ``signature`` member. Transport formatting does not matter; any change to a
value does. ``verify`` trusts only this platform's keys, never the public key the document
carries (anyone can sign with their own key); ``verify_with_public_key`` is for a verifier
holding a public key obtained out of band: GET /range-certification/signing-keys publishes
every key that has signed exports, retired ones included (service.sign_export records each
key the first time it signs), so exports made before a rotation still verify
(service.verify_export). How to verify outside TrueNorth: docs/range-certification.md.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .. import secretbox

ALG = "Ed25519"
_INFO = b"truenorth/range-certification-export/v1"


def canonical(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _keys() -> list[Ed25519PrivateKey]:
    raw = [k.strip() for k in os.getenv(secretbox.KEY_ENV, "").split(",") if k.strip()]
    if not raw or any(len(k) < secretbox.MIN_KEY_LENGTH for k in raw):
        raise secretbox.SecretKeyMissingError(f"{secretbox.KEY_ENV} is not set; certification exports cannot be signed")
    return [Ed25519PrivateKey.from_private_bytes(
        HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(k.encode())) for k in raw]


def _raw_public(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def key_id(public: Ed25519PublicKey) -> str:
    return hashlib.sha256(_raw_public(public)).hexdigest()[:16]


def _body(doc: dict) -> bytes:
    return canonical({k: v for k, v in doc.items() if k != "signature"})


def sign(doc: dict) -> dict:
    """``doc`` with a ``signature`` member: alg, key_id, public_key (base64 raw), value
    (base64), and the SHA-256 of the signed bytes."""
    key = _keys()[0]
    body = _body(doc)
    pub = key.public_key()
    return {**doc, "signature": {
        "alg": ALG, "key_id": key_id(pub), "public_key": base64.b64encode(_raw_public(pub)).decode(),
        "signed_sha256": hashlib.sha256(body).hexdigest(), "value": base64.b64encode(key.sign(body)).decode()}}


def verify_with_public_key(doc: dict, public: Ed25519PublicKey) -> bool:
    sig = doc.get("signature")
    if not isinstance(sig, dict) or sig.get("alg") != ALG or sig.get("key_id") != key_id(public):
        return False
    try:
        public.verify(base64.b64decode(sig.get("value") or "", validate=True), _body(doc))
    except (InvalidSignature, ValueError):
        return False
    return True


def public_key_from_b64(value: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(value, validate=True))


def current_public() -> dict:
    """The key that signs now: {alg, key_id, public_key (base64 raw)}."""
    pub = _keys()[0].public_key()
    return {"alg": ALG, "key_id": key_id(pub), "public_key": base64.b64encode(_raw_public(pub)).decode()}


def verify(doc: dict) -> bool:
    """True when a key of this platform signed exactly this document."""
    return any(verify_with_public_key(doc, k.public_key()) for k in _keys())
