"""Seal and unseal stored credentials (hypervisor passwords and tokens, AI engine keys).

SOURCE OF TRUTH: control-plane/worker/worker/secretbox.py. The API ships a byte-identical
copy at control-plane/api/app/secretbox.py (the two services build separate images and
neither may import the other); tests/api/test_secret_storage.py fails if they differ.

* A sealed value is ``tnsec:v1:`` + a Fernet token (AES-128-CBC + HMAC-SHA256, random IV,
  so the same secret seals differently each time).
* The key is ``TN_SECRETS_KEY``: at least 32 characters of randomness, of any form (it is
  hashed to the Fernet key). A comma-separated list rotates keys: the first seals, every one
  of them unseals. Without a usable key nothing is sealed: ``SecretKeyMissingError``, never a
  silent fallback to storing the secret as typed.
* ``unseal`` returns a value without the prefix unchanged: rows written before sealing
  existed, until the migration that seals them has run. A sealed value no key opens is
  ``SecretUnreadableError``, never a garbage password.

Stdlib plus ``cryptography`` only.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

PREFIX = "tnsec:v1:"
KEY_ENV = "TN_SECRETS_KEY"
MIN_KEY_LENGTH = 32


class SecretKeyMissingError(RuntimeError):
    """TN_SECRETS_KEY is unset or too short, so credentials cannot be sealed or unsealed."""


class SecretUnreadableError(RuntimeError):
    """A sealed value that none of the configured keys opens (wrong or rotated-out key)."""


def _box() -> MultiFernet:
    keys = [k.strip() for k in os.getenv(KEY_ENV, "").split(",") if k.strip()]
    if not keys or any(len(k) < MIN_KEY_LENGTH for k in keys):
        raise SecretKeyMissingError(
            f"{KEY_ENV} must be set to at least {MIN_KEY_LENGTH} random characters "
            "(comma-separated to rotate: newest first) before credentials can be stored or used"
        )
    return MultiFernet([Fernet(base64.urlsafe_b64encode(hashlib.sha256(k.encode()).digest())) for k in keys])


def is_sealed(value: str | None) -> bool:
    return bool(value) and value.startswith(PREFIX)


def seal(plain: str | None) -> str | None:
    """The value to store for ``plain``. None and "" mean "no secret" and stay as they are."""
    if not plain:
        return plain
    return PREFIX + _box().encrypt(plain.encode()).decode()


def unseal(stored: str | None) -> str | None:
    """The secret a stored value holds (see the module docstring for unsealed values)."""
    if not is_sealed(stored):
        return stored
    try:
        return _box().decrypt(stored[len(PREFIX) :].encode()).decode()
    except InvalidToken as exc:
        raise SecretUnreadableError(
            f"a stored credential cannot be decrypted with {KEY_ENV}; was the key changed?"
        ) from exc
