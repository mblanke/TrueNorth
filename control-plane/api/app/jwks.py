"""TrueNorth Range — verify a JWT against a published JWKS (RFC 7517) with PyJWT.

One place for the rules every third-party token must meet, whoever issued it
(Keycloak, a generic OIDC provider, an LTI 1.3 platform):

- the header ``alg`` is on the caller's allow-list, and only asymmetric algorithms
  are ever allowed. ``none`` and HS* are refused outright, so a JWKS public key can
  never be used as an HMAC secret.
- the key is chosen by ``kid``. With no ``kid``, only if exactly one published
  signing key fits the algorithm. A key that declares its own ``alg`` must match.
- ``exp`` is required. ``exp``/``nbf``/``iat`` allow ``CLOCK_SKEW_SECONDS`` of drift
  between the issuer's clock and ours.

Every failure raises a subclass of ``jwt.PyJWTError``, so callers catch one type.
"""

from __future__ import annotations

from typing import Any

import jwt
from jwt import PyJWK

CLOCK_SKEW_SECONDS = 30

# Algorithm -> the JWK key type that can verify it.
ASYMMETRIC_ALGORITHMS: dict[str, str] = {
    "RS256": "RSA",
    "RS384": "RSA",
    "RS512": "RSA",
    "PS256": "RSA",
    "PS384": "RSA",
    "PS512": "RSA",
    "ES256": "EC",
    "ES384": "EC",
    "ES512": "EC",
    "EdDSA": "OKP",
}


def check_algorithms(algorithms: list[str]) -> list[str]:
    """Validate a configured allow-list. Raises ValueError on a symmetric or unknown alg."""
    bad = [a for a in algorithms if a not in ASYMMETRIC_ALGORITHMS]
    if bad or not algorithms:
        raise ValueError(
            f"Unsupported JWT algorithm(s) {bad or algorithms}: tokens verified against a JWKS "
            f"must use one of {sorted(ASYMMETRIC_ALGORITHMS)}"
        )
    return algorithms


def decode(
    token: str,
    jwks: dict,
    *,
    algorithms: list[str],
    audience: str | None = None,
    issuer: str | None = None,
    verify_aud: bool = True,
) -> dict[str, Any]:
    """Verify ``token`` against ``jwks`` and return its claims."""
    header = jwt.get_unverified_header(token)
    alg = header.get("alg")
    if alg not in algorithms or alg not in ASYMMETRIC_ALGORITHMS:
        raise jwt.InvalidAlgorithmError(f"Algorithm {alg!r} is not allowed")
    key = _select_key(jwks, header.get("kid"), alg)
    return jwt.decode(
        token,
        key,
        algorithms=[alg],
        audience=audience if verify_aud else None,
        issuer=issuer,
        leeway=CLOCK_SKEW_SECONDS,
        options={"verify_aud": verify_aud, "require": ["exp"]},
    )


def _select_key(jwks: dict, kid: str | None, alg: str) -> PyJWK:
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise jwt.PyJWKSetError("Invalid JWK Set")
    signing = [k for k in jwks["keys"] if isinstance(k, dict) and k.get("use", "sig") == "sig"]
    if kid is not None:
        candidates = [k for k in signing if k.get("kid") == kid]
        if not candidates:
            raise jwt.InvalidTokenError(f"No published signing key with kid {kid!r}")
    else:
        candidates = [k for k in signing if k.get("kty") == ASYMMETRIC_ALGORITHMS[alg]]
        if len(candidates) != 1:
            raise jwt.InvalidTokenError("Token has no kid and the key set has no single matching key")
    jwk = candidates[0]
    if jwk.get("alg") not in (None, alg):
        raise jwt.InvalidAlgorithmError(f"Key {kid!r} is for {jwk['alg']}, token says {alg}")
    try:
        return PyJWK(jwk, algorithm=alg)
    except jwt.PyJWTError as exc:
        raise jwt.InvalidTokenError(f"Key {kid!r} cannot verify {alg}") from exc
