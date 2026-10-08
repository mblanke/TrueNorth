"""The installer renders truenorth-web's redirect URIs from tn_domain_fqdn.

infra/keycloak/realm-truenorth.json lists every documented origin (dev, itest, Helm,
the default installer FQDN; tests/api/test_keycloak_realm.py). A site whose
tn_domain_fqdn differs would still get "Invalid parameter: redirect_uri", so
roles/tn_keycloak sets the web client's redirect URIs, web origins and post-logout
URIs from tn_domain_fqdn on every run.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "install/roles/tn_keycloak"


def test_web_client_urls_come_from_tn_domain_fqdn():
    web = yaml.safe_load((ROLE / "defaults/main.yml").read_text())["tn_kc_client_settings"]["truenorth-web"]
    assert web["redirectUris"] == ["https://{{ tn_domain_fqdn }}/*"]
    assert web["webOrigins"] == ["https://{{ tn_domain_fqdn }}"]
    assert web["attributes"] == {"post.logout.redirect.uris": "https://{{ tn_domain_fqdn }}/*"}


def test_the_put_runs_when_any_owned_field_differs():
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    put = next(t for t in tasks if str(t.get("name", "")).startswith("Keycloak — set the clients' redirect URIs"))
    cond = " ".join(put["when"])
    for field in ("redirectUris", "webOrigins", "attributes"):
        assert field in cond, field
