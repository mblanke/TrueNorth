"""Dev-only: TrueNorth on one local origin for the live-class demo, with a sign-in page.

Never for a deployment. Built on the browser-journeys machinery
(tests/integration/_journey_server.py, _journey_idp.py): the production SPA build at ``/``,
the API under ``/api`` with real bearer-token checks (``AUTH_BACKEND=generic_oidc``), and,
in place of Keycloak, a minimal OIDC provider under ``/auth/realms/truenorth`` that the SPA's
keycloak-js talks to unchanged. Unlike the journeys (which answer Keycloak's calls inside
Playwright), this one serves a real username/password page, so a person can sign in from any
browser. Accounts come from ``.demo/accounts.json`` (written by seed_live_class_demo.py,
passwords PBKDF2-hashed); tokens are signed with ``.demo/idp-key.pem``. Staff tokens carry
``amr: [pwd, otp]`` as the journeys' do: there is no second factor in this demo.

Environment: DEMO_DIR, DEMO_SPA (the build's ``browser`` directory), DEMO_ISSUER, plus the
API's own settings, set by scripts/dev/live_class_demo.sh. Listen on loopback only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.staticfiles import StaticFiles


def _sqlite_datetimes_aware() -> None:
    """SQLite keeps no offset, so ``DateTime(timezone=True)`` columns come back naive and the
    SPA reads those as the browser's local time (an inject at T+0:35 shows at T+4:35 in EDT).
    PostgreSQL, in a deployment, returns them aware in UTC; this makes the demo's SQLite do the same."""
    from datetime import UTC

    from sqlalchemy.dialects.sqlite import DATETIME

    original = DATETIME.result_processor

    def result_processor(self, dialect, coltype):
        inner = original(self, dialect, coltype)
        if not getattr(self, "timezone", False):
            return inner

        def process(value):
            value = inner(value) if inner else value
            return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value

        return process

    DATETIME.result_processor = result_processor


_sqlite_datetimes_aware()

from app.main import app as api  # noqa: E402

DEMO = Path(os.environ["DEMO_DIR"])
SPA = Path(os.environ["DEMO_SPA"])
ISSUER = os.environ["DEMO_ISSUER"]  # http://localhost:<port>/auth/realms/truenorth
REALM_PATH = urlsplit(ISSUER).path  # /auth/realms/truenorth
OIDC = f"{REALM_PATH}/protocol/openid-connect/"
CLIENT = "truenorth-web"
COOKIE = "TN_DEMO_SSO"
TTL = 3600


def _key():
    path = DEMO / "idp-key.pem"
    if not path.is_file():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


KEY = _key()
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
JWKS = {"keys": [{**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key())),
                  "kid": "demo-idp", "alg": "RS256", "use": "sig"}]}

SESSIONS: dict[str, dict] = {}  # sso cookie -> account
CODES: dict[str, dict] = {}  # one-time code -> {account, nonce, challenge, redirect_uri, sid}


def _accounts() -> dict:
    return json.loads((DEMO / "accounts.json").read_text())


def _check(login: str, password: str) -> dict | None:
    login = login.strip().lower()
    for a in _accounts().values():
        if login in (a["username"].lower(), a["email"].lower()):
            got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(a["salt"]), 200_000).hex()
            return a if hmac.compare_digest(got, a["pbkdf2_sha256"]) else None
    return None


def _same_origin(url: str, request: Request) -> bool:
    u = urlsplit(url)
    return u.scheme in ("http", "https") and u.netloc == request.headers.get("host", "")


def _sign(claims: dict) -> str:
    return jwt.encode(claims, PEM, algorithm="RS256", headers={"kid": "demo-idp"})


def _tokens(account: dict, sid: str, nonce: str | None) -> dict:
    now = int(time.time())
    base = {"iss": ISSUER, "sub": account["keycloak_id"], "iat": now, "auth_time": now, "sid": sid,
            "session_state": sid, "azp": CLIENT, "email": account["email"], "name": account["display_name"],
            "preferred_username": account["username"]}
    if nonce:
        base["nonce"] = nonce
    access = {**base, "aud": CLIENT, "typ": "Bearer", "exp": now + TTL, "amr": ["pwd", "otp"], "scope": "openid"}
    refresh = {**base, "aud": ISSUER, "typ": "Refresh", "exp": now + 10 * TTL}
    ident = {**base, "aud": CLIENT, "typ": "ID", "exp": now + TTL}
    return {"access_token": _sign(access), "refresh_token": _sign(refresh), "id_token": _sign(ident),
            "token_type": "Bearer", "expires_in": TTL, "refresh_expires_in": 10 * TTL, "session_state": sid,
            "scope": "openid"}


def _back(redirect_uri: str, mode: str, params: dict) -> RedirectResponse:
    sep = "#" if mode == "fragment" else ("&" if "?" in redirect_uri else "?")
    return RedirectResponse(redirect_uri + sep + urlencode(params), status_code=302)


