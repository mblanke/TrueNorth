"""Tests for the pluggable Auth backend system (Phase 2 — Auth modularity)."""

from __future__ import annotations

import os

import httpx
import pytest
from fastapi import HTTPException

from app.auth_backends import (
    DisabledAuthBackend,
    GenericOIDCBackend,
    KeycloakOIDCBackend,
    get_auth_backend,
)
from app.auth_backends import _reset_backend  # noqa: PLC2701 — test-only helper


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_auth_singleton():
    """Ensure every test starts with a fresh singleton."""
    _reset_backend()
    yield
    _reset_backend()


_MOCK_JWKS = {
    "keys": [
        {
            "kty": "RSA",
            "kid": "test-key",
            "use": "sig",
            "n": "test",
            "e": "AQAB",
        }
    ]
}

_MOCK_PAYLOAD = {
    "sub": "user-123",
    "email": "tester@truenorth.local",
    "preferred_username": "tester",
    "realm_access": {"roles": ["user"]},
    "tenant_id": "",
}


# ---------------------------------------------------------------------------
# DisabledAuthBackend
# ---------------------------------------------------------------------------


class TestDisabledAuthBackend:
    @pytest.mark.asyncio
    async def test_health_check_returns_true(self):
        backend = DisabledAuthBackend()
        assert await backend.health_check() is True


# ---------------------------------------------------------------------------
# KeycloakOIDCBackend
# ---------------------------------------------------------------------------


class TestKeycloakOIDCBackend:
    def _make_backend(self) -> KeycloakOIDCBackend:
        return KeycloakOIDCBackend(
            keycloak_url="http://mock-keycloak:8080",
            realm="test-realm",
        )

    def test_jwks_url_constructed_correctly(self):
        b = self._make_backend()
        assert b._jwks_url == "http://mock-keycloak:8080/realms/test-realm/protocol/openid-connect/certs"

    def test_trailing_slash_stripped(self):
        b = KeycloakOIDCBackend(keycloak_url="http://kc:8080/", realm="r")
        assert not b._url.endswith("/")

    @pytest.mark.asyncio
    async def test_validate_token_success(self, respx_mock, mocker):
        b = self._make_backend()
        # Stub JWKS fetch
        respx_mock.get(b._jwks_url).mock(return_value=httpx.Response(200, json=_MOCK_JWKS))
        # Stub jwt.decode to return our payload
        mocker.patch("app.auth_backends.keycloak_oidc.jwt.decode", return_value=_MOCK_PAYLOAD)

        payload = await b.validate_token("fake.jwt.token")
        assert payload["sub"] == "user-123"

    @pytest.mark.asyncio
    async def test_validate_token_invalid_jwt_raises_401(self, respx_mock, mocker):
        from jose import JWTError

        b = self._make_backend()
        respx_mock.get(b._jwks_url).mock(return_value=httpx.Response(200, json=_MOCK_JWKS))
        mocker.patch("app.auth_backends.keycloak_oidc.jwt.decode", side_effect=JWTError("bad"))

        with pytest.raises(HTTPException) as exc_info:
            await b.validate_token("bad.token")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_validate_token_jwks_fetch_failure_raises_503(self, respx_mock, mocker):
        b = self._make_backend()
        # Circuit breaker raises CircuitOpenError
        from app.circuit_breaker import CircuitOpenError

        mocker.patch(
            "app.auth_backends.keycloak_oidc.keycloak_breaker.call",
            side_effect=CircuitOpenError("keycloak", 30),
        )

        with pytest.raises(HTTPException) as exc_info:
            await b.validate_token("any.token")
        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_health_check_success(self, respx_mock):
        b = self._make_backend()
        respx_mock.get(
            "http://mock-keycloak:8080/realms/test-realm/.well-known/openid-configuration"
        ).mock(return_value=httpx.Response(200, json={"issuer": "http://mock-keycloak:8080"}))
        assert await b.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_unreachable_returns_false(self, respx_mock):
        b = self._make_backend()
        respx_mock.get(
            "http://mock-keycloak:8080/realms/test-realm/.well-known/openid-configuration"
        ).mock(side_effect=httpx.ConnectError("refused"))
        assert await b.health_check() is False

    def test_reads_env_vars_when_no_explicit_args(self, monkeypatch):
        monkeypatch.setenv("KEYCLOAK_URL", "http://env-kc:9999")
        monkeypatch.setenv("KEYCLOAK_REALM", "env-realm")
        b = KeycloakOIDCBackend()
        assert b._url == "http://env-kc:9999"
        assert b._realm == "env-realm"

    @pytest.mark.asyncio
    async def test_jwks_cached_on_second_call(self, respx_mock, mocker):
        b = self._make_backend()
        route = respx_mock.get(b._jwks_url).mock(
            return_value=httpx.Response(200, json=_MOCK_JWKS)
        )
        mocker.patch("app.auth_backends.keycloak_oidc.jwt.decode", return_value=_MOCK_PAYLOAD)
        await b.validate_token("tok1")
        await b.validate_token("tok2")
        # JWKS endpoint should only be hit once
        assert route.call_count == 1


