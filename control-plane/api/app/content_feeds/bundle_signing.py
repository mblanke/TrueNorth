"""How a feed bundle is signed and verified (ADR 0014 decision 25).

Ed25519, the range certification export's helpers (``range_certification.signing``:
canonical JSON, key ids, public keys), with **domain separation**: what is signed is
``CONTEXT`` followed by the canonical JSON of the bundle without its ``signature`` member.
A document signed the range-certification way (no context) by the same key therefore never
verifies as a feed bundle, and a feed bundle never as anything else. The certification
construction itself is unchanged.

The signature member is ``{alg, key_id, public_key, signed_sha256, value}``; only ``alg``,
``key_id`` and ``value`` are used. ``public_key`` is informational: verification uses the
site's configured key, never the bundle's.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ..range_certification.signing import canonical, key_id

ALG = "Ed25519"
CONTEXT = b"truenorth/content-feed-bundle/v1\n"


def message(doc: dict) -> bytes:
    """The bytes a bundle's signature covers."""
    return CONTEXT + canonical({k: v for k, v in doc.items() if k != "signature"})


def sha256(doc: dict) -> str:
    return hashlib.sha256(message(doc)).hexdigest()


def sign(doc: dict, key: Ed25519PrivateKey) -> dict:
    """``doc`` with its ``signature`` member (scripts/build_feed_bundle.py)."""
    body = message(doc)
    pub = key.public_key()
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {**doc, "signature": {"alg": ALG, "key_id": key_id(pub), "public_key": base64.b64encode(raw).decode(),
                                 "signed_sha256": hashlib.sha256(body).hexdigest(),
                                 "value": base64.b64encode(key.sign(body)).decode()}}


def verify(doc: dict, public: Ed25519PublicKey) -> bool:
    """True only when ``public`` signed exactly this bundle. Never raises: a document that
    cannot even be canonicalised (a lone surrogate, say) is simply not verified."""
    sig = doc.get("signature")
    if not isinstance(sig, dict) or sig.get("alg") != ALG or sig.get("key_id") != key_id(public):
        return False
    try:
        public.verify(base64.b64decode(sig.get("value") or "", validate=True), message(doc))
    except (InvalidSignature, ValueError, TypeError, RecursionError):
        return False
    return True
