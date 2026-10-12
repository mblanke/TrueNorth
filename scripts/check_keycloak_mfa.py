#!/usr/bin/env python3
"""Drive a real Keycloak's browser sign-in to prove the MFA configuration works.

Run by scripts/check_keycloak_mfa.sh against the Keycloak image compose.prod.yml pins,
after scripts/keycloak_mfa.py has configured the realm. Standard library only. Proves:

  (i)   a user in a privileged group with no second factor is sent to CONFIGURE_TOTP,
        not let through;
  (ii)  after enrolling TOTP (code computed here), the access token's amr has "otp",
        and so does a refreshed token (the AMR mapper on refresh);
  (iii) a Student signs in with a password only: no OTP page, amr == ["pwd"];
  (iv)  after the break-glass reset (otp credentials deleted, UPDATE_PASSWORD and
        CONFIGURE_TOTP required, sessions logged out) the next sign-in lands on
        CONFIGURE_TOTP again and must then set a new password;
  (v)   a privileged user holding tn-mfa-exempt (the test-host demo exemption) gets no
        OTP page, and without it gets one;
  (vi)  a step-up request (acr "mfa", essential) from a Student-group user is asked for a
        second factor, and its token then carries acr "mfa";
  (vii) the login page carries the banner and the acknowledgement checkbox, when the
        theme is mounted (--expect-banner).

The master realm admin password (user ``admin``) is read from stdin, never argv; a fresh
admin token is taken for each Admin API call (admin-cli tokens live about 60 s, and the
TOTP steps wait for the next 30 s window).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import http.cookiejar
import json
import re
import secrets
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CLIENT = "truenorth-web"
REDIRECT = "http://localhost:4200/"
PASSWORD = "Check-" + secrets.token_hex(8)

GROUPS = ["TN-Platform-Admins", "TN-Range-Operators", "TN-Instructors", "TN-Content-Authors",
          "TN-Students", "TN-Observers"]
# The same mapper as tn_kc_protocol_mappers in install/roles/tn_keycloak/defaults/main.yml.
AMR_MAPPER = {"name": "amr", "protocol": "openid-connect", "protocolMapper": "oidc-amr-mapper",
              "config": {"access.token.claim": "true", "id.token.claim": "true"}}
FAILURES: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        FAILURES.append(what)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class Browser:
    """A cookie jar and manual redirects, so the redirect to the app can be caught."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)

    def fetch(self, url: str, data: dict | None = None) -> tuple[int, str, str]:
        """(status, final url, body). Follows redirects inside Keycloak; stops at the app."""
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        for _ in range(10):
            req = urllib.request.Request(url, data=body)
            try:
                resp = self.opener.open(req, timeout=30)
                return resp.status, url, resp.read().decode()
            except urllib.error.HTTPError as exc:
                if exc.code in (301, 302, 303, 307):
                    url = urllib.parse.urljoin(url, exc.headers["Location"])
                    body = None
                    if url.startswith(REDIRECT):
                        return 302, url, ""
                    continue
                return exc.code, url, exc.read().decode()
        raise RuntimeError("too many redirects")


def form_action(page: str) -> str:
    m = re.search(r'<form[^>]*action="([^"]+)"', page)
    if not m:
        raise RuntimeError("no form on the page:\n" + page[:2000])
    return html.unescape(m.group(1))


# What keycloak-js sends for login({acr: {values: ['mfa'], essential: true}}).
STEP_UP = {"claims": json.dumps({"id_token": {"acr": {"essential": True, "values": ["mfa"]}}})}


def mfa(c: dict) -> bool:
    """The API's rule (app/mfa_policy.py defaults): amr otp/hwk/swk/mfa, or acr mfa."""
    return bool({"otp", "hwk", "swk", "mfa"} & set(c.get("amr") or [])) or c.get("acr") == "mfa"


def wait_next_window() -> None:
    """A TOTP code is accepted once: wait for the next 30 s window before another."""
    time.sleep(31 - (time.time() % 30))


def page_message(page: str) -> str:
    """The error or title Keycloak shows on a page, for failure messages."""
    for pat in (r'id="input-error[^"]*"[^>]*>\s*([^<]+)', r'kc-feedback-text">([^<]+)', r'<h1 id="kc-page-title">\s*([^<]+)'):
        m = re.search(pat, page)
        if m:
            return html.unescape(m.group(1).strip())
    return "(no message)"


