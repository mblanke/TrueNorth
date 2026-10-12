"""The API's startup check of the realm's MFA configuration (review S1-5).

A deployment that upgrades the API without configuring its realm (Helm against an
existing Keycloak) would lock every member of staff out with 403s. In production the API
now refuses to start and names what is missing; the installer's compose path warns (its
60-keycloak configures the realm right after, and 95-smoke-test gates on it). The real
Keycloak side is proven by scripts/check_keycloak_mfa.sh (viii).
"""

from __future__ import annotations

import pytest
from app import settings
from app.auth_backends import keycloak_mfa_check as k

GOOD = {
    "browserFlow": "tn-browser",
    "attributes": {"acr.loa.map": '{"pwd":1,"mfa":2}', "tn.mfa.configured": "1:abc"},
}


def test_a_configured_realm_has_no_problems():
    assert k.realm_problems(GOOD) == []
    assert k.realm_problems({**GOOD, "browserFlow": "tn-browser--2"}) == []


@pytest.mark.parametrize(
    ("realm", "needle"),
    [
        ({**GOOD, "browserFlow": "browser"}, "browserFlow"),
        ({**GOOD, "attributes": {"tn.mfa.configured": "1:abc"}}, "acr.loa.map"),
        ({**GOOD, "attributes": {"acr.loa.map": '{"mfa":2}'}}, "tn.mfa.configured"),
    ],
)
def test_each_missing_piece_is_named(realm, needle):
    assert any(needle in p for p in k.realm_problems(realm))


@pytest.fixture
def prod(monkeypatch):
    monkeypatch.setenv("TN_ENV", "production")
    monkeypatch.delenv("TN_MFA_KEYCLOAK_CHECK", raising=False)
    monkeypatch.delenv("TN_MFA_ROLES", raising=False)
    monkeypatch.setenv("AUTH_BACKEND", "keycloak_oidc")
    return monkeypatch


def test_production_refuses_an_unconfigured_realm(prod):
    prod.setattr(k, "check", lambda: k.realm_problems({"browserFlow": "browser"}))
    with pytest.raises(settings.UnsafeSettingsError) as exc:
        k.enforce_at_startup()
    text = str(exc.value)
    assert "locked out" in text and "browserFlow" in text and "keycloak_mfa.py" in text


def test_production_refuses_without_an_admin_client(prod):
    for name in ("KEYCLOAK_ADMIN_CLIENT_ID", "KEYCLOAK_ADMIN_CLIENT_SECRET"):
        prod.delenv(name, raising=False)
    prod.setenv("KEYCLOAK_URL", "http://keycloak:8080/auth")
    with pytest.raises(settings.UnsafeSettingsError, match="KEYCLOAK_ADMIN_CLIENT_ID"):
        k.enforce_at_startup()


def test_warn_mode_logs_and_starts(prod, caplog):
    prod.setenv("TN_MFA_KEYCLOAK_CHECK", "warn")
    prod.setattr(k, "check", lambda: ["browserFlow is 'browser'"])
    k.enforce_at_startup()
    assert "browserFlow" in caplog.text


def test_an_unreachable_keycloak_never_blocks_the_start(prod, caplog):
    def down():
        raise k.KeycloakUnreachableError("ConnectError")

    prod.setattr(k, "check", down)
    k.enforce_at_startup()
    assert "could not read the Keycloak realm" in caplog.text


def test_off_outside_production_and_without_mfa_roles(monkeypatch):
    monkeypatch.setenv("TN_ENV", "development")
    monkeypatch.delenv("TN_MFA_KEYCLOAK_CHECK", raising=False)
    monkeypatch.setattr(k, "check", lambda: pytest.fail("must not check"))
    k.enforce_at_startup()
    monkeypatch.setenv("TN_ENV", "production")
    monkeypatch.setenv("TN_MFA_ROLES", "")
    k.enforce_at_startup()


def test_installer_compose_path_warns_and_helm_can_choose():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert "TN_MFA_KEYCLOAK_CHECK=warn" in (root / "install/roles/tn_config/templates/env.production.j2").read_text()
    assert "TN_MFA_KEYCLOAK_CHECK" in (root / "infra/k8s/helm/truenorth-range/templates/configmap.yaml").read_text()
    assert "Upgrading to v1.2.0" in (root / "infra/k8s/README.md").read_text()



# ── Review 2: a refused admin client is a configuration error; junk answers do not crash ──
class _Resp:
    def __init__(self, status, body=None, text=False):
        self.status_code, self._body, self._text = status, body, text

    def json(self):
        if self._text:
            raise ValueError("not JSON")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("x", request=None, response=None)


class _Client:
    def __init__(self, token, realm):
        self._token, self._realm = token, realm

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **k):
        return self._token

    def get(self, *a, **k):
        return self._realm


@pytest.fixture
def configured(monkeypatch):
    for name, value in (("KEYCLOAK_URL", "http://kc/auth"), ("KEYCLOAK_ADMIN_CLIENT_ID", "truenorth-api-admin"),
                        ("KEYCLOAK_ADMIN_CLIENT_SECRET", "s")):
        monkeypatch.setenv(name, value)
    import httpx

    def use(token, realm):
        monkeypatch.setattr(httpx, "Client", lambda **k: _Client(token, realm))

    return use


@pytest.mark.parametrize("token, realm", [(_Resp(401), None), (_Resp(200, {"access_token": "t"}), _Resp(403))])
def test_a_refused_admin_client_is_reported_as_misconfigured(configured, token, realm):
    configured(token, realm)
    [problem] = k.check()
    assert "misconfigured Admin API client" in problem


@pytest.mark.parametrize("token, realm", [(_Resp(200, text=True), None), (_Resp(200, {}), None),
                                         (_Resp(200, {"access_token": "t"}), _Resp(200, text=True))])
def test_junk_answers_count_as_unreachable_not_a_crash(configured, token, realm):
    configured(token, realm)
    with pytest.raises(k.KeycloakUnreachableError):
        k.check()


def test_a_configured_realm_passes_through_check(configured):
    configured(_Resp(200, {"access_token": "t"}), _Resp(200, GOOD))
    assert k.check() == []