def _issue_code(q: dict, account: dict, sid: str) -> RedirectResponse:
    code = secrets.token_urlsafe(24)
    CODES[code] = {"account": account, "nonce": q.get("nonce"), "challenge": q.get("code_challenge"),
                   "redirect_uri": q.get("redirect_uri", ""), "sid": sid, "at": time.time()}
    return _back(q["redirect_uri"], q.get("response_mode", "fragment"),
                 {"state": q.get("state", ""), "session_state": sid, "code": code})


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in - TrueNorth (demo)</title>
<style>
body{{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;background:#0f1b2d;color:#1b2430;
display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px;box-sizing:border-box}}
form{{background:#fff;border-radius:10px;padding:28px 28px 22px;width:100%;max-width:360px;box-shadow:0 10px 40px #0006}}
h1{{font-size:20px;margin:0 0 4px}} p.sub{{margin:0 0 18px;color:#5b6675;font-size:13px}}
label{{display:block;font-size:13px;font-weight:600;margin:12px 0 4px}}
input{{width:100%;box-sizing:border-box;padding:9px 10px;border:1px solid #c5ccd6;border-radius:6px;font-size:15px}}
button{{margin-top:18px;width:100%;padding:10px;border:0;border-radius:6px;background:#1e5aa8;color:#fff;
font-size:15px;font-weight:600;cursor:pointer}} .err{{color:#b3261e;font-size:13px;margin:10px 0 0}}
.note{{margin-top:14px;font-size:12px;color:#7a8594}}</style></head><body>
<form method="post" action="{action}">
<h1>TrueNorth Range</h1><p class="sub">Local live-class demo. Not a deployment.</p>
{hidden}
<label for="u">Username or email</label><input id="u" name="username" autocomplete="username" value="{user}" autofocus required>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
{error}<button type="submit">Sign in</button>
<p class="note">Accounts: .demo/credentials.txt in the demo worktree.</p></form></body></html>"""

FORWARD = ("client_id", "redirect_uri", "state", "nonce", "response_mode", "response_type", "scope",
           "code_challenge", "code_challenge_method")


def _login_page(q: dict, user: str = "", error: str = "") -> HTMLResponse:
    hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(q[k])}">' for k in FORWARD if k in q)
    body = PAGE.format(action=html.escape(OIDC + "auth"), hidden=hidden, user=html.escape(user),
                       error=f'<p class="err" role="alert">{html.escape(error)}</p>' if error else "")
    return HTMLResponse(body, status_code=401 if error else 200,
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "SAMEORIGIN"})


async def _form(request: Request) -> dict:
    return {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}


async def oidc(request: Request) -> Response:
    endpoint = request.url.path[len(OIDC):]
    q = dict(request.query_params)

    if endpoint.startswith("3p-cookies/"):
        return HTMLResponse('<!doctype html><script>parent.postMessage("supported", "*");</script>')
    if endpoint == "certs":
        return JSONResponse(JWKS)

    if endpoint == "auth":
        if request.method == "POST":
            q = await _form(request)
        if q.get("client_id") != CLIENT or not _same_origin(q.get("redirect_uri", ""), request):
            return HTMLResponse("Invalid client or redirect_uri", status_code=400)
        sid = request.cookies.get(COOKIE, "")
        if request.method == "GET":
            if sid in SESSIONS:
                return _issue_code(q, SESSIONS[sid], sid)
            if q.get("prompt") == "none":
                return _back(q["redirect_uri"], q.get("response_mode", "fragment"),
                             {"error": "login_required", "state": q.get("state", "")})
            return _login_page(q)
        account = _check(q.get("username", ""), q.get("password", ""))
        if account is None:
            return _login_page(q, q.get("username", ""), "Invalid username or password.")
        sid = secrets.token_urlsafe(18)
        SESSIONS[sid] = account
        resp = _issue_code(q, account, sid)
        resp.set_cookie(COOKIE, sid, path="/auth", httponly=True, samesite="lax")
        return resp

    if endpoint == "token" and request.method == "POST":
        f = await _form(request)
        if f.get("grant_type") == "authorization_code":
            c = CODES.pop(f.get("code", ""), None)
            if c is None or time.time() - c["at"] > 120 or f.get("redirect_uri", c["redirect_uri"]) != c["redirect_uri"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if c["challenge"]:
                digest = hashlib.sha256(f.get("code_verifier", "").encode()).digest()
                if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != c["challenge"]:
                    return JSONResponse({"error": "invalid_grant", "error_description": "PKCE"}, status_code=400)
            return JSONResponse(_tokens(c["account"], c["sid"], c["nonce"]), headers={"Cache-Control": "no-store"})
        if f.get("grant_type") == "refresh_token":
            try:
                claims = jwt.decode(f.get("refresh_token", ""), KEY.public_key(), algorithms=["RS256"], audience=ISSUER)
            except jwt.PyJWTError:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            account = SESSIONS.get(claims.get("sid", ""))
            if account is None or account["keycloak_id"] != claims["sub"]:
                return JSONResponse({"error": "invalid_grant", "error_description": "session ended"}, status_code=400)
            return JSONResponse(_tokens(account, claims["sid"], claims.get("nonce")),
                                headers={"Cache-Control": "no-store"})
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    if endpoint == "logout":
        SESSIONS.pop(request.cookies.get(COOKIE, ""), None)
        target = q.get("post_logout_redirect_uri") or q.get("redirect_uri") or "/"
        resp = RedirectResponse(target if target == "/" or _same_origin(target, request) else "/", status_code=302)
        resp.delete_cookie(COOKIE, path="/auth")
        return resp

    if endpoint == "userinfo":
        return JSONResponse({"error": "not_supported"}, status_code=400)
    return JSONResponse({"error": "invalid_request"}, status_code=400)


class SpaFiles(StaticFiles):
    """The SPA's files; any other path is a client-side route, so it gets ``index.html``."""

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            return FileResponse(SPA / "index.html")


spa = SpaFiles(directory=SPA)


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        return await api(scope, receive, send)
    path = scope.get("path", "")
    if path == "/api" or path.startswith("/api/"):
        inner = dict(scope)
        inner["path"] = path[4:] or "/"
        raw = scope.get("raw_path") or path.encode()
        inner["raw_path"] = raw[4:] or b"/"
        return await api(inner, receive, send)
    if path.startswith(OIDC) and scope["type"] == "http":
        resp = await oidc(Request(scope, receive))
        return await resp(scope, receive, send)
    if path == "/assets/config.json":
        return await JSONResponse({})(scope, receive, send)
    return await spa(scope, receive, send)
