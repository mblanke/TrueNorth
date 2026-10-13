"""Directory-backed (AD) sign-in, qualified against a real directory (plan M2).

Two modes, one suite:

``AD_MODE=fixture``   the CI `ad` lane: the Samba AD test DC, Keycloak and the rogue-CA
                      front end of tools/ad-test-dc (``tools/ad-test-dc/run.sh test`` sets
                      every variable below). Federated by install/roles/tn_keycloak.
``AD_MODE=external``  the same checks against a real DC behind a real Keycloak (the owner's
                      Windows AD DS/AD CS for staging). Point the variables at it; a check
                      whose inputs are not supplied skips and says which (README "External
                      DC mode" in tools/ad-test-dc).

Unset: every test skips. The API runs in-process (as the Moodle lane's tests do) with its
real Keycloak token backend: each request below carries a real token from the directory.

Variables (fixture defaults in parentheses):

  AD_KC_URL              Keycloak base, with /auth (http://127.0.0.1:18480/auth)
  AD_KC_REALM            realm (truenorth)
  AD_KC_CLIENT           a client with the password grant and the truenorth-identity scope
                         (truenorth-ad-test); AD_KC_CLIENT_SECRET_FILE if it is confidential
  AD_KC_ADMIN_USER / AD_KC_ADMIN_PASSWORD_FILE   master-realm admin, for the federation
                         checks (admin / $AD_SECRETS_DIR/keycloak-admin)
  AD_SECRETS_DIR         one password file per account, named by sAMAccountName (0600)
  AD_USER_<KEY>          the sAMAccountName for each role below (fixture accounts.tsv)
  AD_LOCAL_ADMIN_UPN     the local recovery administrator; password file ``local-admin``
  AD_LDAPS_ADDR / AD_DC_HOST / AD_CA_FILE   host:port to reach LDAPS, the name its
                         certificate carries, and the CA that should have issued it
  AD_ROGUE_URL           an ldaps:// URL presenting a certificate from an untrusted CA
  AD_MISNAMED_URL        an ldaps:// URL presenting a TRUSTED certificate for another name
  AD_GROUP_REMOVE_CMD / AD_GROUP_ADD_CMD / AD_USER_DISABLE_CMD / AD_USER_ENABLE_CMD
                         shell templates ({group}, {user}) that change the directory
  AD_GROUP_DELETE_CMD / AD_GROUP_CREATE_CMD ({group})   remove/recreate a TN-* group
  AD_FEDERATE_CMD        re-run the installer's Keycloak tasks (fixture: run.sh federate)
  AD_DC_STOP_CMD / AD_DC_START_CMD   stop/start the DC (recovery-path test)
  AD_PROPAGATION_WAIT=1  also measure the periodic changed-users sync (up to ~7 min)
  AD_REPORT              where the measurements are written as JSON
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import shlex
import socket
import ssl
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

pytestmark = pytest.mark.integration

MODE = os.getenv("AD_MODE", "").strip().lower()
if MODE not in ("fixture", "external"):
    pytest.skip("AD_MODE is not set (fixture: tools/ad-test-dc/run.sh test; external: see its README)",
                allow_module_level=True)

ROOT = Path(__file__).resolve().parents[2]
KC = os.getenv("AD_KC_URL", "http://127.0.0.1:18480/auth").rstrip("/")
REALM = os.getenv("AD_KC_REALM", "truenorth")
CLIENT = os.getenv("AD_KC_CLIENT", "truenorth-ad-test")
SECRETS = Path(os.getenv("AD_SECRETS_DIR", str(ROOT / "build/ad-test-dc/secrets")))
LOCAL_ADMIN = os.getenv("AD_LOCAL_ADMIN_UPN", "tnadmin@tn-ad-test.local")
REPORT = Path(os.getenv("AD_REPORT", str(ROOT / "build/ad-test-dc/ad-report.json")))
TOKEN_URL = f"{KC}/realms/{REALM}/protocol/openid-connect/token"

# The installer's own group list and group -> role map (inventory group_vars), so the
# lane checks the configuration staging and production render, not a copy of it.
_GV = yaml.safe_load((ROOT / "install/inventory/group_vars/all/main.yml").read_text())
TN_GROUPS: list[str] = _GV["tn_ad_groups"]
ROLE_MAP: dict[str, str] = _GV["tn_registration_group_role_map"]

# key -> (fixture sAMAccountName, the group it is in, the role TrueNorth suggests)
ACCOUNTS = {
    "ADMIN": ("ad.admin", "TN-Platform-Admins", "admin"),
    "RANGEOPS": ("ad.rangeops", "TN-Range-Operators", "range_ops"),
    "INSTRUCTOR": ("ad.instructor", "TN-Instructors", "instructor"),
    "AUTHOR": ("ad.author", "TN-Content-Authors", "instructor"),
    "STUDENT": ("ad.student", "TN-Students", "student"),
    "OBSERVER": ("ad.observer", "TN-Observers", "observer"),
}
EXTRA = {"MULTI": "ad.multi", "NESTED": "ad.nested", "MOVER": "ad.mover", "DISABLED": "ad.disabled",
         "COLLISION": "ad.collision", "TOGGLE": "ad.author"}

MEASURED: dict = {}


def sam(key: str) -> str:
    default = ACCOUNTS[key][0] if key in ACCOUNTS else EXTRA[key]
    return os.getenv(f"AD_USER_{key}", default)


def password(name: str) -> str:
    f = SECRETS / name
    if not f.is_file():
        pytest.skip(f"no password file {f} (AD_SECRETS_DIR)")
    return f.read_text().strip()


def run_cmd(var: str, **fields) -> None:
    template = os.getenv(var, "").strip()
    if not template:
        pytest.skip(f"{var} is not set: this check changes the directory")
    cmd = template.format(**{k: shlex.quote(v) for k, v in fields.items()})
    subprocess.run(cmd, shell=True, check=True, capture_output=True, timeout=120)  # noqa: S602 - operator-supplied template


def grant(username: str, secret: str) -> httpx.Response:
    data = {"grant_type": "password", "client_id": CLIENT, "username": username, "password": secret,
            "scope": "openid"}
    cs = os.getenv("AD_KC_CLIENT_SECRET_FILE", "").strip()
    if cs:
        data["client_secret"] = Path(cs).read_text().strip()
    return httpx.post(TOKEN_URL, data=data, timeout=60)


def refresh(refresh_token: str) -> httpx.Response:
    data = {"grant_type": "refresh_token", "client_id": CLIENT, "refresh_token": refresh_token}
    cs = os.getenv("AD_KC_CLIENT_SECRET_FILE", "").strip()
    if cs:
        data["client_secret"] = Path(cs).read_text().strip()
    return httpx.post(TOKEN_URL, data=data, timeout=60)


def login(key_or_name: str, secret_name: str | None = None) -> dict:
    name = sam(key_or_name) if key_or_name.isupper() else key_or_name
    r = grant(name, password(secret_name or name))
    assert r.status_code == 200, f"{name}: {r.status_code} {r.text[:200]}"
    return r.json()


def claims(token: str) -> dict:
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def admin_headers() -> dict:
    pw_file = os.getenv("AD_KC_ADMIN_PASSWORD_FILE", str(SECRETS / "keycloak-admin"))
    if not Path(pw_file).is_file():
        pytest.skip("no Keycloak admin credential (AD_KC_ADMIN_PASSWORD_FILE)")
    r = httpx.post(f"{KC}/realms/master/protocol/openid-connect/token", timeout=30, data={
        "grant_type": "password", "client_id": "admin-cli",
        "username": os.getenv("AD_KC_ADMIN_USER", "admin"), "password": Path(pw_file).read_text().strip()})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def component() -> dict:
    r = httpx.get(f"{KC}/admin/realms/{REALM}/components", headers=admin_headers(), timeout=30,
                  params={"type": "org.keycloak.storage.UserStorageProvider", "name": "corp-ad"})
    r.raise_for_status()
    assert r.json(), "no 'corp-ad' federation component (install/roles/tn_keycloak ldap_federation.yml)"
    return r.json()[0]


def sync(action: str = "triggerChangedUsersSync") -> dict:
    r = httpx.post(f"{KC}/admin/realms/{REALM}/user-storage/{component()['id']}/sync",
                   params={"action": action}, headers=admin_headers(), timeout=300)
    r.raise_for_status()
    return r.json()


def record(key: str, value) -> None:
    MEASURED[key] = value
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({"mode": MODE, "at": dt.datetime.now(dt.UTC).isoformat(), **MEASURED}, indent=2))


# ── The API, in-process, with its real Keycloak token backend ─────────────────
@pytest.fixture
def api(client, monkeypatch):
    """The TestClient with AUTH_DISABLED off and app.auth_backends' Keycloak backend
    pointed at this directory's realm (audience checked, as under TN_ENV=production)."""
    from app import auth, auth_backends
    from app.auth_backends.keycloak_oidc import KeycloakOIDCBackend

    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(auth_backends, "_instance",
                        KeycloakOIDCBackend(keycloak_url=KC, realm=REALM, audience="truenorth-api"))
    monkeypatch.setenv("REGISTRATION_ALLOWED_GROUPS", ",".join(TN_GROUPS))
    monkeypatch.setenv("REGISTRATION_GROUP_ROLE_MAP", json.dumps(ROLE_MAP))
    return client


