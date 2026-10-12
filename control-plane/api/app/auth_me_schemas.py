"""``GET /auth/me``: the identity state the SPA routes on (moved out of schemas.py, ADR 0003)."""

from __future__ import annotations

from pydantic import BaseModel

from .schemas import RegistrationPrefill, RegistrationRequestOut, RegistrationSuggestions, UserFullOut


class AuthMeOut(BaseModel):
    """Identity state for the SPA — always 200 on a valid token.

    A discriminated union on ``status`` so the client never has to interpret an
    error response to decide where to send someone.

    ``mfa_required`` / ``mfa_satisfied`` (v1.2.0): whether this registered user's role
    needs a second factor and whether this token shows one (app/mfa_policy.py). When
    required and not satisfied, every other route answers 403 ``mfa_required`` and the
    SPA re-authenticates asking for acr "mfa" (step-up).
    """

    status: str  # registered | pending | rejected | unregistered
    user: UserFullOut | None = None
    platform_admin: bool = False  # rbac.is_platform_admin: the UI shows platform-wide actions (display only)
    request: RegistrationRequestOut | None = None
    prefill: RegistrationPrefill | None = None
    suggestions: RegistrationSuggestions | None = None
    mfa_required: bool = False
    mfa_satisfied: bool = True
