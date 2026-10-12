"""``mfa_policy.mfa_satisfied`` and the IdP ``tn_session`` strip (v1.2.0, F1 and F6).

F1: identity.py treated any ``acr`` other than "" or "0" as MFA, and Keycloak issues
``acr="1"`` for every password-only sign-in. F6: ``TokenPayload(**claims)`` kept a
``tn_session`` claim from an IdP token; only TrueNorth may set it.
"""

from __future__ import annotations

import pytest
from app import mfa_policy
from app.auth import TokenPayload, token_from_idp_claims


@pytest.mark.parametrize(
    "amr, acr",
    [
        ([], ""),
        ([], "0"),
        ([], "1"),  # F1: a fresh password sign-in
        (["pwd"], "1"),
        (["pwd", "kba"], None),
        ("pwd", ""),
    ],
)
def test_password_only_is_not_mfa(amr, acr):
    assert not mfa_policy.mfa_satisfied_claims(amr, acr)


@pytest.mark.parametrize(
    "amr, acr",
    [
        (["pwd", "otp"], "1"),
        (["hwk"], ""),
        (["OTP"], ""),
        ("otp", ""),
        (["swk"], ""),
        ([], "mfa"),
        ([], "MFA"),
    ],
)
def test_second_factor_is_mfa(amr, acr):
    assert mfa_policy.mfa_satisfied_claims(amr, acr)


def test_config_cannot_make_acr_1_count(monkeypatch):
    monkeypatch.setenv("TN_MFA_ACR_VALUES", "mfa,1,0,")
    assert not mfa_policy.mfa_satisfied_claims([], "1")
    assert not mfa_policy.mfa_satisfied_claims([], "0")
    assert not mfa_policy.mfa_satisfied_claims([], "")
    assert mfa_policy.mfa_satisfied_claims([], "mfa")
    assert "'1'" in mfa_policy.configured_acr_problem()


def test_default_acr_config_has_no_problem(monkeypatch):
    monkeypatch.delenv("TN_MFA_ACR_VALUES", raising=False)
    assert mfa_policy.configured_acr_problem() is None


def test_amr_values_are_configurable(monkeypatch):
    monkeypatch.setenv("TN_MFA_AMR_VALUES", "hwk")
    assert not mfa_policy.mfa_satisfied_claims(["otp"], "")
    assert mfa_policy.mfa_satisfied_claims(["hwk"], "")


def test_satisfied_reads_a_token_or_a_dict():
    assert mfa_policy.mfa_satisfied(TokenPayload(sub="x", amr=["pwd", "otp"]))
    assert not mfa_policy.mfa_satisfied(TokenPayload(sub="x", acr="1"))
    assert mfa_policy.mfa_satisfied({"amr": ["hwk"]})


def test_idp_claims_cannot_set_tn_session():
    """F6: an IdP token naming a TrueNorth session kind loses that claim."""
    token = token_from_idp_claims({"sub": "kc-1", "tn_session": "inprocess", "amr": ["pwd"]})
    assert token.tn_session == ""
    assert token.sub == "kc-1" and token.amr == ["pwd"]


def test_config_cannot_make_pwd_count(monkeypatch):
    monkeypatch.setenv("TN_MFA_AMR_VALUES", "otp,pwd,kba")
    assert not mfa_policy.mfa_satisfied_claims(["pwd"], "")
    assert not mfa_policy.mfa_satisfied_claims(["kba"], "")
    assert mfa_policy.mfa_satisfied_claims(["otp"], "")
    assert "'pwd'" in mfa_policy.configured_amr_problem()
    monkeypatch.delenv("TN_MFA_AMR_VALUES")
    assert mfa_policy.configured_amr_problem() is None
