"""``GET /auth/me``: the identity state the SPA routes on (moved out of schemas.py, ADR 0003)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .schemas import RegistrationPrefill, RegistrationRequestOut, RegistrationSuggestions, UserFullOut


class AuthMeOut(BaseModel):
    """Identity state for the SPA — always 200 on a valid token.

    A discriminated union on ``status`` so the client never has to interpret an
    error response to decide where to send someone.

    ``mfa_required`` / ``mfa_satisfied`` (v1.2.0): whether this registered user's role
    needs a second factor and whether this token shows one (app/mfa_policy.py). When
    required and not satisfied, every other route answers 403 ``mfa_required`` and the
    SPA re-authenticates asking for acr "mfa" (step-up).

    ``acknowledgement_required`` (ADR 0014 decision 3, #279): this user must read and agree
    to the rules of use (app/acknowledgement/rule.py). While true, every route but the
    acknowledgement ones answers 403 ``acknowledgement_required``. The SPA deals with MFA
    first, then this.
    """

    status: str  # registered | pending | rejected | unregistered
    user: UserFullOut | None = None
    platform_admin: bool = False  # rbac.is_platform_admin: the UI shows platform-wide actions (display only)
    request: RegistrationRequestOut | None = None
    prefill: RegistrationPrefill | None = None
    suggestions: RegistrationSuggestions | None = None
    mfa_required: bool = False
    mfa_satisfied: bool = True
    acknowledgement_required: bool = False
    # ADR 0014 §4 (#280): classes whose seat this person lost for not signing in in time. A
    # forfeited roll-call entry never admits anyone; the SPA tells them to ask their instructor.
    roll_call_forfeited: list[str] = Field(default_factory=list)
