"""The login banner (NIST AC-8): theme, realm, installer and compose agree (v1.2.0, S2).

The behaviour is proven on the pinned Keycloak image by scripts/check_keycloak_realm.sh
(CI job keycloak-realm: the banner and a required acknowledgement box on the sign-in page,
and a realm localization override replacing the text, MessageFormat quoting included) and
scripts/check_keycloak_mfa.sh (every sign-in there ticks the box). Here, statically:

* the realm file and tn_kc_realm_settings both select the truenorth theme;
* login.ftl shows tnLoginBanner and a required tn_ack checkbox, and says which upstream
  file it was copied from;
* the compose files mount the theme read-only;
* banner.yml writes every message, is idempotent (when:), and hides nothing (no_log false).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
THEME = ROOT / "infra/keycloak/themes/truenorth/login"
ROLE = ROOT / "install/roles/tn_keycloak"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
GROUP_VARS = yaml.safe_load((ROOT / "install/inventory/group_vars/all/main.yml").read_text())


def test_realm_file_and_installer_select_the_theme():
    import json

    realm = json.loads((ROOT / "infra/keycloak/realm-truenorth.json").read_text())
    for settings in (realm, DEFAULTS["tn_kc_realm_settings"]):
        assert settings["loginTheme"] == "truenorth"
        assert settings["internationalizationEnabled"] is True
        assert settings["supportedLocales"] == ["en"] and settings["defaultLocale"] == "en"


def test_login_ftl_shows_the_banner_and_requires_the_acknowledgement():
    ftl = (THEME / "login.ftl").read_text()
    assert "Keycloak 24.0.5" in ftl and "Re-diff" in ftl  # the upstream it must track
    assert 'id="tn-banner"' in ftl and 'msg("tnLoginBanner")' in ftl
    assert re.search(r'<input[^>]*name="tn_ack"[^>]*required', ftl)
    assert 'msg("tnLoginBannerSubmit")' in ftl
    assert "<script" not in ftl.split('id="tn-banner"')[1].split("</form>")[0]  # no JS gate


def test_theme_properties_and_default_messages():
    props = (THEME / "theme.properties").read_text()
    assert "parent=keycloak" in props and "css/tn-banner.css" in props and "css/login.css" in props
    msgs = (THEME / "messages/messages_en.properties").read_text()
    for key in ("tnLoginBannerTitle", "tnLoginBanner", "tnLoginBannerAck", "tnLoginBannerSubmit"):
        assert re.search(rf"^{key}=.+", msgs, re.M), key
    assert "Government of Canada training system" in msgs
    assert (THEME / "resources/css/tn-banner.css").is_file()


def test_owner_text_is_the_installer_default():
    text = " ".join(GROUP_VARS["tn_login_banner_text"].split())
    assert text == (
        "This is a Government of Canada training system. Use is monitored and recorded. "
        "Unauthorized use is prohibited and may result in disciplinary or legal action. "
        "By continuing you consent to monitoring."
    )
    for var in ("tn_login_banner_title", "tn_login_banner_ack"):
        assert GROUP_VARS[var]


def test_compose_mounts_the_theme_read_only():
    for name in ("compose.prod.yml", "compose.dev.yml"):
        compose = yaml.safe_load((ROOT / "infra/platform/docker" / name).read_text())
        vols = compose["services"]["keycloak"]["volumes"]
        mounts = [v for v in vols if "/opt/keycloak/themes/truenorth" in v]
        assert len(mounts) == 1 and mounts[0].endswith(":ro"), name


def test_banner_tasks_write_every_message_idempotently():
    tasks = yaml.safe_load((ROLE / "tasks/banner.yml").read_text())
    put = next(t for t in tasks if t.get("ansible.builtin.uri", {}).get("method") == "PUT")
    assert "/localization/en/" in put["ansible.builtin.uri"]["url"]
    assert put["ansible.builtin.uri"]["headers"]["Content-Type"].startswith("text/plain")
    assert "tn_kc_banner_messages" in put["loop"]
    assert put["when"]  # only when the text differs
    assert put["no_log"] is False  # banner text is not secret
    assert set(DEFAULTS["tn_kc_banner_messages"]) == {"tnLoginBannerTitle", "tnLoginBanner", "tnLoginBannerAck"}
    for value in DEFAULTS["tn_kc_banner_messages"].values():
        assert "replace(\"'\", \"''\")" in value  # MessageFormat-escaped
    main = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    assert any(t.get("ansible.builtin.include_tasks") == "banner.yml" for t in main)


def test_theme_change_restarts_keycloak_once():
    tasks = yaml.safe_load((ROLE / "tasks/banner.yml").read_text())
    record = next(t for t in tasks if t.get("notify"))
    assert record["notify"] == "Restart Keycloak for the login theme"
    handlers = yaml.safe_load((ROLE / "handlers/main.yml").read_text())
    restart = next(h for h in handlers if h["name"] == "Restart Keycloak for the login theme")
    assert restart["community.docker.docker_compose_v2"]["services"] == ["keycloak"]