def bearer(tok: dict) -> dict:
    return {"Authorization": f"Bearer {tok['access_token']}"}


def approve(api, db_session, request_id: str, role: str, tenant_id) -> httpx.Response:
    from _shared import act_as, real_user
    from app.auth import get_current_user
    from app.main import app as fastapi_app
    from app.models import UserRole

    act_as(real_user(db_session, UserRole.admin, tenant_id))
    try:
        return api.post(f"/registration/requests/{request_id}/approve", json={"role": role, "tenant_id": str(tenant_id)})
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


# ── 1. LDAPS trust ───────────────────────────────────────────────────────────
def _ldaps(ca_file: str, hostname: str | None = None) -> dict:
    addr = os.getenv("AD_LDAPS_ADDR", "")
    host = hostname or os.getenv("AD_DC_HOST", "")
    if not (addr and host):
        pytest.skip("AD_LDAPS_ADDR / AD_DC_HOST are not set")
    h, _, p = addr.rpartition(":")
    ctx = ssl.create_default_context(cafile=ca_file)
    with socket.create_connection((h, int(p)), timeout=15) as raw, ctx.wrap_socket(raw, server_hostname=host) as tls:
        return tls.getpeercert()


def test_ldaps_certificate_chains_to_the_supplied_ca():
    ca = os.getenv("AD_CA_FILE", "")
    if not ca:
        pytest.skip("AD_CA_FILE is not set")
    cert = _ldaps(ca)
    record("ldaps_subject", dict(x[0] for x in cert["subject"]))


