"""Did this sign-in use a second factor, and does this user need one? One answer for every caller.

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

import logging
import os
from collections.abc import Iterable

logger = logging.getLogger(__name__)

DEFAULT_AMR_VALUES = "otp,hwk,swk,mfa"
DEFAULT_ACR_VALUES = "mfa"

# Keycloak's numeric levels for "no context" and "password". They never prove MFA,
# so they are dropped from TN_MFA_ACR_VALUES even if an operator lists them.
NEVER_MFA_ACR = frozenset({"", "0", "1"})
# amr entries that are a single factor (the password, knowledge-based): never MFA, whatever
# TN_MFA_AMR_VALUES lists.
NEVER_MFA_AMR = frozenset({"", "pwd", "kba"})


def _csv(name: str, default: str) -> frozenset[str]:
    raw = os.getenv(name)
    if raw is None:
        raw = default
    return frozenset(v.strip().lower() for v in raw.split(","))


def amr_values() -> frozenset[str]:
    """The amr entries that count as a second factor; never "pwd" or "kba"."""
    return _csv("TN_MFA_AMR_VALUES", DEFAULT_AMR_VALUES) - NEVER_MFA_AMR


def acr_values() -> frozenset[str]:
    """The acr values that count as a second factor; never "", "0" or "1"."""
    return _csv("TN_MFA_ACR_VALUES", DEFAULT_ACR_VALUES) - NEVER_MFA_ACR


def configured_amr_problem() -> str | None:
    """Why TN_MFA_AMR_VALUES as configured is unsafe, or None. For production checks."""
    bad = sorted((_csv("TN_MFA_AMR_VALUES", DEFAULT_AMR_VALUES) & NEVER_MFA_AMR) - {""})
    if bad:
        return f"TN_MFA_AMR_VALUES lists {', '.join(repr(b) for b in bad)}: those are a single factor"
    return None


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


# ── Who must use MFA, enforced on every request (auth.get_current_user) ──
# The role is the database's (users.role), never the token's: when the two disagree
# (an admin promoted someone in the UI before their Keycloak groups changed) the API
# refuses with mfa_required and the SPA steps up. Divergence costs a prompt, never access.
#
# ``TN_MFA_ROLES``          roles that must sign in with a second factor; default
#                           ``admin,instructor,range_ops,observer``. Production refuses
#                           to start without all four (settings.production_problems).
# ``TN_MFA_DEMO_EXEMPT``    test hosts only (TN_TEST_HOST): the shipped demo staff
#                           (``*.demo@truenorth.test``) skip MFA. Ignored anywhere else,
#                           refused at startup in production without TN_TEST_HOST, and
#                           each use is audited.
DEFAULT_MFA_ROLES = "admin,instructor,range_ops,observer"
PRIVILEGED_ROLES = frozenset(DEFAULT_MFA_ROLES.split(","))
DEMO_EMAIL_SUFFIX = ".demo@truenorth.test"
INPROCESS_SESSION = "inprocess"
LTI_SESSION = "lti"
MFA_REQUIRED_DETAIL = {
    "code": "mfa_required",
    "state": "mfa_required",
    "message": "Your role requires multi-factor sign-in",
    "next": "/auth/me",
}


def mfa_roles() -> frozenset[str]:
    return _csv("TN_MFA_ROLES", DEFAULT_MFA_ROLES) - {""}


def _role_value(role: object) -> str:
    return str(getattr(role, "value", role) or "").lower()


def mfa_required_for(role: object) -> bool:
    return _role_value(role) in mfa_roles()


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def is_test_host() -> bool:
    """TN_TEST_HOST (the same switch as course_releases.test_content)."""
    return _flag("TN_TEST_HOST")


def demo_exemption_requested() -> bool:
    return _flag("TN_MFA_DEMO_EXEMPT")


def demo_exemption_active() -> bool:
    """Only where both TN_MFA_DEMO_EXEMPT and TN_TEST_HOST are on."""
    return demo_exemption_requested() and is_test_host()


def is_demo_email(email: str | None) -> bool:
    e = (email or "").strip().lower()
    local = e[: -len(DEMO_EMAIL_SUFFIX)] if e.endswith(DEMO_EMAIL_SUFFIX) else ""
    return bool(local) and "@" not in local


def demo_exempt(email: str | None) -> bool:
    return demo_exemption_active() and is_demo_email(email)


def mfa_denial_reason(identity: object, role: object, email: str | None = None) -> str | None:
    """None when this request may proceed; otherwise why MFA is missing.

    Order: not required for the role -> proceed; the in-process harness (host root and
    Docker, audited at start) -> proceed; an LTI session for a privileged role -> refused
    (defence in depth: lti_identity.session only names Students); a second factor in the
    token -> proceed; the test-host demo exemption -> proceed (the caller audits it).
    """
    if not mfa_required_for(role):
        return None
    session = getattr(identity, "tn_session", "") or ""
    if session == INPROCESS_SESSION:
        return None
    if session == LTI_SESSION:
        return "an LTI session cannot carry a role that requires multi-factor sign-in"
    if mfa_satisfied(identity):
        return None
    if demo_exempt(email):
        return None
    return "no second factor in the token (amr/acr)"


def mfa_denial(identity: object, role: object, email: str | None = None):
    """The 403 to raise, or None. Imported lazily so this module stays importable alone."""
    if mfa_denial_reason(identity, role, email) is None:
        return None
    from fastapi import HTTPException

    from .auth import AUTH_STATE_HEADER

    return HTTPException(status_code=403, detail=dict(MFA_REQUIRED_DETAIL),
                         headers={AUTH_STATE_HEADER: "mfa_required"})


def uses_demo_exemption(identity: object, role: object, email: str | None) -> bool:
    """True when this request proceeds only because of the demo exemption (to audit it)."""
    return (
        mfa_required_for(role)
        and (getattr(identity, "tn_session", "") or "") not in (INPROCESS_SESSION, LTI_SESSION)
        and not mfa_satisfied(identity)
        and demo_exempt(email)
    )


_demo_audited: dict[str, str] = {}


def audit_demo_exemption(db, user) -> None:
    """One audit row per demo user per UTC day per process: who skipped MFA, and why."""
    from datetime import UTC, datetime

    from .models import AuditLog

    today = datetime.now(UTC).date().isoformat()
    key = str(user.id)
    if _demo_audited.get(key) == today:
        return
    db.add(AuditLog(
        user_id=user.id, tenant_id=user.tenant_id, action="mfa_demo_exempt", resource_type="user",
        resource_id=key, detail="TN_MFA_DEMO_EXEMPT on a test host (TN_TEST_HOST): demo staff signed in without MFA",
    ))
    db.commit()
    _demo_audited[key] = today
    logger.warning("MFA skipped for demo user %s (TN_MFA_DEMO_EXEMPT on a test host)", user.id)


def production_problems() -> list[str]:
    """MFA settings production refuses (settings.production_problems)."""
    problems: list[str] = []
    missing = sorted(PRIVILEGED_ROLES - mfa_roles())
    if missing:
        problems.append(f"TN_MFA_ROLES leaves out {', '.join(missing)}: privileged roles must use MFA")
    for check in (configured_acr_problem(), configured_amr_problem()):
        if check:
            problems.append(check)
    if not amr_values() and not acr_values():
        problems.append("TN_MFA_AMR_VALUES and TN_MFA_ACR_VALUES are both empty: nothing could prove MFA")
    if demo_exemption_requested() and not is_test_host():
        problems.append("TN_MFA_DEMO_EXEMPT is on without TN_TEST_HOST: the demo MFA exemption is for test hosts only")
    return problems
