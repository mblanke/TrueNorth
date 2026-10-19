"""Seals for typed-in range-account passwords in stored templates, bound to that purpose.

``app/secretbox.py`` seals hypervisor, AI and LTI secrets with one key; a value it sealed
must not be accepted as a range password (pasted into a template's ``accounts:``, it would
be unsealed into a credential anyone with ``range:credentials_read`` can read). So these
tokens use their own prefix and their own key, derived from ``TN_SECRETS_KEY`` with HKDF
(info ``truenorth/range-credentials/v1``): a secretbox token neither carries the prefix nor
opens with the key. Rotation follows ``TN_SECRETS_KEY`` (comma-separated, newest first).
``secretbox`` and its users are unchanged.
"""

from __future__ import annotations

import base64
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .. import secretbox

PREFIX = "tnsec:rc1:"
_INFO = b"truenorth/range-credentials/v1"


class NotARangeSealError(ValueError):
    """A sealed value this module did not make (another secretbox purpose, or forged)."""


def _box() -> MultiFernet:
    keys = [k.strip() for k in os.getenv(secretbox.KEY_ENV, "").split(",") if k.strip()]
    if not keys or any(len(k) < secretbox.MIN_KEY_LENGTH for k in keys):
        raise secretbox.SecretKeyMissingError(f"{secretbox.KEY_ENV} is not set; range passwords cannot be sealed")
    derived = [HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(k.encode()) for k in keys]
    return MultiFernet([Fernet(base64.urlsafe_b64encode(d)) for d in derived])


def looks_sealed(value: object) -> bool:
    """Any sealed-looking value (``tnsec:``), this module's or not."""
    return isinstance(value, str) and value.startswith("tnsec:")


def seal(plain: str) -> str:
    return PREFIX + _box().encrypt(plain.encode()).decode()


def unseal(token: str) -> str:
    """The password a range seal holds. ``NotARangeSealError`` for anything else."""
    if not token.startswith(PREFIX):
        raise NotARangeSealError("not a range-credential seal")
    try:
        return _box().decrypt(token[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise NotARangeSealError("a range-credential seal this platform cannot open") from exc