def test_ldaps_refuses_a_certificate_from_an_untrusted_ca(tmp_path):
    """A CA nobody installed: the handshake must fail on verification, not succeed."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Untrusted Test CA")])
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now)
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    other = tmp_path / "untrusted-ca.pem"
    other.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    with pytest.raises(ssl.SSLCertVerificationError):
        _ldaps(str(other))


def test_ldaps_refuses_the_right_ca_for_the_wrong_name():
    """Hostname verification on the client side: the DC's certificate, from the trusted CA,
    asked for under a name it was not issued to."""
    ca = os.getenv("AD_CA_FILE", "")
    if not ca:
        pytest.skip("AD_CA_FILE is not set")
    with pytest.raises(ssl.SSLCertVerificationError):
        _ldaps(ca, hostname="not-the-dc.invalid")


def _test_ldap(action: str, url: str, comp: dict) -> httpx.Response:
    cfg = comp["config"]
    return httpx.post(f"{KC}/admin/realms/{REALM}/testLDAPConnection", headers=admin_headers(), timeout=60, json={
        "action": action, "connectionUrl": url, "bindDn": cfg["bindDn"][0],
        # Keycloak substitutes the stored credential for its mask when componentId is given.
        "bindCredential": "**********", "componentId": comp["id"],
        "useTruststoreSpi": "always", "startTls": "false", "authType": "simple"})


def test_keycloak_binds_to_the_dc_and_refuses_an_untrusted_ca():
    """Keycloak's own LDAPS client: the real DC binds; a front end presenting a certificate
    from a CA that is not in Keycloak's truststore fails the handshake (no bind)."""
    comp = component()
    real = comp["config"]["connectionUrl"][0]
    ok = _test_ldap("testAuthentication", real, comp)
    assert ok.status_code == 204, f"bind to {real}: {ok.status_code} {ok.text[:200]}"
    rogue = os.getenv("AD_ROGUE_URL", "").strip()
    if not rogue:
        pytest.skip("AD_ROGUE_URL is not set (the trusted half passed)")
    bad = _test_ldap("testConnection", rogue, comp)
    assert bad.status_code == 400, f"untrusted CA accepted at {rogue}: {bad.status_code}"
    bad_auth = _test_ldap("testAuthentication", rogue, comp)
    assert bad_auth.status_code == 400


