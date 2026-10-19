"""A test identity provider for browser journeys against a real, auth-enforcing API.

Used by tests/integration/test_browser_journeys.py (and shaped as the Idp in
test_moodle_student_journey.py): an RSA key whose public half the API is told to trust
(``AUTH_BACKEND=generic_oidc``, ``OIDC_JWKS_URL`` -> ``/test-idp/jwks`` on
_journey_server.py), and a Playwright browser context in which Keycloak's three browser
calls (the cookie probe, the silent check-sso iframe, the code exchange) are answered with
tokens signed by that key. The API verifies signature, issuer, audience and expiry as it
would Keycloak's, then resolves the ``sub`` to a ``users`` row: the role, tenant and
permissions are the database's, never the browser's.

Test-only, never shipped.
"""

from __future__ import annotations

import json
import pathlib
import re
import time
from urllib.parse import parse_qs, urlsplit

CLIENT = "truenorth-web"
OIDC = re.compile(r"/realms/[^/]+/protocol/openid-connect/")


class Idp:
    def __init__(self, tmp: pathlib.Path, issuer: str):
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.issuer = issuer
        self.pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        self.jwks_file = tmp / "jwks.json"
        self.jwks_file.write_text(json.dumps({"keys": [{**jwk, "kid": "journey-idp", "alg": "RS256", "use": "sig"}]}))

    def token(self, sub: str, typ: str = "Bearer", email: str = "") -> str:
        import jwt

        now = int(time.time())
        # amr: a password and a one-time code, as Keycloak's tn-browser flow reports a staff
        # sign-in (app/mfa_policy.py). Students are not asked; it is harmless there.
        claims = {"iss": self.issuer, "aud": CLIENT, "azp": CLIENT, "sub": sub, "typ": typ, "iat": now,
                  "exp": now + 3600, "preferred_username": sub, "email": email, "amr": ["pwd", "otp"]}
        return jwt.encode(claims, self.pem, algorithm="RS256", headers={"kid": "journey-idp"})


def signed_in_context(browser, idp: Idp, person, base_url: str):
    """A browser profile in which Keycloak says ``person`` (a users row) is signed in."""
    ctx = browser.new_context(base_url=base_url)

    def answer(route):
        url = urlsplit(route.request.url)
        endpoint = url.path.split("/protocol/openid-connect/", 1)[1]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        html = "<!doctype html><html><body>{}</body></html>"
        if endpoint.startswith("3p-cookies/"):
            return route.fulfill(status=200, content_type="text/html",
                                 body=html.format('<script>parent.postMessage("supported", "*");</script>'))
        if endpoint == "auth":  # the silent check-sso iframe: back with a code, as Keycloak would
            target = json.dumps(f"{q.get('redirect_uri', '')}#state={q.get('state', '')}&session_state=j&code=j-code")
            return route.fulfill(status=200, content_type="text/html",
                                 body=html.format(f"<script>location.replace({target});</script>"))
        if endpoint == "token":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "access_token": idp.token(person.keycloak_id, email=person.email),
                "refresh_token": idp.token(person.keycloak_id, "Refresh"),
                "token_type": "Bearer", "expires_in": 3600, "refresh_expires_in": 3600, "session_state": "j"}))
        return route.fulfill(status=400, content_type="application/json", body='{"error":"invalid_request"}')

    ctx.route(OIDC, answer)
    return ctx