def totp(secret: str, at: float | None = None) -> str:
    counter = int((at or time.time()) // 30)
    digest = hmac.new(secret.encode(), struct.pack(">Q", counter), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def claims(token: str) -> dict:
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


class Realm:
    def __init__(self, base: str, realm: str, admin_password: str) -> None:
        self.base, self.realm, self._password = base.rstrip("/"), realm, admin_password

    @property
    def token(self) -> str:
        data = urllib.parse.urlencode({"grant_type": "password", "client_id": "admin-cli", "username": "admin",
                                       "password": self._password}).encode()
        with urllib.request.urlopen(f"{self.base}/realms/master/protocol/openid-connect/token", data) as r:
            return json.load(r)["access_token"]

    def admin(self, method: str, path: str, body=None):
        req = urllib.request.Request(
            f"{self.base}/admin/realms/{self.realm}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None

    def group_id(self, name: str) -> str:
        for g in self.admin("GET", "/groups?max=1000"):
            if g["name"] == name:
                return g["id"]
        self.admin("POST", "/groups", {"name": name})
        return self.group_id(name)

    def identity_scope(self) -> None:
        """What install/roles/tn_keycloak does for the claim contract, reduced to the AMR
        mapper (tn_kc_protocol_mappers; tests/contracts/test_keycloak_mfa.py keeps the two
        alike): the truenorth-identity scope, a default scope of the web client."""
        scopes = {s["name"]: s for s in self.admin("GET", "/client-scopes")}
        if "truenorth-identity" not in scopes:
            self.admin("POST", "/client-scopes", {
                "name": "truenorth-identity", "protocol": "openid-connect",
                "attributes": {"include.in.token.scope": "true"},
                "protocolMappers": [AMR_MAPPER]})
            scopes = {s["name"]: s for s in self.admin("GET", "/client-scopes")}
        web = self.admin("GET", f"/clients?clientId={CLIENT}")[0]
        self.admin("PUT", f"/clients/{web['id']}/default-client-scopes/{scopes['truenorth-identity']['id']}")

    def user(self, username: str, group: str) -> str:
        self.admin("POST", "/users", {
            "username": username, "email": username, "emailVerified": True, "enabled": True,
            "firstName": "Check", "lastName": "User",
            "credentials": [{"type": "password", "value": PASSWORD, "temporary": False}]})
        uid = self.admin("GET", f"/users?username={urllib.parse.quote(username)}&exact=true")[0]["id"]
        self.admin("PUT", f"/users/{uid}/groups/{self.group_id(group)}")
        return uid

    def auth_url(self, verifier: str, extra: dict | None = None) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"client_id": CLIENT, "response_type": "code", "redirect_uri": REDIRECT, "scope": "openid",
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": secrets.token_hex(8),
                  "nonce": secrets.token_hex(8)}
        params.update(extra or {})
        return f"{self.base}/realms/{self.realm}/protocol/openid-connect/auth?" + urllib.parse.urlencode(params)

    def exchange(self, code: str, verifier: str) -> dict:
        data = urllib.parse.urlencode({"grant_type": "authorization_code", "client_id": CLIENT, "code": code,
                                       "redirect_uri": REDIRECT, "code_verifier": verifier}).encode()
        with urllib.request.urlopen(f"{self.base}/realms/{self.realm}/protocol/openid-connect/token", data) as r:
            return json.load(r)

    def refresh(self, refresh_token: str) -> dict:
        data = urllib.parse.urlencode({"grant_type": "refresh_token", "client_id": CLIENT,
                                       "refresh_token": refresh_token}).encode()
        with urllib.request.urlopen(f"{self.base}/realms/{self.realm}/protocol/openid-connect/token", data) as r:
            return json.load(r)


class SignIn:
    """One browser sign-in: login page, password, then whatever Keycloak asks next."""

    def __init__(self, realm: Realm, username: str, extra: dict | None = None, browser: Browser | None = None):
        self.realm, self.username = realm, username
        self.browser = browser or Browser()
        self.verifier = secrets.token_urlsafe(48)
        _, _, self.login_page = self.browser.fetch(realm.auth_url(self.verifier, extra))
        data = {"username": username, "password": PASSWORD, "credentialId": ""}
        if 'name="tn_ack"' in self.login_page:
            data["tn_ack"] = "on"
        self.status, self.url, self.page = self.browser.fetch(form_action(self.login_page), data)

    @property
    def at_configure_totp(self) -> bool:
        return 'id="totpSecret"' in self.page or "login-config-totp" in self.page or "kc-totp-settings" in self.page

    @property
    def at_otp_form(self) -> bool:
        return 'name="otp"' in self.page and not self.at_configure_totp

    def enrol_totp(self) -> str:
        secret = html.unescape(re.search(r'id="totpSecret"[^>]*value="([^"]+)"', self.page).group(1))
        self.status, self.url, self.page = self.browser.fetch(
            form_action(self.page), {"totp": totp(secret), "totpSecret": secret, "userLabel": "check", "mode": "manual"})
        return secret

    def answer_otp(self, secret: str) -> None:
        self.status, self.url, self.page = self.browser.fetch(form_action(self.page), {"otp": totp(secret)})

    def step_up(self) -> SignIn:
        """A new authorization request in this browser asking for acr "mfa" (the SPA's step-up)."""
        up = SignIn.__new__(SignIn)
        up.realm, up.username, up.browser = self.realm, self.username, self.browser
        up.verifier = secrets.token_urlsafe(48)
        up.status, up.url, up.page = self.browser.fetch(self.realm.auth_url(up.verifier, STEP_UP))
        up.login_page = up.page
        return up

    def tokens(self) -> dict:
        if not self.url.startswith(REDIRECT):
            raise RuntimeError(f"sign-in did not reach the app; stopped at {self.url}: {page_message(self.page)}")
        code = urllib.parse.parse_qs(urllib.parse.urlparse(self.url).query)["code"][0]
        return self.realm.exchange(code, self.verifier)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--realm", default="truenorth")
    ap.add_argument("--expect-banner", action="store_true")
    ap.add_argument("--create-groups", action="store_true",
                    help="only create the TN-* groups (as the installer does) and exit")
    args = ap.parse_args()
    realm = Realm(args.base, args.realm, sys.stdin.readline().strip())
    if args.create_groups:
        for name in GROUPS:
            realm.group_id(name)
        realm.identity_scope()
        print("groups: " + ", ".join(GROUPS) + "; truenorth-identity scope with the AMR mapper")
        return 0

    tag = secrets.token_hex(3)
    instructor = f"instructor-{tag}@check.test"
    student = f"student-{tag}@check.test"
    demo = f"staff-{tag}.demo@check.test"
    inst_id = realm.user(instructor, "TN-Instructors")
    realm.user(student, "TN-Students")
    realm.user(demo, "TN-Instructors")

    # (vii) banner
    _, _, page = Browser().fetch(realm.auth_url(secrets.token_urlsafe(48)))
    if args.expect_banner:
        check('id="tn-banner"' in page, "(vii) login page shows the banner (id=tn-banner)")
        check(re.search(r'<input[^>]*name="tn_ack"[^>]*required', page) is not None
              or re.search(r'<input[^>]*required[^>]*name="tn_ack"', page) is not None,
              "(vii) login page has a required tn_ack checkbox")

    # (i) privileged, no credential -> CONFIGURE_TOTP
    s = SignIn(realm, instructor)
    check(s.at_configure_totp, "(i) instructor with no second factor is sent to CONFIGURE_TOTP")

    # (ii) enrol. Keycloak 24 does not count the code typed while enrolling (a required
    # action, not the OTP authenticator): that first token is password-level, the API
    # answers mfa_required, and the SPA steps up (acr "mfa") in the same browser.
    secret = s.enrol_totp()
    c = claims(s.tokens()["access_token"])
    check(not mfa(c), f"(ii) the enrolment sign-in itself is not MFA (amr={c.get('amr')}, acr={c.get('acr')}): "
          "the API refuses it and the SPA steps up")
    wait_next_window()
    up = s.step_up()
    check(up.at_otp_form, "(ii) step-up in the same browser asks for the OTP code (not a password)")
    up.answer_otp(secret)
    tok = up.tokens()
    c = claims(tok["access_token"])
    check(c.get("acr") == "mfa" and "otp" in (c.get("amr") or []),
          f"(ii) step-up token: acr mfa, amr has otp (amr={c.get('amr')}, acr={c.get('acr')})")
    r = claims(realm.refresh(tok["refresh_token"])["access_token"])
    check(mfa(r), f"(ii) a refreshed token is still MFA (amr={r.get('amr')}, acr={r.get('acr')})")

    # (ii) a later sign-in in a new browser: the OTP form, then amr otp.
    wait_next_window()
    s2 = SignIn(realm, instructor)
    check(s2.at_otp_form, "(ii) the next sign-in asks for the OTP code")
    s2.answer_otp(secret)
    tok2 = s2.tokens()
    c2 = claims(tok2["access_token"])
    check("otp" in (c2.get("amr") or []), f"(ii) OTP sign-in token amr has otp (amr={c2.get('amr')})")
    r2 = claims(realm.refresh(tok2["refresh_token"])["access_token"])
    check("otp" in (r2.get("amr") or []), f"(ii) its refreshed token keeps amr otp (amr={r2.get('amr')})")

    # SSO cookie re-entry: a new authorization request in the same browser.
    v = secrets.token_urlsafe(48)
    _, url, _ = s2.browser.fetch(realm.auth_url(v))
    if url.startswith(REDIRECT):
        code = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["code"][0]
        c3 = claims(realm.exchange(code, v)["access_token"])
        check("otp" in (c3.get("amr") or []), f"(ii) SSO-cookie token keeps amr otp (amr={c3.get('amr')})")
    else:
        check(False, "(ii) SSO-cookie re-entry reached the app")

    # (iii) student: password only
    st = SignIn(realm, student)
    check(not st.at_configure_totp and not st.at_otp_form, "(iii) student gets no OTP page")
    cs = claims(st.tokens()["access_token"])
    check(cs.get("amr") == ["pwd"], f"(iii) student amr == ['pwd'] (amr={cs.get('amr')}, acr={cs.get('acr')})")

    # (vi) step-up for a student-group user asks for a second factor
    su = SignIn(realm, student, extra=STEP_UP)
    check(su.at_configure_totp or su.at_otp_form, "(vi) step-up (acr mfa requested) asks a Student for a second factor")
    if su.at_configure_totp:
        su.enrol_totp()
        csu = claims(su.tokens()["access_token"])
        check(csu.get("acr") == "mfa", f"(vi) step-up token acr == 'mfa' (acr={csu.get('acr')}, amr={csu.get('amr')})")

    # (v) demo exemption by role
    d = SignIn(realm, demo)
    check(d.at_configure_totp, "(v) a demo instructor WITHOUT tn-mfa-exempt is asked to enrol")
    role = realm.admin("GET", "/roles/tn-mfa-exempt")
    demo_id = realm.admin("GET", f"/users?username={urllib.parse.quote(demo)}&exact=true")[0]["id"]
    realm.admin("POST", f"/users/{demo_id}/role-mappings/realm", [role])
    d2 = SignIn(realm, demo)
    check(not d2.at_configure_totp and not d2.at_otp_form, "(v) holding tn-mfa-exempt, the demo instructor gets no OTP page")
    realm.admin("DELETE", f"/users/{demo_id}/role-mappings/realm", [role])

    # (iv) break-glass (roles/tn_keycloak/tasks/breakglass.yml): delete otp credentials,
    # require a new password and CONFIGURE_TOTP, log out
    for cred in realm.admin("GET", f"/users/{inst_id}/credentials"):
        if cred["type"] in ("otp", "webauthn"):
            realm.admin("DELETE", f"/users/{inst_id}/credentials/{cred['id']}")
    u = realm.admin("GET", f"/users/{inst_id}")
    actions = sorted(set(u.get("requiredActions", [])) | {"CONFIGURE_TOTP", "UPDATE_PASSWORD"})
    realm.admin("PUT", f"/users/{inst_id}", {"requiredActions": actions})
    realm.admin("POST", f"/users/{inst_id}/logout")
    s4 = SignIn(realm, instructor)
    check(s4.at_configure_totp, "(iv) after the break-glass reset the next sign-in is sent to CONFIGURE_TOTP")
    if s4.at_configure_totp:
        wait_next_window()
        s4.enrol_totp()
    check('name="password-new"' in s4.page, "(iv) ... and must set a new password before reaching the app "
          f"({page_message(s4.page)})")

    # (viii) what the API's startup check reads (app/auth_backends/keycloak_mfa_check.py),
    # with a service account holding view-realm only, as truenorth-api-admin does.
    realm.admin("POST", "/clients", {"clientId": f"view-only-{tag}", "publicClient": False,
                                     "serviceAccountsEnabled": True, "standardFlowEnabled": False,
                                     "secret": "view-only-" + tag + "-secret"})
    vid = realm.admin("GET", f"/clients?clientId=view-only-{tag}")[0]["id"]
    sa = realm.admin("GET", f"/clients/{vid}/service-account-user")["id"]
    rm = realm.admin("GET", "/clients?clientId=realm-management")[0]["id"]
    view = [r for r in realm.admin("GET", f"/users/{sa}/role-mappings/clients/{rm}/available") if r["name"] == "view-realm"]
    realm.admin("POST", f"/users/{sa}/role-mappings/clients/{rm}", view)
    data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": f"view-only-{tag}",
                                   "client_secret": "view-only-" + tag + "-secret"}).encode()
    with urllib.request.urlopen(f"{realm.base}/realms/{realm.realm}/protocol/openid-connect/token", data) as r:
        vtok = json.load(r)["access_token"]
    req = urllib.request.Request(f"{realm.base}/admin/realms/{realm.realm}", headers={"Authorization": f"Bearer {vtok}"})
    with urllib.request.urlopen(req) as r:
        rep_ = json.load(r)
    attrs = rep_.get("attributes") or {}
    check(rep_.get("browserFlow", "").startswith("tn-browser") and '"mfa"' in str(attrs.get("acr.loa.map"))
          and str(attrs.get("tn.mfa.configured", "")).startswith("1:"),
          "(viii) a view-realm-only service account sees the flow, acr mfa and tn.mfa.configured (the API's startup check)")

    print(f"\n{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