def test_keycloak_refuses_a_trusted_certificate_for_another_hostname():
    """A front end presenting a certificate from the TRUSTED test CA, but issued to another
    name than the one Keycloak connects to: hostname verification must refuse it, or any
    certificate the CA ever issued could stand in for the DC."""
    misnamed = os.getenv("AD_MISNAMED_URL", "").strip()
    if not misnamed:
        pytest.skip("AD_MISNAMED_URL is not set")
    comp = component()
    for action in ("testConnection", "testAuthentication"):
        r = _test_ldap(action, misnamed, comp)
        assert r.status_code == 400, f"{action}: wrong-name certificate accepted at {misnamed}: {r.status_code}"


# ── 2. Group -> role mapping and tenant binding ──────────────────────────────
@pytest.mark.parametrize("key", list(ACCOUNTS))
def test_group_suggests_the_role(api, key):
    _, group, role = ACCOUNTS[key]
    tok = login(key)
    c = claims(tok["access_token"])
    assert group in c.get("groups", []), f"{sam(key)} token groups {c.get('groups')}"
    assert "truenorth-api" in (c["aud"] if isinstance(c["aud"], list) else [c["aud"]])
    assert c.get("ad_object_guid"), "ad_object_guid claim missing (ad-object-guid mapper)"
    me = api.get("/auth/me", headers=bearer(tok))
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["status"] in ("unregistered", "registered", "pending")
    if body["status"] == "unregistered":
        assert body["suggestions"]["role"] == role
        assert body["suggestions"]["may_register"] is True
        assert body["prefill"]["ad_object_guid"] == c["ad_object_guid"]


def test_several_groups_resolve_to_the_highest_role(api):
    tok = login("MULTI")
    groups = claims(tok["access_token"]).get("groups", [])
    assert {"TN-Students", "TN-Instructors"} <= set(groups)
    me = api.get("/auth/me", headers=bearer(tok)).json()
    assert me["suggestions"]["role"] == "instructor"


def test_tenant_binding_follows_the_group_and_approval_lands_there(api, db_session, monkeypatch):
    """TN-Students -> tenant 'alpha-wing' (REGISTRATION_GROUP_TENANT_MAP). The suggestion
    names it, approval creates the account there, and the same AD token then resolves to
    that row (registered, source 'ad', objectGUID kept)."""
    from _shared import real_tenant
    from app.models import User

    wing = real_tenant(db_session, f"alpha-wing-{uuid.uuid4().hex[:6]}")
    monkeypatch.setenv("REGISTRATION_GROUP_TENANT_MAP", json.dumps({"TN-Students": wing.slug}))
    tok = login("STUDENT")
    me = api.get("/auth/me", headers=bearer(tok)).json()
    assert me["status"] == "unregistered"
    assert me["suggestions"]["tenant_id"] == str(wing.id)

    sub = api.post("/registration", json={"timezone": "UTC"}, headers=bearer(tok))
    assert sub.status_code == 201, sub.text
    r = approve(api, db_session, sub.json()["id"], "student", wing.id)
    assert r.status_code == 200, r.text

    me = api.get("/auth/me", headers=bearer(tok)).json()
    assert me["status"] == "registered"
    assert me["user"]["tenant_id"] == str(wing.id) and me["user"]["role"] == "student"
    row = db_session.query(User).filter(User.keycloak_id == claims(tok["access_token"])["sub"]).one()
    assert row.source == "ad" and row.ad_object_guid == claims(tok["access_token"])["ad_object_guid"]


