"""Keycloak token, session, password and client rules: the realm file and the installer.

infra/keycloak/realm-truenorth.json is what a fresh install imports; install/roles/
tn_keycloak enforces the same settings on every run, so an install whose realm was
imported before them gets them too. These keep the two in step and hold the rules:

* access tokens live 5 minutes, browser sessions 30 min idle / 10 h max, offline
  sessions at most 7 days;
* local passwords: 12+ characters, not the username, not one of the last 5;
* no password grant on a user-facing client: only the installer's truenorth-smoke
  client has it, and that client is disabled except while 95-smoke-test runs;
* public clients require PKCE S256; no out-of-band redirect.

scripts/check_keycloak_realm.sh imports the file into the pinned Keycloak (CI job
keycloak-realm): the sample users' passwords must satisfy the policy, or the import
fails ("invalidPasswordMinLengthMessage").
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
REALM = json.loads((ROOT / "infra/keycloak/realm-truenorth.json").read_text())
ROLE = ROOT / "install/roles/tn_keycloak"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
SMOKE = "truenorth-smoke"
WEEK = 7 * 24 * 3600


def _clients() -> dict[str, dict]:
    return {c["clientId"]: c for c in REALM["clients"]}


def test_token_and_session_lifetimes():
    assert REALM["accessTokenLifespan"] == 300
    assert REALM["accessTokenLifespanForImplicitFlow"] <= 300
    assert REALM["ssoSessionIdleTimeout"] <= 1800
    assert REALM["ssoSessionMaxLifespan"] <= 12 * 3600
    assert REALM["offlineSessionIdleTimeout"] <= WEEK
    assert REALM["offlineSessionMaxLifespanEnabled"] is True
    assert REALM["offlineSessionMaxLifespan"] <= WEEK
    for client in REALM["clients"]:
        lifespan = (client.get("attributes") or {}).get("access.token.lifespan")
        assert lifespan is None or int(lifespan) <= 300, client["clientId"]


def test_password_policy():
    policy = REALM["passwordPolicy"]
    length = re.search(r"length\((\d+)\)", policy)
    assert length and int(length.group(1)) >= 12
    assert "notUsername" in policy
    assert re.search(r"passwordHistory\(\d+\)", policy)


def test_sample_users_satisfy_the_policy():
    """Otherwise the realm does not import at all (dev, itest, check_keycloak_realm.sh)."""
    for user in REALM.get("users", []):
        for cred in user.get("credentials", []):
            if cred.get("type") == "password":
                assert len(cred["value"]) >= 12, user["username"]
                assert cred["value"].lower() != user["username"].lower()


def test_only_the_smoke_client_has_the_password_grant_and_it_is_off():
    clients = _clients()
    grants = sorted(cid for cid, c in clients.items() if c.get("directAccessGrantsEnabled"))
    assert grants == [SMOKE]
    smoke = clients[SMOKE]
    assert smoke["enabled"] is False and smoke["publicClient"] is False
    assert not smoke.get("standardFlowEnabled") and not smoke.get("redirectUris")
    assert clients["truenorth-api"]["publicClient"] is False


def test_public_clients_require_pkce_and_have_no_oob_redirect():
    public = [c for c in REALM["clients"] if c.get("publicClient")]
    assert {c["clientId"] for c in public} == {"truenorth-web", "truenorth-cli"}
    for client in public:
        assert client["attributes"].get("pkce.code.challenge.method") == "S256", client["clientId"]
    for client in REALM["clients"]:
        assert not any("urn:ietf:wg:oauth:2.0:oob" in u for u in client.get("redirectUris") or []), client["clientId"]


def test_installer_enforces_the_realm_settings_every_run():
    want = DEFAULTS["tn_kc_realm_settings"]
    for key, value in want.items():
        assert REALM[key] == value, f"tn_kc_realm_settings.{key}={value!r}, realm file has {REALM.get(key)!r}"
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    put = next(t for t in tasks if t.get("name") == "Keycloak — update realm token/session/password settings")
    assert "tn_kc_realm_settings" in put["vars"]["_want"]
    # Checked on every run (not only at import) against the realm as it is now, and PUT
    # only when a field differs, so a clean re-run reports no change.
    assert any(t.get("name") == "Keycloak — read the realm's current settings" for t in tasks)
    assert put["when"] == "tn_kc_realm_now.json | combine(_want) != tn_kc_realm_now.json"


def test_installer_enforces_the_client_rules_every_run():
    settings = DEFAULTS["tn_kc_client_settings"]
    for cid in ("truenorth-web", "truenorth-api", "truenorth-cli"):
        assert settings[cid]["directAccessGrantsEnabled"] is False, cid
    for cid in ("truenorth-web", "truenorth-cli"):
        assert settings[cid]["attributes"]["pkce.code.challenge.method"] == "S256", cid
    assert settings["truenorth-cli"]["redirectUris"] == []
    assert settings[SMOKE]["enabled"] is False and settings[SMOKE]["directAccessGrantsEnabled"] is True
    assert DEFAULTS["tn_kc_smoke_client"] == SMOKE
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    read = next(t for t in tasks if t.get("name") == "Keycloak — read the clients")
    assert set(settings) <= {str(i).replace("{{ tn_keycloak_web_client }}", "truenorth-web")
                             .replace("{{ tn_keycloak_api_client }}", "truenorth-api")
                             .replace("{{ tn_kc_smoke_client }}", SMOKE)
                             .replace("{{ tn_keycloak_api_admin_client }}", "truenorth-api-admin")
                             for i in read["loop"]}


def test_the_smoke_test_uses_its_own_client_and_switches_it_off_again():
    tasks = yaml.safe_load((ROOT / "install/roles/tn_smoke/tasks/main.yml").read_text())
    [block] = [t for t in tasks if "block" in t]
    token = next(t for t in block["block"] if t["name"] == "Smoke — obtain a token for the bootstrap admin's AD account")
    body = token["ansible.builtin.uri"]["body"]
    assert body["grant_type"] == "password"
    assert body["client_id"] == "{{ tn_kc_smoke_client }}"
    assert "keycloak_smoke_client_secret" in body["client_secret"]
    [off] = block["always"]
    assert off["ansible.builtin.uri"]["body"]["enabled"] is False
    secrets = yaml.safe_load((ROOT / "install/roles/tn_config/defaults/main.yml").read_text())["tn_generated_secrets"]
    assert "keycloak_smoke_client_secret" in {s["name"] for s in secrets}
