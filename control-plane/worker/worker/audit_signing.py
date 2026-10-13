"""The audit export signing key (ADR 0011): Ed25519, derived from AUDIT_SIGNING_SEED.

The seed is an installer-generated secret (``audit_signing_seed`` in tn_generated_secrets,
persisted under config/secrets and escrowed by backup.sh), passed to the worker that runs
the export and to nothing else. There is no fallback key: without a seed of at least 32
characters, nothing is exported, so an unsigned export can never exist.

    key    = Ed25519PrivateKey(HKDF-SHA256(seed, info="truenorth-audit-export-v1"))
    key_id = first 16 hex characters of SHA-256(raw public key)

Anyone can verify a manifest with OpenSSL 3 and the public key in the archive
(``keys/<key_id>.pub.pem``)::

    openssl pkeyutl -verify -pubin -inkey keys/<key_id>.pub.pem -rawin \\
        -in audit-2026-10-11.manifest.json -sigfile audit-2026-10-11.manifest.json.sig

Rotation (docs/runbooks/key-rotation.md): a new seed gives a new key_id; manifests carry
the key_id they were signed with, and old public keys stay in ``keys/``.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SEED_ENV = "AUDIT_SIGNING_SEED"
MIN_SEED_CHARS = 32
HKDF_INFO = b"truenorth-audit-export-v1"


class SigningKeyError(RuntimeError):
    """No usable signing seed: nothing may be exported."""


@dataclass(frozen=True)
class SigningKey:
    private: Ed25519PrivateKey
    key_id: str
    public_pem: bytes

    def sign(self, data: bytes) -> bytes:
        return self.private.sign(data)


def key_id_of(public: Ed25519PublicKey) -> str:
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def from_seed(seed: str) -> SigningKey:
    if not seed or len(seed) < MIN_SEED_CHARS:
        raise SigningKeyError(
            f"{SEED_ENV} is missing or shorter than {MIN_SEED_CHARS} characters: audit exports are not signed, "
            "so none is written (installer secret audit_signing_seed)"
        )
    material = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=HKDF_INFO).derive(seed.encode())
    private = Ed25519PrivateKey.from_private_bytes(material)
    public = private.public_key()
    pem = public.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return SigningKey(private=private, key_id=key_id_of(public), public_pem=pem)


def load() -> SigningKey:
    """The key from the environment. Raises SigningKeyError without a usable seed."""
    return from_seed(os.getenv(SEED_ENV, ""))


def verify(public_pem: bytes, data: bytes, signature: bytes) -> bool:
    public = serialization.load_pem_public_key(public_pem)
    if not isinstance(public, Ed25519PublicKey):
        return False
    try:
        public.verify(signature, data)
    except InvalidSignature:
        return False
    return True


def fingerprint(key: SigningKey) -> str:
    """SHA-256 of the raw public key, in full (key_id is its first 16 characters)."""
    raw = key.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()


if __name__ == "__main__":  # python -m worker.audit_signing: the public fingerprint, never the seed
    import sys

    try:
        k = load()
    except SigningKeyError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    print(f"key_id {k.key_id} sha256:{fingerprint(k)}")
