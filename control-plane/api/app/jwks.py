"""TrueNorth Range — verify a JWT against a published JWKS (RFC 7517) with PyJWT.

One place for the rules every third-party token must meet, whoever issued it
(Keycloak, a generic OIDC provider, an LTI 1.3 platform):

- the header ``alg`` is on the caller's allow-list, and only asymmetric algorithms
  are ever allowed. ``none`` and HS* are refused outright, so a JWKS public key can
  never be used as an HMAC secret.
- only keys that can verify the header ``alg`` are considered: a signing key (``use``
  absent or ``sig``) of the matching ``kty`` whose declared ``alg``, if any, is that alg.
  Among those the key is chosen by ``kid``, else by ``x5t`` (ADFS), else only if exactly
  one remains. If several share the ``kid`` (rollover), each is tried.
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


def unverified_kid(token: str) -> str | None:
    """The header kid, unverified: only for picking (or refreshing) keys. None if the
    header cannot be read; decode() rejects such a token with the real reason."""
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        return None
    return kid if isinstance(kid, str) else None


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
    last_error: jwt.PyJWTError | None = None
    for key in _candidate_keys(jwks, header, alg):
        try:
            return jwt.decode(
                token,
                key,
                algorithms=[alg],
                audience=audience if verify_aud else None,
                issuer=issuer,
                leeway=CLOCK_SKEW_SECONDS,
                options={"verify_aud": verify_aud, "require": ["exp"]},
            )
        except jwt.InvalidSignatureError as exc:
            last_error = exc
    raise last_error or jwt.InvalidTokenError("No usable key")


def _candidate_keys(jwks: dict, header: dict, alg: str) -> list[PyJWK]:
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise jwt.PyJWKSetError("Invalid JWK Set")
    usable = [
        k
        for k in jwks["keys"]
        if isinstance(k, dict)
        and k.get("use", "sig") == "sig"
        and k.get("kty") == ASYMMETRIC_ALGORITHMS[alg]
        and k.get("alg") in (None, alg)
    ]
    kid, x5t = header.get("kid"), header.get("x5t")
    if kid is not None:
        matched = [k for k in usable if k.get("kid") == kid]
        hint = f"kid {kid!r}"
    elif x5t is not None:
        matched = [k for k in usable if k.get("x5t") == x5t]
        hint = f"x5t {x5t!r}"
    else:
        matched = usable if len(usable) == 1 else []
        hint = "no kid (and not exactly one matching key)"
    keys: list[PyJWK] = []
    for jwk in matched:
        try:
            keys.append(PyJWK(jwk, algorithm=alg))
        except jwt.PyJWTError:
            continue
    if not keys:
        raise jwt.InvalidTokenError(f"No published {alg} signing key for {hint}")
    return keys
