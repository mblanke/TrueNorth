"""Seals for range passwords, each bound to what it belongs to (ADR 0013 §3).

``app/secretbox.py`` seals hypervisor, AI and LTI secrets with one key; nothing it sealed is
a range password, and no range seal opens anywhere but its own place. Two kinds, each with
its own key derived from ``TN_SECRETS_KEY`` (HKDF-SHA256, distinct ``info``) and its own
prefix, sealed with AES-256-GCM whose associated data names the owner:

==============  =====================================  ==========================================
prefix          what                                   associated data
==============  =====================================  ==========================================
``tnsec:rt1:``  a typed-in password in a stored         ``template|<tenant id or "global">``
                template's ``accounts:`` (seal_yaml)
``tnsec:rk1:``  a stored range credential (its          ``row|<tenant>|<range>|<credential id>``
                password, the previous one, and the     (a snapshot's copy keeps its account's)
                copies snapshots keep of it)
==============  =====================================  ==========================================

So a stored credential pasted into a template's ``password:`` is not a template seal and is
refused, and a template seal pasted into another tenant's template does not open (the
review's decryption-oracle finding on #250). The tenant, not the template id, binds a
template seal: the range designer saves a copy of a tenant's template under a new id.

``tnsec:rc1:`` (Fernet, ADR 0012 and S3's first cut) is read only to migrate it
(``unseal_legacy``); nothing accepts it from a template any more.
Rotation follows ``TN_SECRETS_KEY`` (comma-separated, newest first). Stdlib + ``cryptography``.
"""

from __future__ import annotations

import base64
import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .. import secretbox

TEMPLATE_PREFIX = "tnsec:rt1:"
ROW_PREFIX = "tnsec:rk1:"
LEGACY_PREFIX = "tnsec:rc1:"
PREFIX = TEMPLATE_PREFIX  # what a template's typed-in password is stored as
_TEMPLATE_INFO = b"truenorth/range-credentials/template/v2"
_ROW_INFO = b"truenorth/range-credentials/row/v1"
_LEGACY_INFO = b"truenorth/range-credentials/v1"


class NotARangeSealError(ValueError):
    """A sealed value this module did not make for this place (another purpose, owner or key)."""


def _secrets() -> list[str]:
    keys = [k.strip() for k in os.getenv(secretbox.KEY_ENV, "").split(",") if k.strip()]
    if not keys or any(len(k) < secretbox.MIN_KEY_LENGTH for k in keys):
        raise secretbox.SecretKeyMissingError(f"{secretbox.KEY_ENV} is not set; range passwords cannot be sealed")
    return keys


def _derive(info: bytes) -> list[bytes]:
    return [HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(k.encode()) for k in _secrets()]


def _seal(prefix: str, info: bytes, plain: str, aad: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(_derive(info)[0]).encrypt(nonce, plain.encode(), aad.encode())
    return prefix + base64.urlsafe_b64encode(nonce + ct).decode()


def _open(prefix: str, info: bytes, token: str, aad: str) -> str:
    if not isinstance(token, str) or not token.startswith(prefix):
        raise NotARangeSealError("not a seal of this kind")
    try:
        raw = base64.urlsafe_b64decode(token[len(prefix):].encode())
    except (ValueError, TypeError):
        raise NotARangeSealError("a malformed seal") from None
    for key in _derive(info):
        try:
            return AESGCM(key).decrypt(raw[:12], raw[12:], aad.encode()).decode()
        except (InvalidTag, ValueError):
            continue
    raise NotARangeSealError("a seal this platform cannot open here")


def looks_sealed(value: object) -> bool:
    """Any sealed-looking value (``tnsec:``), this module's or not."""
    return isinstance(value, str) and value.startswith("tnsec:")


# ── Template seals ──────────────────────────────────────────────────────
def _template_aad(tenant_id: uuid.UUID | str | None) -> str:
    return f"template|{tenant_id or 'global'}"


def seal(plain: str, tenant_id: uuid.UUID | str | None) -> str:
    """A typed-in template password, sealed for templates of ``tenant_id``."""
    return _seal(TEMPLATE_PREFIX, _TEMPLATE_INFO, plain, _template_aad(tenant_id))


def unseal(token: str, tenant_id: uuid.UUID | str | None) -> str:
    """The password a template seal of ``tenant_id`` holds. ``NotARangeSealError`` for anything
    else: another tenant's template seal, a stored credential, a secretbox value, ``rc1``."""
    return _open(TEMPLATE_PREFIX, _TEMPLATE_INFO, token, _template_aad(tenant_id))


# ── Stored credential seals ─────────────────────────────────────────────
def row_aad(tenant_id, range_id, credential_id) -> str:
    return f"row|{tenant_id}|{range_id}|{credential_id}"


def seal_row(plain: str, aad: str) -> str:
    return _seal(ROW_PREFIX, _ROW_INFO, plain, aad)


def unseal_row(token: str | None, aad: str) -> str | None:
    """A stored credential bound to ``aad``. Values from before ADR 0013's sealing (``rc1``,
    ``tnsec:v1``, plain) are read as they are until the migration reseals them."""
    if token is None:
        return None
    if token.startswith(ROW_PREFIX):
        return _open(ROW_PREFIX, _ROW_INFO, token, aad)
    if token.startswith(LEGACY_PREFIX):
        return unseal_legacy(token)
    return secretbox.unseal(token)


def unseal_legacy(token: str) -> str:
    """A ``tnsec:rc1:`` value (Fernet, no owner bound): for the migration and legacy rows only."""
    if not token.startswith(LEGACY_PREFIX):
        raise NotARangeSealError("not a legacy range seal")
    box = MultiFernet([Fernet(base64.urlsafe_b64encode(k)) for k in _derive(_LEGACY_INFO)])
    try:
        return box.decrypt(token[len(LEGACY_PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise NotARangeSealError("a legacy range seal this platform cannot open") from exc


def seal_legacy(plain: str) -> str:
    """``tnsec:rc1:`` for the downgrade only (the code before ADR 0013 reads it)."""
    box = Fernet(base64.urlsafe_b64encode(_derive(_LEGACY_INFO)[0]))
    return LEGACY_PREFIX + box.encrypt(plain.encode()).decode()