def test_an_unmapped_group_falls_back_to_the_default_tenant(api, db_session):
    from app.models import Tenant

    default = db_session.query(Tenant).filter(Tenant.slug == "default").one()
    me = api.get("/auth/me", headers=bearer(login("OBSERVER"))).json()
    assert me["suggestions"]["tenant_id"] == str(default.id)


# ── 3. Nested groups: direct membership only (documented policy) ─────────────
def test_nested_group_membership_is_not_expanded(api):
    """ad.nested is in TN-Cohort-Alpha, which is a member of TN-Students. The federation
    reads direct membership only (LOAD_GROUPS_BY_MEMBER_ATTRIBUTE) and only the exact TN-*
    group DNs (groups.ldap.filter), so the claim carries neither TN-Students nor
    TN-Cohort-Alpha: no role is suggested and registration (limited to the TN-* groups) is
    refused. Put people directly in a TN-* group.

    ad.nested is also in the fixture's decoy, a group whose cn is TN-Platform-Admins in a
    sub-OU of Groups: a same-named group elsewhere must never grant that claim."""
    tok = login("NESTED")
    groups = claims(tok["access_token"]).get("groups", [])
    assert "TN-Students" not in groups
    assert "TN-Platform-Admins" not in groups, "a same-named group in a sub-OU counted"
    assert not set(groups) - set(TN_GROUPS), f"a non-TN-* group reached the claim: {groups}"
    me = api.get("/auth/me", headers=bearer(tok)).json()
    assert me["suggestions"]["role"] is None
    assert me["suggestions"]["may_register"] is False
    r = api.post("/registration", json={"timezone": "UTC"}, headers=bearer(tok))
    assert r.status_code == 403, r.text
    record("nested_group_claim", groups)


# ── 4. Disabled accounts ─────────────────────────────────────────────────────
def test_a_disabled_ad_account_gets_no_token():
    r = grant(sam("DISABLED"), password(sam("DISABLED")))
    assert r.status_code in (400, 401), r.text
    assert "access_token" not in r.text
    record("disabled_account_response", r.json().get("error_description"))


def test_disabling_an_account_mid_session_is_measured():
    """Measured: a new sign-in fails at once (AD refuses the bind). A refresh token keeps
    working until Keycloak re-reads the user (the changed-users sync, every
    changedSyncPeriod = 300 s), then fails with 'User disabled'. Tokens already issued
    stay valid until they expire (access token 300 s); the API checks no session."""
    user = sam("TOGGLE")
    tok = login(user)
    run_cmd("AD_USER_DISABLE_CMD", user=user)
    try:
        assert grant(user, password(user)).status_code in (400, 401), "new sign-in still works after disable"
        before = refresh(tok["refresh_token"]).status_code
        sync()
        after = refresh(tok["refresh_token"])
        assert after.status_code == 400, after.text
        record("disable_refresh_before_sync", before)
        record("disable_refresh_after_sync", after.json().get("error_description"))
    finally:
        run_cmd("AD_USER_ENABLE_CMD", user=user)
        sync()
    assert grant(user, password(user)).status_code == 200


