"""Static checks on infra/keycloak/realm-truenorth.json for things Keycloak 24 rejects.

The authoritative check is scripts/check_keycloak_realm.sh (CI job keycloak-realm),
which imports the file into the pinned Keycloak image. These catch the shapes that
broke it before, without Docker, so DoD fails first.
"""

import json
import re
from pathlib import Path

REALM = json.loads((Path(__file__).resolve().parents[2] / "infra/keycloak/realm-truenorth.json").read_text())

# AuthenticationExecutionModel.Requirement in Keycloak >= 8; OPTIONAL was removed.
REQUIREMENTS = {"REQUIRED", "CONDITIONAL", "ALTERNATIVE", "DISABLED"}


def test_flow_executions_are_flat_and_use_current_requirements():
    """A sub-flow is its own entry referenced by flowAlias, never nested inline."""
    for flow in REALM.get("authenticationFlows", []):
        for execution in flow.get("authenticationExecutions", []):
            assert "authenticationExecutions" not in execution, (
                f"flow {flow['alias']!r}: nested executions; define the sub-flow separately"
            )
            assert execution.get("requirement") in REQUIREMENTS, (
                f"flow {flow['alias']!r}: requirement {execution.get('requirement')!r} is not one of {sorted(REQUIREMENTS)}"
            )


def test_no_legacy_user_federation_block():
    """LDAP federation is a component, created by install/roles/tn_keycloak, not this file."""
    assert "userFederationProviders" not in REALM


def test_client_attributes_are_strings():
    for client in REALM.get("clients", []):
        for key, value in (client.get("attributes") or {}).items():
            assert isinstance(value, str), f"client {client['clientId']!r} attribute {key!r} must be a string"


# ── truenorth-web redirect URIs ────────────────────────────────────────
REPO = Path(__file__).resolve().parents[2]


def _web_client() -> dict:
    return next(c for c in REALM["clients"] if c["clientId"] == "truenorth-web")


def _installer_fqdn() -> str:
    """tn_domain_fqdn: the host the installer puts in DOMAIN for compose.prod.yml."""
    text = (REPO / "install/inventory/group_vars/all/main.yml").read_text()
    match = re.search(r"^tn_domain_fqdn:\s*\"?([^\s\"#]+)", text, re.MULTILINE)
    assert match, "tn_domain_fqdn not found in install/inventory/group_vars/all/main.yml"
    return match.group(1)


def _helm_ingress_host() -> str:
    text = (REPO / "infra/k8s/helm/truenorth-range/values.yaml").read_text()
    match = re.search(r"^ingress:\n(?:[ \t].*\n)*?  host:\s*(\S+)", text, re.MULTILINE)
    assert match, "ingress.host not found in the Helm values"
    return match.group(1)


def test_web_client_covers_every_documented_origin():
    """Dev (ng serve / compose :4200), the itest stack (:14200), the Helm ingress host and
    the installer's FQDN. A missing one is "Invalid parameter: redirect_uri" at sign-in."""
    origins = {
        "http://localhost:4200",
        "http://localhost:14200",
        f"https://{_helm_ingress_host()}",
        f"https://{_installer_fqdn()}",
    }
    client = _web_client()
    assert set(client["redirectUris"]) >= {f"{o}/*" for o in origins}
    assert set(client["webOrigins"]) >= origins
    post_logout = set(client["attributes"]["post.logout.redirect.uris"].split("##"))
    assert post_logout >= {f"{o}/*" for o in origins}


def test_web_client_has_no_wildcard_hosts():
    """Keycloak only honours a trailing `*` in a redirect URI; `*` anywhere else is either
    dead (a literal `*.example.com` host) or, as a bare `*`, an open redirect."""
    client = _web_client()
    uris = list(client["redirectUris"]) + client["attributes"]["post.logout.redirect.uris"].split("##")
    for uri in uris:
        head = uri[:-2] if uri.endswith("/*") else uri
        assert "*" not in head, f"truenorth-web redirect URI {uri!r} has a wildcard outside a trailing /*"
        assert re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d+)?", head), f"{uri!r} is not <scheme>://<host>[:port]/*"
    for origin in client["webOrigins"]:
        assert "*" not in origin and origin != "+", f"truenorth-web web origin {origin!r} is a wildcard"
