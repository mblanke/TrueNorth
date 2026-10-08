"""A column type that seals its value at rest (app/secretbox.py) and unseals it on load.

For a secret only server code ever reads, so its call sites can stay unchanged: Python
sees the plain value, the database holds ``tnsec:v1:...``. A value already sealed is
stored as it is, and a legacy plain value (a row the sealing migration has not reached)
loads as it is, exactly as ``secretbox.unseal`` treats it.

Not for a column anything filters or joins on: the same secret seals differently every time.
"""

from __future__ import annotations

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from . import secretbox


class SealedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or secretbox.is_sealed(value):
            return value
        return secretbox.seal(value)

    def process_result_value(self, value, dialect):
        return secretbox.unseal(value)
