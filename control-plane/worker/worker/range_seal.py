"""Open stored range credentials (``tnsec:rk1:``, ADR 0013 §3).

The API seals every stored range password with a key derived for stored credentials only
(control-plane/api/app/range_credentials/seal.py: HKDF-SHA256, info
``truenorth/range-credentials/row/v1``, AES-256-GCM) and binds it to its owner through the
associated data (``row|<tenant>|<range>|<credential>``, ``snapshot|<tenant>|<snapshot>|<credential>``).
The worker cannot import the API, so this module derives the same key;
tests/contracts/test_range_seal_matches_api.py fails if the two drift. Values from before
the ADR 0013 migration (``tnsec:rc1:``, ``tnsec:v1:``, plain) are still read.

Opening only: the worker never makes a range password. To put a credential's previous
password back it copies the sealed value between the row's two columns, which share the
row's binding (db_ops.revert_credential_change).
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import secretbox

ROW_PREFIX = "tnsec:rk1:"
LEGACY_PREFIX = "tnsec:rc1:"
_ROW_INFO = b"truenorth/range-credentials/row/v1"
_LEGACY_INFO = b"truenorth/range-credentials/v1"


def _derive(info: bytes) -> list[bytes]:
    keys = [k.strip() for k in os.getenv(secretbox.KEY_ENV, "").split(",") if k.strip()]
    if not keys or any(len(k) < secretbox.MIN_KEY_LENGTH for k in keys):
        raise secretbox.SecretKeyMissingError(f"{secretbox.KEY_ENV} is not set; range passwords cannot be opened")
    return [HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(k.encode()) for k in keys]


def row_aad(tenant_id, range_id, credential_id) -> str:
    return f"row|{tenant_id}|{range_id}|{credential_id}"


def snapshot_aad(tenant_id, snapshot_id, credential_id) -> str:
    return f"snapshot|{tenant_id}|{snapshot_id}|{credential_id}"


def unseal(stored: str | None, aad: str) -> str | None:
    """The password a stored value bound to ``aad`` holds."""
    if stored is None:
        return None
    if stored.startswith(ROW_PREFIX):
        raw = base64.urlsafe_b64decode(stored[len(ROW_PREFIX):].encode())
        for key in _derive(_ROW_INFO):
            try:
                return AESGCM(key).decrypt(raw[:12], raw[12:], aad.encode()).decode()
            except (InvalidTag, ValueError):
                continue
        raise secretbox.SecretUnreadableError(
            f"a range password cannot be opened here with {secretbox.KEY_ENV} (another owner, or the key changed)")
    if stored.startswith(LEGACY_PREFIX):
        box = MultiFernet([Fernet(base64.urlsafe_b64encode(k)) for k in _derive(_LEGACY_INFO)])
        try:
            return box.decrypt(stored[len(LEGACY_PREFIX):].encode()).decode()
        except InvalidToken as exc:
            raise secretbox.SecretUnreadableError("a legacy range password cannot be opened") from exc
    return secretbox.unseal(stored)