# ── 5. Group removal: what drops, and when ───────────────────────────────────
def test_group_removal_drops_the_claim_not_the_truenorth_role(api, db_session):
    """Remove ad.mover from TN-Instructors. The `groups` claim drops after Keycloak
    re-reads the user (a changed-users sync), so at the next token issue (sign-in or
    refresh). The TrueNorth role does NOT change: AD groups suggest, an approval grants
    (docs/identity.md); revoking TrueNorth access is a TrueNorth action (disable the
    account or change its role), or disabling the AD account (previous test)."""
    from app.models import User

    user, group = sam("MOVER"), "TN-Instructors"
    tok = login(user)
    assert group in claims(tok["access_token"]).get("groups", [])
    sub = api.post("/registration", json={"timezone": "UTC"}, headers=bearer(tok))
    assert sub.status_code in (201, 409), sub.text
    if sub.status_code == 201:
        from app.models import Tenant

        tid = db_session.query(Tenant).filter(Tenant.slug == "default").one().id
        assert approve(api, db_session, sub.json()["id"], "instructor", tid).status_code == 200

    # Worst case: Keycloak has this user cached with the group (a sign-in loads it). From a
    # fresh load the next sign-in may already read the change; from a warm cache it waits
    # for the user to be re-read (measured both ways while building this lane).
    httpx.post(f"{KC}/admin/realms/{REALM}/clear-user-cache", headers=admin_headers(), timeout=30)
    login(user)
    login(user)
    run_cmd("AD_GROUP_REMOVE_CMD", group=group, user=user)
    removed_at = time.monotonic()
    try:
        record("group_removed_claim_before_sync",
               group in claims(login(user)["access_token"]).get("groups", []))
        if os.getenv("AD_PROPAGATION_WAIT", "") == "1":
            period = int(component()["config"].get("changedSyncPeriod", ["300"])[0])
            deadline = removed_at + period + 120
            while time.monotonic() < deadline:
                if group not in claims(login(user)["access_token"]).get("groups", []):
                    break
                time.sleep(10)
            elapsed = round(time.monotonic() - removed_at)
            record("group_removal_periodic_seconds", elapsed)
            record("changed_sync_period_seconds", period)
            assert time.monotonic() < deadline, f"claim still present {elapsed}s after removal"
        else:
            sync()
        fresh = login(user)
        assert group not in claims(fresh["access_token"]).get("groups", [])
        assert group not in claims(refresh(tok["refresh_token"]).json()["access_token"]).get("groups", [])
        me = api.get("/auth/me", headers=bearer(fresh)).json()
        assert me["status"] == "registered"
        assert me["user"]["role"] == "instructor", "the approved role is TrueNorth's, not AD's"
        assert db_session.query(User).filter(User.keycloak_id == claims(fresh["access_token"])["sub"]).one().is_active
    finally:
        run_cmd("AD_GROUP_ADD_CMD", group=group, user=user)
        sync()


# ── 6. Email collision ───────────────────────────────────────────────────────
def test_an_ad_account_with_a_local_staff_email_never_signs_in_as_it(api, db_session):
    """ad.collision (TN-Platform-Admins) carries the local recovery administrator's email.
    Keycloak refuses to import it (duplicate email) so it gets no token at all, and the
    local administrator still signs in as itself with its own subject and groups."""
    from app import bootstrap_admin
    from app.models import Tenant, User

    local = login(LOCAL_ADMIN, "local-admin")
    lc = claims(local["access_token"])
    assert lc["email"].lower() == LOCAL_ADMIN.lower()
    assert "TN-Platform-Admins" in lc.get("groups", [])
    assert not lc.get("ad_object_guid"), "the recovery administrator must be a LOCAL account"

    collider = sam("COLLISION")
    r = grant(collider, password(collider))
    assert r.status_code in (400, 401), f"{collider} signed in: {r.status_code}"
    record("collision_response", r.json().get("error_description"))

    # TrueNorth side: the bootstrap admin row (as 80-seed makes it) stays bound to the local
    # subject and keeps its role.
    tenant = db_session.query(Tenant).filter(Tenant.slug == "default").one()
    row = bootstrap_admin.ensure_admin(db_session, tenant, {"id": lc["sub"], "email": lc["email"],
                                                            "username": LOCAL_ADMIN})
    me = api.get("/auth/me", headers=bearer(local)).json()
    assert me["status"] == "registered" and me["user"]["role"] == "admin"
    db_session.refresh(row)
    assert row.keycloak_id == lc["sub"]
    assert db_session.query(User).filter(User.email == row.email).count() == 1


def test_keycloak_never_creates_a_local_account_over_a_directory_email():
    """The other direction (why 60-keycloak makes the recovery admin BEFORE federating):
    with the directory federated, a local account whose email an AD user has is refused."""
    student = sam("STUDENT")
    email = claims(login(student)["access_token"])["email"]
    r = httpx.post(f"{KC}/admin/realms/{REALM}/users", headers=admin_headers(), timeout=30,
                   json={"username": f"shadow-{uuid.uuid4().hex[:6]}@local.test", "email": email,
                         "enabled": False, "firstName": "Shadow", "lastName": "Account"})
    if r.status_code == 201:  # never leave it behind (external mode runs on staging)
        httpx.delete(r.headers["Location"], headers=admin_headers(), timeout=30)
    assert r.status_code == 409, f"local account created over {student}'s email: {r.status_code}"


