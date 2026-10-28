"""How a content-port bundle is signed and verified.

The feed bundle's construction (``content_feeds.bundle_signing``: Ed25519 over a one-line
context followed by the canonical JSON of the document without its ``signature`` member,
built on the range certification helpers) with **purpose separation twice over**:

* its own context, ``CONTEXT``: a feed bundle, a range certification export (no context) or
  anything else signed by the same key never verifies as a content-port bundle, and a
  content-port bundle never as one of them;
* its own key: the exporting site signs with an Ed25519 key derived by HKDF-SHA256 from
  the platform secret ``TN_SECRETS_KEY`` with info ``truenorth/content-port-export/v1``.
  Nothing else signs or seals with it (the certification export's key has another info).
  Rotation follows ``TN_SECRETS_KEY`` (comma-separated, newest first): the first entry signs.

Verification on the importing site uses **only** the public keys its administrator
configured (``TN_CONTENT_PORT_TRUSTED_KEYS``, the installer's
``tn_content_port_trusted_keys``: comma-separated base64 raw Ed25519 public keys). Without
one, nothing is accepted (fail closed). The ``public_key`` a bundle carries is informational
and never trusted.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .. import secretbox
from ..content_feeds.bundle_signing import ALG, message_for, sign_for, verify_for
from ..range_certification.signing import key_id, public_key_from_b64

CONTEXT = b"truenorth-content-port/1\n"
_INFO = b"truenorth/content-port-export/v1"
TRUSTED_ENV = "TN_CONTENT_PORT_TRUSTED_KEYS"


class NoTrustedKeysError(Exception):
    """No (valid) trusted key is configured: nothing can be imported."""


def _signing_key() -> Ed25519PrivateKey:
    raw = [k.strip() for k in os.getenv(secretbox.KEY_ENV, "").split(",") if k.strip()]
    if not raw or any(len(k) < secretbox.MIN_KEY_LENGTH for k in raw):
        raise secretbox.SecretKeyMissingError(f"{secretbox.KEY_ENV} is not set; content cannot be exported")
    return Ed25519PrivateKey.from_private_bytes(
        HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(raw[0].encode()))


def public_info() -> dict:
    """This site's content-port signing key: {alg, key_id, public_key (base64 raw)}. The
    importing site puts ``public_key`` in tn_content_port_trusted_keys."""
    pub = _signing_key().public_key()
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"alg": ALG, "key_id": key_id(pub), "public_key": base64.b64encode(raw).decode()}


def sign(doc: dict) -> dict:
    return sign_for(CONTEXT, doc, _signing_key())


def signed_sha256(doc: dict) -> str:
    """SHA-256 of exactly what the signature covers (the bundle's own ``signed_sha256`` is
    not itself signed, so it is never trusted)."""
    return hashlib.sha256(message_for(CONTEXT, doc)).hexdigest()


def trusted_keys() -> dict[str, Ed25519PublicKey]:
    """{key_id: key} of the configured trusted keys. NoTrustedKeysError when none is set or
    any entry is not a base64 Ed25519 public key (a typo never silently narrows the list)."""
    raw = [k.strip() for k in os.getenv(TRUSTED_ENV, "").split(",") if k.strip()]
    if not raw:
        raise NoTrustedKeysError("No trusted content-port key is configured (tn_content_port_trusted_keys); "
                                 "nothing can be imported")
    out: dict[str, Ed25519PublicKey] = {}
    for value in raw:
        try:
            if len(base64.b64decode(value, validate=True)) != 32:
                raise ValueError
            pub = public_key_from_b64(value)
        except ValueError:
            raise NoTrustedKeysError("A configured content-port trusted key is not a base64 Ed25519 public key "
                                     "(tn_content_port_trusted_keys); nothing can be imported") from None
        out[key_id(pub)] = pub
    return out


def verify(doc: dict) -> str | None:
    """The key id of the trusted key that signed exactly this bundle under ``CONTEXT``, or
    None. NoTrustedKeysError when no key is configured."""
    keys = trusted_keys()
    sig = doc.get("signature")
    kid = sig.get("key_id") if isinstance(sig, dict) else None
    key = keys.get(kid) if isinstance(kid, str) else None
    if key is None or not verify_for(CONTEXT, doc, key):
        return None
    return kid
