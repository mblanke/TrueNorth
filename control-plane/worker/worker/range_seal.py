"""Open range passwords sealed with the range-credential key (``tnsec:rc1:``, ADR 0013 §3).

The API seals every stored range password with a key derived from ``TN_SECRETS_KEY`` for
range credentials only (control-plane/api/app/range_credentials/seal.py: HKDF-SHA256, info
``truenorth/range-credentials/v1``). The worker cannot import the API, so this module
derives the same key; tests/contracts/test_range_seal_matches_api.py fails if the two stop
opening each other's values. ``unseal_stored`` also reads what came before: ``tnsec:v1:``
(the general secretbox) and legacy plain values.

Opening only: the worker never makes a range password (the API generates and seals them).
"""

from __future__ import annotations

import base64
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import secretbox

PREFIX = "tnsec:rc1:"
_INFO = b"truenorth/range-credentials/v1"


def _box() -> MultiFernet:
    keys = [k.strip() for k in os.getenv(secretbox.KEY_ENV, "").split(",") if k.strip()]
    if not keys or any(len(k) < secretbox.MIN_KEY_LENGTH for k in keys):
        raise secretbox.SecretKeyMissingError(f"{secretbox.KEY_ENV} is not set; range passwords cannot be opened")
    derived = [HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(k.encode()) for k in keys]
    return MultiFernet([Fernet(base64.urlsafe_b64encode(d)) for d in derived])


def unseal_stored(stored: str | None) -> str | None:
    """The password a stored ``range_credentials`` value holds (``rc1``, ``v1`` or plain)."""
    if stored is None or not stored.startswith(PREFIX):
        return secretbox.unseal(stored)
    try:
        return _box().decrypt(stored[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise secretbox.SecretUnreadableError(
            f"a range password cannot be opened with {secretbox.KEY_ENV}; was the key changed?") from exc
