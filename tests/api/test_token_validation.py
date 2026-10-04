"""Real-signature tests for every place the API trusts a JWT.

The backend tests in test_auth_backends.py stub `jwt.decode`, so until now nothing
proved that a forged, expired, mis-addressed or wrongly-signed token is refused. These
tests sign tokens with `cryptography` directly, not with any JWT library, so they hold
the validator to the same expectations whichever library implements it.

Covered: the Keycloak and generic OIDC auth backends (every authenticated request) and
the LTI 1.3 launch (platform-signed id_token). For each: the signature and key id,
the algorithm allow-list (including `none` and HS256-with-the-public-key
confusion), the time claims, the audience, and the issuer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import respx
from app import lti13
from app.auth_backends import GenericOIDCBackend, KeycloakOIDCBackend
from app.models import ExternalPlatform, IntegrationAuthType, LTINonce
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi import HTTPException
from httpx import Response

# ── A JWT signer that shares no code with the validator ──────────────────


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _uint(value: int) -> str:
    return _b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def sign(claims: dict, key, *, alg: str = "RS256", kid: str | None = "k1") -> str:
    header = {"alg": alg, "typ": "JWT"}
    if kid is not None:
        header["kid"] = kid
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}".encode()
    if alg == "RS256":
        sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    elif alg == "ES256":
        r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    elif alg == "HS256":
        sig = hmac.new(key, signing_input, hashlib.sha256).digest()
    elif alg == "none":
        sig = b""
    else:
        raise ValueError(alg)
    return f"{signing_input.decode()}.{_b64(sig)}"


def rsa_jwk(key, kid: str = "k1") -> dict:
    numbers = key.public_key().public_numbers()
    return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid, "n": _uint(numbers.n), "e": _uint(numbers.e)}


def ec_jwk(key, kid: str = "e1") -> dict:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "EC",
        "use": "sig",
        "alg": "ES256",
        "crv": "P-256",
        "kid": kid,
        "x": _b64(numbers.x.to_bytes(32, "big")),
        "y": _b64(numbers.y.to_bytes(32, "big")),
    }


def public_pem(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def other_rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def _claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "sub": "user-123",
        "iss": "https://idp.test/realms/truenorth",
        "aud": "truenorth-api",
        "iat": now,
        "exp": now + 300,
        "preferred_username": "tester",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


# python-jose (3.3.0) gaps these tests found; the PyJWT backend fixes them.
JOSE_MIXED_JWKS = pytest.mark.xfail(
    strict=True,
    reason="python-jose tries every JWKS key; a key of another type raises JWKError, "
    "which escapes `except JWTError` as a 500",
)
JOSE_IGNORES_KID = pytest.mark.xfail(strict=True, reason="python-jose ignores kid and tries every key")
JOSE_MISSING_AUD = pytest.mark.xfail(
    strict=True, reason="python-jose skips the audience check when the token has no aud claim"
)


async def _refused(backend, token: str) -> None:
    with pytest.raises(HTTPException) as exc:
        await backend.validate_token(token)
    assert exc.value.status_code == 401


# ── Keycloak OIDC backend ────────────────────────────────────────────────

KC_URL = "http://kc.test"
KC_JWKS = f"{KC_URL}/realms/truenorth/protocol/openid-connect/certs"


@pytest.fixture
def keycloak(rsa_key, ec_key):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(KC_JWKS).mock(return_value=Response(200, json={"keys": [rsa_jwk(rsa_key), ec_jwk(ec_key)]}))
        yield KeycloakOIDCBackend(keycloak_url=KC_URL, realm="truenorth")


@pytest.mark.asyncio
class TestKeycloakBackend:
    async def test_a_valid_token_is_accepted(self, keycloak, rsa_key):
        payload = await keycloak.validate_token(sign(_claims(), rsa_key))
        assert payload["sub"] == "user-123"
        assert payload["preferred_username"] == "tester"

    @JOSE_MIXED_JWKS
    async def test_a_token_signed_by_another_key_is_refused(self, keycloak, other_rsa_key):
        await _refused(keycloak, sign(_claims(), other_rsa_key))

    @JOSE_MIXED_JWKS
    async def test_a_tampered_payload_is_refused(self, keycloak, rsa_key):
        header, _, sig = sign(_claims(), rsa_key).split(".")
        forged = _b64(json.dumps(_claims(sub="admin")).encode())
        await _refused(keycloak, f"{header}.{forged}.{sig}")

    @JOSE_IGNORES_KID
    async def test_an_unknown_key_id_is_refused(self, keycloak, rsa_key):
        await _refused(keycloak, sign(_claims(), rsa_key, kid="rotated-away"))

    async def test_an_expired_token_is_refused(self, keycloak, rsa_key):
        await _refused(keycloak, sign(_claims(exp=int(time.time()) - 60), rsa_key))

    async def test_a_not_yet_valid_token_is_refused(self, keycloak, rsa_key):
        await _refused(keycloak, sign(_claims(nbf=int(time.time()) + 3600), rsa_key))

    async def test_alg_none_is_refused(self, keycloak):
        await _refused(keycloak, sign(_claims(), None, alg="none"))

    async def test_hs256_signed_with_the_public_key_is_refused(self, keycloak, rsa_key):
        """Algorithm confusion: the JWKS public key used as an HMAC secret."""
        await _refused(keycloak, sign(_claims(), public_pem(rsa_key), alg="HS256"))

    async def test_an_algorithm_outside_the_allow_list_is_refused(self, keycloak, ec_key):
        """Keycloak tokens are RS256; an ES256 token is refused even with a published EC key."""
        await _refused(keycloak, sign(_claims(), ec_key, alg="ES256", kid="e1"))

    async def test_garbage_is_refused(self, keycloak):
        await _refused(keycloak, "not.a.jwt")

    async def test_audience_is_not_checked(self, keycloak, rsa_key):
        """Pins current behaviour: Keycloak tokens are accepted for any audience.

        A token issued to any client in the realm is accepted. Tightening this needs a
        configured audience (KEYCLOAK_URL is the internal URL, not the token's issuer,
        so the issuer can't be derived either). Change this test deliberately.
        """
        payload = await keycloak.validate_token(sign(_claims(aud="some-other-client"), rsa_key))
        assert payload["aud"] == "some-other-client"


# ── Generic OIDC backend ─────────────────────────────────────────────────

OIDC_JWKS = "https://idp.test/keys"
ISSUER = "https://idp.test/realms/truenorth"


@pytest.fixture
def oidc_jwks(rsa_key, other_rsa_key, ec_key):
    keys = [rsa_jwk(other_rsa_key, kid="k0"), rsa_jwk(rsa_key, kid="k1"), ec_jwk(ec_key)]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(OIDC_JWKS).mock(return_value=Response(200, json={"keys": keys}))
        yield


@pytest.fixture
def oidc(oidc_jwks):
    return GenericOIDCBackend(jwks_url=OIDC_JWKS, audience="truenorth-api", issuer=ISSUER)


@pytest.mark.asyncio
class TestGenericOIDCBackend:
    async def test_a_valid_token_is_accepted(self, oidc, rsa_key):
        payload = await oidc.validate_token(sign(_claims(), rsa_key))
        assert payload["sub"] == "user-123"

    async def test_the_key_is_chosen_by_kid(self, oidc, other_rsa_key):
        assert (await oidc.validate_token(sign(_claims(), other_rsa_key, kid="k0")))["sub"] == "user-123"

    @JOSE_MIXED_JWKS
    async def test_a_kid_pointing_at_the_wrong_key_is_refused(self, oidc, other_rsa_key):
        await _refused(oidc, sign(_claims(), other_rsa_key, kid="k1"))

    async def test_a_token_for_another_audience_is_refused(self, oidc, rsa_key):
        await _refused(oidc, sign(_claims(aud="another-api"), rsa_key))

    async def test_an_audience_list_containing_ours_is_accepted(self, oidc, rsa_key):
        payload = await oidc.validate_token(sign(_claims(aud=["another-api", "truenorth-api"]), rsa_key))
        assert payload["sub"] == "user-123"

    @JOSE_MISSING_AUD
    async def test_a_token_with_no_audience_is_refused_when_one_is_configured(self, oidc, rsa_key):
        await _refused(oidc, sign(_claims(aud=None), rsa_key))

    async def test_a_token_from_another_issuer_is_refused(self, oidc, rsa_key):
        await _refused(oidc, sign(_claims(iss="https://evil.test/"), rsa_key))

    async def test_a_token_with_no_issuer_is_refused_when_one_is_configured(self, oidc, rsa_key):
        await _refused(oidc, sign(_claims(iss=None), rsa_key))

    async def test_an_expired_token_is_refused(self, oidc, rsa_key):
        await _refused(oidc, sign(_claims(exp=int(time.time()) - 60), rsa_key))

    async def test_alg_none_is_refused(self, oidc):
        await _refused(oidc, sign(_claims(), None, alg="none"))

    async def test_hs256_signed_with_the_public_key_is_refused(self, oidc, rsa_key):
        await _refused(oidc, sign(_claims(), public_pem(rsa_key), alg="HS256"))

    async def test_an_algorithm_outside_the_allow_list_is_refused(self, oidc, ec_key):
        await _refused(oidc, sign(_claims(), ec_key, alg="ES256", kid="e1"))

    @JOSE_MIXED_JWKS
    async def test_es256_is_accepted_when_allowed(self, oidc_jwks, ec_key):
        backend = GenericOIDCBackend(jwks_url=OIDC_JWKS, audience="truenorth-api", algorithms=["ES256"])
        payload = await backend.validate_token(sign(_claims(), ec_key, alg="ES256", kid="e1"))
        assert payload["sub"] == "user-123"

    async def test_rs256_is_refused_when_only_es256_is_allowed(self, oidc_jwks, rsa_key):
        backend = GenericOIDCBackend(jwks_url=OIDC_JWKS, audience="truenorth-api", algorithms=["ES256"])
        await _refused(backend, sign(_claims(), rsa_key))

    async def test_with_no_audience_configured_any_audience_is_accepted(self, oidc_jwks, rsa_key, monkeypatch):
        monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
        monkeypatch.delenv("OIDC_ISSUER", raising=False)
        backend = GenericOIDCBackend(jwks_url=OIDC_JWKS)
        payload = await backend.validate_token(sign(_claims(aud="whatever", iss="https://anyone.test/"), rsa_key))
        assert payload["sub"] == "user-123"


# ── LTI 1.3 launch id_token ──────────────────────────────────────────────

LTI_ISSUER = "http://moodle.test"
LTI_JWKS = "http://moodle.test/mod/lti/certs.php"
TENANT = uuid.UUID("00000000-0000-0000-0000-00000000000a")


def _lti_platform(db) -> ExternalPlatform:
    p = ExternalPlatform(
        id=uuid.uuid4(),
        name="Moodle",
        slug=f"moodle-{uuid.uuid4().hex[:6]}",
        platform_type="moodle",
        base_url=LTI_ISSUER,
        auth_type=IntegrationAuthType.lti13,
        tenant_id=TENANT,
        lti_issuer=LTI_ISSUER,
        lti_client_id="client-1",
        lti_deployment_id="dep-1",
        lti_jwks_url=LTI_JWKS,
    )
    db.add(p)
    db.flush()
    return p


def _lti_state(db, platform) -> tuple[str, str]:
    nonce, state = uuid.uuid4().hex, uuid.uuid4().hex
    db.add(
        LTINonce(
            nonce=nonce, state=state, platform_id=platform.id, expires_at=datetime.now(UTC) + timedelta(minutes=10)
        )
    )
    db.flush()
    return nonce, state


def _lti_claims(nonce: str, **overrides) -> dict:
    lti = {
        "iss": LTI_ISSUER,
        "aud": "client-1",
        "sub": "moodle-user-7",
        "nonce": nonce,
        lti13.CLAIM_DEPLOYMENT: "dep-1",
        lti13.CLAIM_MESSAGE_TYPE: "LtiResourceLinkRequest",
    }
    return _claims(**{**lti, **overrides})


@pytest.fixture
def lti_platform_key(rsa_key):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(LTI_JWKS).mock(return_value=Response(200, json={"keys": [rsa_jwk(rsa_key)]}))
        yield rsa_key


@pytest.mark.asyncio
class TestLTILaunchToken:
    async def test_a_platform_signed_launch_is_accepted(self, db_session, lti_platform_key):
        platform = _lti_platform(db_session)
        nonce, state = _lti_state(db_session, platform)
        found, claims = await lti13.validate_launch(db_session, sign(_lti_claims(nonce), lti_platform_key), state)
        assert found.id == platform.id
        assert claims["sub"] == "moodle-user-7"

    @pytest.mark.parametrize(
        "case",
        ["other_key", "expired", "wrong_audience", "alg_none", "hs256_public_key"],
    )
    async def test_a_bad_launch_token_is_refused(self, db_session, lti_platform_key, other_rsa_key, case):
        platform = _lti_platform(db_session)
        nonce, state = _lti_state(db_session, platform)
        token = {
            "other_key": lambda: sign(_lti_claims(nonce), other_rsa_key),
            "expired": lambda: sign(_lti_claims(nonce, exp=int(time.time()) - 60), lti_platform_key),
            "wrong_audience": lambda: sign(_lti_claims(nonce, aud="client-2"), lti_platform_key),
            "alg_none": lambda: sign(_lti_claims(nonce), None, alg="none"),
            "hs256_public_key": lambda: sign(_lti_claims(nonce), public_pem(lti_platform_key), alg="HS256"),
        }[case]()
        with pytest.raises(Exception):  # noqa: B017 - library-specific error types; the router maps all to 401
            await lti13.validate_launch(db_session, token, state)
        # The nonce is consumed only after the signature checks, so a refused token
        # must not burn it.
        assert db_session.query(LTINonce).filter(LTINonce.state == state).count() == 1