# ── 7. MFA for AD staff (S-track) ────────────────────────────────────────────
def test_an_ad_password_sign_in_is_not_mfa():
    """app.mfa_policy (on main) decides "was this sign-in MFA" from amr/acr. A real AD
    password sign-in through Keycloak (acr "1", no otp amr) must not count."""
    from app import mfa_policy

    c = claims(login("ADMIN")["access_token"])
    assert not mfa_policy.mfa_satisfied(c), f"password-only AD sign-in counted as MFA: amr={c.get('amr')} acr={c.get('acr')}"


def test_ad_staff_fall_under_the_mfa_roles():
    """Role-based MFA (S1: mfa_policy.mfa_required_for / mfa_denial_reason) is on branch
    claude/s1-mfa, not main. When it lands this runs instead of skipping: an AD
    TN-Platform-Admins password sign-in is refused staff access without a second factor."""
    from app import mfa_policy

    if not hasattr(mfa_policy, "mfa_required_for"):
        pytest.skip("TODO(M2/S1): role-based MFA (mfa_policy.mfa_required_for) is not on main yet "
                    "(branch claude/s1-mfa)")
    c = claims(login("ADMIN")["access_token"])
    assert mfa_policy.mfa_required_for("admin")
    assert mfa_policy.mfa_denial_reason(c, "admin", c.get("email")) is not None


# ── 8. Downstream Moodle SSO identity ────────────────────────────────────────
def test_moodle_sso_identity_is_the_truenorth_id(api, db_session):
    """An approved AD Student opens Moodle: the ticket's subject is the TrueNorth user id,
    never the Keycloak subject, the objectGUID or the AD username, so a directory rename
    or re-federation does not change who they are in Moodle."""
    import jwt
    from app import lti13
    from app.models import ExternalPlatform, IntegrationAuthType, Tenant, User

    tenant = db_session.query(Tenant).filter(Tenant.slug == "default").one()
    issuer = "http://moodle.ad-lane.test"
    db_session.add(ExternalPlatform(id=uuid.uuid4(), name="Moodle", slug=f"moodle-{uuid.uuid4().hex[:6]}",
                                    platform_type="moodle", base_url="http://moodle:8080",
                                    auth_type=IntegrationAuthType.lti13, tenant_id=tenant.id,
                                    lti_issuer=issuer, is_active=True))
    db_session.flush()
    tok = login("OBSERVER")
    c = claims(tok["access_token"])
    sub = api.post("/registration", json={"timezone": "UTC"}, headers=bearer(tok))
    assert sub.status_code == 201, sub.text
    assert approve(api, db_session, sub.json()["id"], "student", tenant.id).status_code == 200
    row = db_session.query(User).filter(User.keycloak_id == c["sub"]).one()

    r = api.post("/integrations/moodle/sso", json={}, headers=bearer(tok))
    assert r.status_code == 200, r.text
    key = lti13.get_tool_key(db_session)
    t = jwt.decode(r.json()["token"], key.public_key_pem, algorithms=["RS256"], audience=issuer)
    assert t["sub"] == str(row.id)
    assert t["sub"] not in (c["sub"], c.get("ad_object_guid"), c.get("preferred_username"))
    assert t["tid"] == str(tenant.id) and t["role"] == "student"


# ── 9. The installer's own checks (fixture: AD_FEDERATE_CMD re-runs federate.yml) ─────
def _federate(**env: str) -> subprocess.CompletedProcess:
    cmd = os.getenv("AD_FEDERATE_CMD", "").strip()
    if not cmd:
        pytest.skip("AD_FEDERATE_CMD is not set (re-runs the installer's Keycloak tasks)")
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=600,  # noqa: S602 - operator-supplied
                          env={**os.environ, **env})


