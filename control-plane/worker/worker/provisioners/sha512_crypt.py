"""SHA-512-crypt (``$6$``), in pure Python: the hash Linux ``/etc/shadow`` and cloud-init take.

Ulrich Drepper's specification ("Unix crypt using SHA-256 and SHA-512", the algorithm glibc
implements). Here because ``crypt`` is deprecated (gone in Python 3.13) and absent or
without SHA-512 on some platforms, and a range password must never go into a VM's
cloud-config as plain text. Checked against the specification's test vectors and OpenSSL
(tests/worker/test_range_accounts_deploy.py).
"""

from __future__ import annotations

import hashlib
import secrets

_B64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_SALT_CHARS = _B64
ROUNDS_DEFAULT = 5000
_ROUNDS_MIN, _ROUNDS_MAX = 1000, 999_999_999

# The byte order of the final digest's base-64 encoding (specification, step 22e).
_ORDER = [
    (0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4), (47, 5, 26), (6, 27, 48),
    (28, 49, 7), (50, 8, 29), (9, 30, 51), (31, 52, 10), (53, 11, 32), (12, 33, 54), (34, 55, 13),
    (56, 14, 35), (15, 36, 57), (37, 58, 16), (59, 17, 38), (18, 39, 60), (40, 61, 19), (62, 20, 41),
]


def _b64(b2: int, b1: int, b0: int, n: int) -> str:
    w = (b2 << 16) | (b1 << 8) | b0
    out = []
    for _ in range(n):
        out.append(_B64[w & 0x3F])
        w >>= 6
    return "".join(out)


def _repeat(digest: bytes, length: int) -> bytes:
    return (digest * (length // 64 + 1))[:length]


def sha512_crypt(password: str, salt: str | None = None, rounds: int | None = None) -> str:
    """``$6$[rounds=N$]salt$hash`` for ``password``. A random 16-character salt by default."""
    pw = password.encode("utf-8")
    if salt is None:
        salt = "".join(secrets.choice(_SALT_CHARS) for _ in range(16))
    s = salt.encode("utf-8")[:16]
    explicit = rounds is not None
    n = ROUNDS_DEFAULT if rounds is None else max(_ROUNDS_MIN, min(_ROUNDS_MAX, rounds))

    b = hashlib.sha512(pw + s + pw).digest()
    a = hashlib.sha512(pw + s)
    a.update(_repeat(b, len(pw)))
    i = len(pw)
    while i:
        a.update(b if i & 1 else pw)
        i >>= 1
    a_digest = a.digest()

    dp = hashlib.sha512(pw * len(pw)).digest()
    p = _repeat(dp, len(pw))
    ds = hashlib.sha512(s * (16 + a_digest[0])).digest()
    sb = _repeat(ds, len(s))

    c = a_digest
    for r in range(n):
        h = hashlib.sha512()
        h.update(p if r & 1 else c)
        if r % 3:
            h.update(sb)
        if r % 7:
            h.update(p)
        h.update(c if r & 1 else p)
        c = h.digest()

    encoded = "".join(_b64(c[x], c[y], c[z], 4) for x, y, z in _ORDER) + _b64(0, 0, c[63], 2)
    prefix = f"$6$rounds={n}$" if explicit else "$6$"
    return f"{prefix}{s.decode('utf-8')}${encoded}"
