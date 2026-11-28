# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""How a feed bundle is signed and verified (app/content_feeds/bundle_signing.py, ADR 0014
decision 25).

Ed25519 with domain separation: what is signed is the context, then the canonical JSON of
the bundle without its ``signature`` member. A document signed the range-certification way
(no context) by the same key therefore never verifies as a feed bundle, and verification
uses the site's configured key, never the ``public_key`` the bundle carries.
"""

from __future__ import annotations

import base64
import copy
import hashlib

import pytest
from app.content_feeds.bundle_signing import CONTEXT, message_for, sha256, sign, sign_for, verify, verify_for
from app.range_certification.signing import canonical, key_id
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _bundle() -> dict:
    return {"version": 1, "feed": "northwind", "items": [{"id": "i-1", "title": "Phishing run"}]}


def test_a_signed_bundle_verifies_with_the_signing_key():
    key, doc = _key(), _bundle()
    signed = sign(doc, key)
    assert signed["signature"]["alg"] == "Ed25519"
    assert signed["signature"]["key_id"] == key_id(key.public_key())
    assert verify(signed, key.public_key()) is True


def test_the_bundle_body_is_untouched_by_signing():
    doc, key = _bundle(), _key()
    assert {k: v for k, v in sign(doc, key).items() if k != "signature"} == doc


def test_any_tampering_breaks_verification():
    key, doc = _key(), _bundle()
    signed = sign(doc, key)
    for tampered in (
        {**signed, "feed": "other"},  # body edited after signing
        {**signed, "items": [{"id": "i-1", "title": "Phishing run", "extra": 1}]},
    ):
        assert verify(tampered, key.public_key()) is False


def test_a_foreign_key_never_verifies():
    signed = sign(_bundle(), _key())
    assert verify(signed, _key().public_key()) is False


def test_the_public_key_member_is_informational_only():
    """Verification uses the key given, never the bundle's ``public_key``."""
    key, other = _key(), _key()
    signed = sign(_bundle(), key)
    forged = copy.deepcopy(signed)
    forged["signature"]["public_key"] = base64.b64encode(
        other.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    assert verify(forged, other.public_key()) is False  # signed by ``key``, not ``other``
    assert verify(forged, key.public_key()) is True  # and it is still ``key``'s signature


def test_a_document_signed_without_the_context_never_verifies_as_a_bundle():
    """The same key signing the same body the range-certification way (no context)."""
    key, doc = _key(), _bundle()
    body = canonical({k: v for k, v in doc.items() if k != "signature"})
    plain = {
        **doc,
        "signature": {
            "alg": "Ed25519",
            "key_id": key_id(key.public_key()),
            "value": base64.b64encode(key.sign(body)).decode(),
        },
    }
    assert verify(plain, key.public_key()) is False


def test_a_bundle_never_verifies_under_another_context():
    key, doc = _key(), _bundle()
    signed = sign_for(b"truenorth/content-port/v1\n", doc, key)
    assert verify_for(b"truenorth/content-port/v1\n", signed, key.public_key()) is True
    assert verify(signed, key.public_key()) is False  # not a feed bundle


def test_a_context_is_one_line_ending_in_a_newline():
    with pytest.raises(ValueError):
        message_for(b"truenorth/no-newline", {})
    with pytest.raises(ValueError):
        message_for(b"two\nlines\n", {})


def test_the_signature_member_is_excluded_from_what_is_signed():
    key = _key()
    doc = {**_bundle(), "signature": {"alg": "forged", "value": "nope"}}
    # Signing re-writes the signature member, so a pre-existing one cannot poison the body.
    assert verify(sign(doc, key), key.public_key()) is True


def test_verify_never_raises_on_a_document_it_cannot_even_canonicalise():
    key = _key()
    assert verify({"signature": None}, key.public_key()) is False
    assert verify({}, key.public_key()) is False
    assert (
        verify(
            {"signature": {"alg": "Ed25519", "key_id": key_id(key.public_key()), "value": "!!not-base64!!"}},
            key.public_key(),
        )
        is False
    )
    assert (
        verify({"signature": {"alg": "plain", "key_id": key_id(key.public_key()), "value": "aa"}}, key.public_key())
        is False
    )


def test_the_message_is_the_context_then_the_canonical_body():
    doc = _bundle()
    assert message_for(CONTEXT, doc) == CONTEXT + canonical(doc)
    assert sha256(doc) == hashlib.sha256(message_for(CONTEXT, doc)).hexdigest()