# ---------------------------------------------------------------------------
# GenericOIDCBackend
# ---------------------------------------------------------------------------


class TestGenericOIDCBackend:
    def _make_backend(self) -> GenericOIDCBackend:
        return GenericOIDCBackend(
            jwks_url="https://mock-idp.example.com/.well-known/jwks.json",
            audience="api://truenorth",
            algorithms=["RS256"],
        )

    def test_missing_jwks_url_raises(self, monkeypatch):
        monkeypatch.delenv("OIDC_JWKS_URL", raising=False)
        with pytest.raises(ValueError, match="OIDC_JWKS_URL is required"):
            GenericOIDCBackend()

    @pytest.mark.asyncio
    async def test_validate_token_success(self, respx_mock, mocker):
        b = self._make_backend()
        respx_mock.get(b._jwks_url).mock(return_value=httpx.Response(200, json=_MOCK_JWKS))
        mocker.patch("app.auth_backends.generic_oidc.jwt.decode", return_value=_MOCK_PAYLOAD)
        payload = await b.validate_token("fake.jwt.token")
        assert payload["sub"] == "user-123"

    @pytest.mark.asyncio
    async def test_validate_token_invalid_jwt_raises_401(self, respx_mock, mocker):
        from jose import JWTError

        b = self._make_backend()
        respx_mock.get(b._jwks_url).mock(return_value=httpx.Response(200, json=_MOCK_JWKS))
        mocker.patch("app.auth_backends.generic_oidc.jwt.decode", side_effect=JWTError("bad"))

        with pytest.raises(HTTPException) as exc_info:
            await b.validate_token("bad.token")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_jwks_fetch_failure_raises_503(self, respx_mock):
        b = self._make_backend()
        respx_mock.get(b._jwks_url).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(HTTPException) as exc_info:
            await b.validate_token("any.token")
        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_health_check_success(self, respx_mock):
        b = self._make_backend()
        respx_mock.get(b._jwks_url).mock(return_value=httpx.Response(200, json=_MOCK_JWKS))
        assert await b.health_check() is True

    def test_reads_env_vars_when_no_explicit_args(self, monkeypatch):
        monkeypatch.setenv("OIDC_JWKS_URL", "https://env-idp.example.com/keys")
        monkeypatch.setenv("OIDC_AUDIENCE", "api://env-app")
        monkeypatch.setenv("OIDC_ALGORITHMS", "RS256,ES256")
        b = GenericOIDCBackend()
        assert b._jwks_url == "https://env-idp.example.com/keys"
        assert b._audience == "api://env-app"
        assert "ES256" in b._algorithms


# ---------------------------------------------------------------------------
# get_auth_backend factory
# ---------------------------------------------------------------------------


class TestGetAuthBackend:
    def test_default_returns_keycloak(self, monkeypatch):
        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        monkeypatch.delenv("AUTH_BACKEND", raising=False)
        backend = get_auth_backend()
        assert isinstance(backend, KeycloakOIDCBackend)

    def test_explicit_keycloak_oidc(self, monkeypatch):
        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        monkeypatch.setenv("AUTH_BACKEND", "keycloak_oidc")
        backend = get_auth_backend()
        assert isinstance(backend, KeycloakOIDCBackend)

    def test_generic_oidc(self, monkeypatch):
        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        monkeypatch.setenv("AUTH_BACKEND", "generic_oidc")
        monkeypatch.setenv("OIDC_JWKS_URL", "https://idp.example.com/keys")
        backend = get_auth_backend()
        assert isinstance(backend, GenericOIDCBackend)

    def test_disabled_via_env_var(self, monkeypatch):
        monkeypatch.setenv("AUTH_DISABLED", "true")
        backend = get_auth_backend()
        assert isinstance(backend, DisabledAuthBackend)

    def test_auth_disabled_takes_precedence_over_backend(self, monkeypatch):
        monkeypatch.setenv("AUTH_DISABLED", "true")
        monkeypatch.setenv("AUTH_BACKEND", "keycloak_oidc")
        backend = get_auth_backend()
        # AUTH_DISABLED wins
        assert isinstance(backend, DisabledAuthBackend)

    def test_unknown_backend_raises(self, monkeypatch):
        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        monkeypatch.setenv("AUTH_BACKEND", "ldap_magic")
        with pytest.raises(ValueError, match="Unknown AUTH_BACKEND"):
            get_auth_backend()

    def test_singleton_caching(self, monkeypatch):
        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        monkeypatch.setenv("AUTH_BACKEND", "disabled")
        b1 = get_auth_backend()
        b2 = get_auth_backend()
        assert b1 is b2

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.delenv("AUTH_DISABLED", raising=False)
        monkeypatch.setenv("AUTH_BACKEND", "DISABLED")
        backend = get_auth_backend()
        assert isinstance(backend, DisabledAuthBackend)
