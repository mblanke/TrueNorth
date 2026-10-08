"""Production OpenSearch runs its security plugin; only dev/itest and an explicit override don't.

compose.prod.yml once defaulted the plugin off ("every client talks plain http"). These
hold the production defaults (plugin on, no demo config, TLS + auth on every client, the
CA mounted where OPENSEARCH_VERIFY_SSL points), the installer's matching defaults, and the
Helm chart's credentials. No Docker or cluster needed.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "infra/platform/docker"
INSTALL = ROOT / "install"
HELM = ROOT / "infra/k8s/helm/truenorth-range"
CA_IN_CONTAINER = "/etc/truenorth/opensearch-ca.pem"


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that accepts compose's merge tags (!reset, !override) as plain values."""


for _tag in ("!reset", "!override"):
    _ComposeLoader.add_constructor(
        _tag, lambda loader, node: loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node) if isinstance(node, yaml.MappingNode) else loader.construct_scalar(node))


def _compose(name: str) -> dict:
    return yaml.load((DOCKER / name).read_text(), Loader=_ComposeLoader)["services"]  # noqa: S506 - SafeLoader subclass


def _env(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        return dict(item.split("=", 1) for item in env)
    return {k: str(v) for k, v in env.items()}


def _default(value: str) -> str:
    """The default of a ${VAR:-default} / ${VAR-default} interpolation, else the value."""
    m = re.fullmatch(r"\$\{\w+:?-(.*)\}", value)
    return m.group(1) if m else value


def test_prod_opensearch_runs_its_security_plugin_without_demo_config():
    env = _env(_compose("compose.prod.yml")["opensearch"])
    assert _default(env["DISABLE_SECURITY_PLUGIN"]) == "false"
    assert env["DISABLE_INSTALL_DEMO_CONFIG"] == "true"
    assert env["plugins.security.ssl.http.enabled"] == "true"
    # -E list settings split on commas: the DNs must be single-RDN.
    assert "," not in env["plugins.security.nodes_dn"] and "," not in env["plugins.security.authcz.admin_dn"]
    mounts = " ".join(_compose("compose.prod.yml")["opensearch"]["volumes"])
    assert "/usr/share/opensearch/config/certs" in mounts and "internal_users.yml" in mounts


def test_every_prod_client_uses_tls_auth_and_the_mounted_ca():
    services = _compose("compose.prod.yml")
    for name in ("api", "worker-provision", "worker-scenario", "worker-telemetry"):
        env = _env(services[name])
        assert _default(env["OPENSEARCH_URL"]).startswith("https://"), name
        assert _default(env["OPENSEARCH_USER"]) == "admin", name
        assert "OPENSEARCH_PASS" in env, name
        assert _default(env["OPENSEARCH_VERIFY_SSL"]) == CA_IN_CONTAINER, name
        assert any(v.endswith(f":{CA_IN_CONTAINER}:ro") for v in services[name].get("volumes", [])), name
    dash = _env(services["opensearch-dashboards"])
    assert _default(dash["DISABLE_SECURITY_DASHBOARDS_PLUGIN"]) == "false"
    assert dash["OPENSEARCH_USERNAME"] == "kibanaserver" and dash["OPENSEARCH_SSL_VERIFICATIONMODE"] == "full"


def test_dev_and_itest_stay_convenient():
    for name in ("compose.dev.yml", "compose.itest.yml"):
        assert _env(_compose(name)["opensearch"])["DISABLE_SECURITY_PLUGIN"] == "true", name


def test_installer_defaults_to_security_on_with_generated_passwords():
    group_vars = yaml.safe_load((INSTALL / "inventory/group_vars/all/main.yml").read_text())
    assert group_vars["tn_opensearch_disable_security"] is False
    secrets = yaml.safe_load((INSTALL / "roles/tn_config/defaults/main.yml").read_text())
    names = {s["name"] for s in secrets["tn_generated_secrets"]}
    assert {"opensearch_admin_password", "opensearch_dashboards_password"} <= names
    env = (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()
    on = env.split("{% else %}", 1)[1].split("{% endif %}", 1)[0]
    assert "OPENSEARCH_DISABLE_SECURITY=false" in on and "OPENSEARCH_URL=https://" in on
    assert f"OPENSEARCH_VERIFY_SSL={CA_IN_CONTAINER}" in env
    tasks = (INSTALL / "roles/tn_tls/tasks/main.yml").read_text()
    assert "include_tasks: opensearch.yml" in tasks


def test_helm_requires_opensearch_credentials():
    values = yaml.safe_load((HELM / "values.yaml").read_text())["opensearch"]
    assert values["password"] == "" and values["securityDisabled"] is False and values["verifySSL"] is True
    secret = (HELM / "templates/secret.yaml").read_text()
    assert 'required "opensearch.password is required' in secret
    assert "OPENSEARCH_VERIFY_SSL" in (HELM / "templates/configmap.yaml").read_text()
