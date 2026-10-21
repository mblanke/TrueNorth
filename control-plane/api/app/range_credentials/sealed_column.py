"""The column type of a stored range password: sealed with the range-credential key (ADR 0013 §3).

Writes ``tnsec:rc1:...`` (``seal.py``: a key derived from ``TN_SECRETS_KEY`` for range
credentials only), so a value from the general secretbox (hypervisor, AI and LTI secrets) is
never a range password and the other way round. Reads also accept what came before: a
``tnsec:v1:`` value (rows sealed by ADR 0012's first slice, until the ADR 0013 migration
reseals them) and a legacy plain value. A ``tnsec:v1:`` value handed in for writing is
opened and sealed again as ``rc1``.

Not for a column anything filters or joins on: the same password seals differently each time.
"""

from __future__ import annotations

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from .. import secretbox
from . import seal as rc_seal


class RangeSealedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or value.startswith(rc_seal.PREFIX):
            return value
        if secretbox.is_sealed(value):
            value = secretbox.unseal(value)
        return rc_seal.seal(value)

    def process_result_value(self, value, dialect):
        return unseal_stored(value)


def unseal_stored(value: str | None) -> str | None:
    """The password a stored value holds: ``rc1``, ``v1`` or (legacy) plain."""
    if value is None:
        return None
    if value.startswith(rc_seal.PREFIX):
        return rc_seal.unseal(value)
    return secretbox.unseal(value)