def test_turning_the_local_admin_off_disables_it_and_on_restores_it():
    """tn_keycloak_local_admin false with AD on: the local recovery administrator is
    disabled, not left as an unmanaged sign-in; true again re-enables it. AD accounts are
    never touched by it."""
    assert grant(LOCAL_ADMIN, password("local-admin")).status_code == 200
    try:
        off = _federate(AD_KEEP_LOCAL_ADMIN="false")
        assert off.returncode == 0, off.stdout[-2000:]
        assert "there is now NO sign-in that works without" in off.stdout
        assert grant(LOCAL_ADMIN, password("local-admin")).status_code in (400, 401)
        assert grant(sam("ADMIN"), password(sam("ADMIN"))).status_code == 200
    finally:
        on = _federate()
    assert on.returncode == 0, on.stdout[-2000:]
    assert grant(LOCAL_ADMIN, password("local-admin")).status_code == 200


def _mapper(name: str) -> dict:
    comp = component()
    r = httpx.get(f"{KC}/admin/realms/{REALM}/components", headers=admin_headers(), timeout=30,
                  params={"parent": comp["id"], "name": name})
    r.raise_for_status()
    return r.json()[0]


def test_installer_puts_an_edited_group_mapper_back():
    """ldap_federation.yml reconciles sub-mappers like the component: someone switches
    ad-groups to LDAP_ONLY and drops its filter; the next run restores both."""
    m = _mapper("ad-groups")
    want_filter = m["config"]["groups.ldap.filter"]
    edited = {**m, "config": {**m["config"], "mode": ["LDAP_ONLY"], "groups.ldap.filter": [""]}}
    httpx.put(f"{KC}/admin/realms/{REALM}/components/{m['id']}", headers=admin_headers(), json=edited,
              timeout=30).raise_for_status()
    assert _mapper("ad-groups")["config"]["mode"] == ["LDAP_ONLY"]
    run = _federate()
    assert run.returncode == 0, run.stdout[-2000:]
    back = _mapper("ad-groups")["config"]
    assert back["mode"] == ["READ_ONLY"] and back["groups.ldap.filter"] == want_filter


def test_installer_fails_when_a_tn_group_is_missing_from_ad():
    """The local recovery administrator's TN-* groups exist in Keycloak whatever AD holds;
    ldap_sync.yml must still fail when AD lacks one (it counts the mapper's LDAP groups)."""
    group, member = "TN-Observers", sam("OBSERVER")
    run_cmd("AD_GROUP_DELETE_CMD", group=group)
    try:
        run = _federate()
        assert run.returncode != 0, "federate passed with a TN-* group missing from AD"
        assert "found 5 of the 6 TN-* groups" in run.stdout, run.stdout[-2000:]
    finally:
        run_cmd("AD_GROUP_CREATE_CMD", group=group)
        run_cmd("AD_GROUP_ADD_CMD", group=group, user=member)
    run = _federate()
    assert run.returncode == 0, run.stdout[-2000:]


# ── 10. The local recovery path (last: it stops the DC) ──────────────────────
def test_zz_local_admin_signs_in_while_the_directory_is_down():
    """Stop the DC: the local recovery administrator still gets a token (Keycloak finds it
    locally and never asks the directory); an AD user fails cleanly and quickly. Start the
    DC again and AD sign-in recovers."""
    login(LOCAL_ADMIN, "local-admin")
    run_cmd("AD_DC_STOP_CMD")
    try:
        t0 = time.monotonic()
        local = grant(LOCAL_ADMIN, password("local-admin"))
        record("dc_down_local_admin_seconds", round(time.monotonic() - t0, 2))
        assert local.status_code == 200, local.text
        t0 = time.monotonic()
        ad = grant(sam("STUDENT"), password(sam("STUDENT")))
        record("dc_down_ad_user_seconds", round(time.monotonic() - t0, 2))
        record("dc_down_ad_user_status", ad.status_code)
        assert ad.status_code != 200, "an AD user signed in with the DC stopped"
    finally:
        run_cmd("AD_DC_START_CMD")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if grant(sam("STUDENT"), password(sam("STUDENT"))).status_code == 200:
            break
        time.sleep(5)
    else:
        pytest.fail("AD sign-in did not recover within 180 s of starting the DC")
