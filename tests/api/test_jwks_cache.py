"""JWKS caching across IdP signing-key rotation.

The OIDC backends are singletons. They used to fetch the JWKS once per process, so a
Keycloak (or generic OIDC) key rotation failed every login until the API was restarted.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import jwt
import pytest
from app.auth_backends.generic_oidc import GenericOIDCBackend
from app.auth_backends.jwks_cache import JWKSCache
from app.auth_backends.keycloak_oidc import KeycloakOIDCBackend
from app.jwks import unverified_kid
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jwt.algorithms import RSAAlgorithm


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _set(*kids: str) -> dict:
    return {"keys": [{"kty": "RSA", "kid": k} for k in kids]}


class Fetcher:
    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        result = self.results[min(self.calls, len(self.results)) - 1]
        if isinstance(result, Exception):
            raise result
        return result


def _cache(fetch, clock, ttl=600, min_refresh=30) -> JWKSCache:
    return JWKSCache(fetch, ttl=ttl, min_refresh_interval=min_refresh, clock=clock)


@pytest.mark.asyncio
class TestJWKSCache:
    async def test_fetches_once_then_serves_from_cache(self):
        fetch, clock = Fetcher(_set("a")), Clock()
        cache = _cache(fetch, clock)
        assert await cache.get("a") == _set("a")
        clock.now += 100
        await cache.get("a")
        assert fetch.calls == 1

    async def test_refreshes_after_the_ttl(self):
        fetch, clock = Fetcher(_set("a"), _set("b")), Clock()
        cache = _cache(fetch, clock)
        await cache.get()
        clock.now += 601
        assert await cache.get() == _set("b")

    async def test_an_unknown_kid_triggers_a_refresh(self):
        """This is what key rotation looks like from the API's side."""
        fetch, clock = Fetcher(_set("old"), _set("old", "new")), Clock()
        cache = _cache(fetch, clock)
        await cache.get("old")
        clock.now += 31
        assert await cache.get("new") == _set("old", "new")
        assert fetch.calls == 2

    async def test_random_kids_cannot_force_a_fetch_per_request(self):
        fetch, clock = Fetcher(_set("a")), Clock()
        cache = _cache(fetch, clock)
        await cache.get("a")
        clock.now += 31
        for i in range(100):
            await cache.get(f"attacker-{i}")
        assert fetch.calls == 2  # one refresh in the window, not one hundred

    async def test_a_failed_refresh_keeps_the_cached_keys(self):
        fetch, clock = Fetcher(_set("a"), RuntimeError("idp down")), Clock()
        cache = _cache(fetch, clock)
        await cache.get()
        clock.now += 601
        assert await cache.get() == _set("a")

    async def test_with_nothing_cached_a_fetch_error_propagates(self):
        cache = _cache(Fetcher(RuntimeError("idp down")), Clock())
        with pytest.raises(RuntimeError):
            await cache.get()

    async def test_concurrent_requests_share_one_fetch(self):
        class Slow(Fetcher):
            async def __call__(self):
                await asyncio.sleep(0.01)
                return await super().__call__()

        fetch = Slow(_set("a"))
        cache = _cache(fetch, Clock())
        await asyncio.gather(*(cache.get("a") for _ in range(20)))
        assert fetch.calls == 1


def test_unverified_kid_reads_the_header_only():
    assert unverified_kid(jwt.encode({}, "k" * 32, algorithm="HS256", headers={"kid": "abc"})) == "abc"
    assert unverified_kid("not-a-jwt") is None


# -- end to end through the backends ------------------------------------------------------
def _rsa():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key, kid: str) -> dict:
    return {**json.loads(RSAAlgorithm.to_jwk(key.public_key())), "kid": kid, "use": "sig", "alg": "RS256"}


def _token(key, kid: str) -> str:
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    now = int(time.time())
    return jwt.encode({"sub": "u", "iat": now, "exp": now + 300}, pem, algorithm="RS256", headers={"kid": kid})


def _keycloak() -> KeycloakOIDCBackend:
    return KeycloakOIDCBackend(keycloak_url="http://kc.test", realm="truenorth")


def _generic() -> GenericOIDCBackend:
    return GenericOIDCBackend(jwks_url="https://idp.test/jwks", algorithms=["RS256"])


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [_keycloak, _generic], ids=["keycloak", "generic"])
async def test_logins_survive_an_idp_key_rotation(respx_mock, make):
    old, new = _rsa(), _rsa()
    b = make()
    clock = Clock()
    b._jwks_cache._clock = clock
    route = respx_mock.get(b._jwks_url).mock(
        side_effect=[
            httpx.Response(200, json={"keys": [_jwk(old, "old")]}),
            httpx.Response(200, json={"keys": [_jwk(old, "old"), _jwk(new, "new")]}),
        ]
    )
    assert (await b.validate_token(_token(old, "old")))["sub"] == "u"

    clock.now += 60  # the IdP rotates well inside the TTL; the API has been up all along
    assert (await b.validate_token(_token(new, "new")))["sub"] == "u"
    assert (await b.validate_token(_token(old, "old")))["sub"] == "u"
    assert route.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [_keycloak, _generic], ids=["keycloak", "generic"])
async def test_a_forged_kid_still_cannot_get_in(respx_mock, make):
    """Refreshing on an unknown kid must not weaken verification."""
    real, forger = _rsa(), _rsa()
    b = make()
    respx_mock.get(b._jwks_url).mock(return_value=httpx.Response(200, json={"keys": [_jwk(real, "real")]}))
    with pytest.raises(HTTPException) as exc:
        await b.validate_token(_token(forger, "made-up"))
    assert exc.value.status_code == 401
