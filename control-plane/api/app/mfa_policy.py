"""Did this sign-in use a second factor? One answer for every caller.

Keycloak reports how a user signed in two ways:

* ``amr`` (Authentication Method Reference), a list such as ``["pwd", "otp"]``, emitted
  by the realm's AMR protocol mapper from each authenticator's reference value;
* ``acr`` (Authentication Context Class Reference), the level of authentication the
  flow reached, named by the realm's ``acr.loa.map`` (``{"pwd": 1, "mfa": 2}``).

``acr`` is NOT a yes/no MFA flag. Keycloak issues ``acr="1"`` for every fresh password
sign-in and ``"0"`` for a session-cookie one, so treating "any acr other than empty or
0" as MFA (identity.py before v1.2.0) let a password-only login count as MFA. Here an
``acr`` counts only when it is in an explicit allow-list, and "0", "1" and "" are
removed from that list whatever the configuration says.

Configuration (comma-separated, case-insensitive):

``TN_MFA_AMR_VALUES``  amr entries that prove a second factor; default ``otp,hwk,swk,mfa``.
``TN_MFA_ACR_VALUES``  acr values that prove a second factor; default ``mfa``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

DEFAULT_AMR_VALUES = "otp,hwk,swk,mfa"
DEFAULT_ACR_VALUES = "mfa"

# Keycloak's numeric levels for "no context" and "password". They never prove MFA,
# so they are dropped from TN_MFA_ACR_VALUES even if an operator lists them.
NEVER_MFA_ACR = frozenset({"", "0", "1"})


def _csv(name: str, default: str) -> frozenset[str]:
    raw = os.getenv(name)
    if raw is None:
        raw = default
    return frozenset(v.strip().lower() for v in raw.split(","))


def amr_values() -> frozenset[str]:
    """The amr entries that count as a second factor (empty entries dropped)."""
    return _csv("TN_MFA_AMR_VALUES", DEFAULT_AMR_VALUES) - {""}


def acr_values() -> frozenset[str]:
    """The acr values that count as a second factor; never "", "0" or "1"."""
    return _csv("TN_MFA_ACR_VALUES", DEFAULT_ACR_VALUES) - NEVER_MFA_ACR


def configured_acr_problem() -> str | None:
    """Why TN_MFA_ACR_VALUES as configured is unsafe, or None. For production checks."""
    listed = _csv("TN_MFA_ACR_VALUES", DEFAULT_ACR_VALUES)
    bad = sorted(listed & NEVER_MFA_ACR)
    if bad:
        shown = ", ".join(repr(b) for b in bad)
        return f"TN_MFA_ACR_VALUES lists {shown}: Keycloak issues those for a password-only sign-in"
    return None


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(v) for v in value]
    return [str(value)]


def mfa_satisfied_claims(amr: object, acr: object) -> bool:
    """Whether ``amr``/``acr`` claims show a second factor."""
    methods = {a.strip().lower() for a in _as_list(amr)}
    if methods & amr_values():
        return True
    level = str(acr if acr is not None else "").strip().lower()
    return level in acr_values()


def mfa_satisfied(identity: object) -> bool:
    """Whether a token identity (``auth.TokenPayload`` or a claims dict) shows MFA."""
    if isinstance(identity, dict):
        return mfa_satisfied_claims(identity.get("amr"), identity.get("acr"))
    return mfa_satisfied_claims(getattr(identity, "amr", None), getattr(identity, "acr", None))
