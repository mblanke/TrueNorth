"""The chart configures Keycloak for MFA before the api starts (CI helm-kind, v1.2.0).

Before this, a Helm install against a realm the installer never touched crash-looped: the
api's startup check (TN_MFA_KEYCLOAK_CHECK=refuse under TN_ENV=production) found no
tn-browser flow. The chart's keycloak-mfa pre-install/pre-upgrade hook now runs the
installer's own scripts/keycloak_mfa.py (a byte-identical copy in the chart) with a Keycloak
admin credential only the hook sees, and creates the API's view-realm client. The kind
smoke (infra/k8s/kind/smoke.sh) installs a real Keycloak and keeps the check in refuse mode.
"""

from __future__ import annotations

import re

import pytest
import yaml
from test_helm_chart import CHART, REPO, SECRETS, needs_helm, render, render_error

FILES = CHART / "files/keycloak"


def test_the_chart_ships_the_installers_script_and_spec_unchanged():
    assert (FILES / "keycloak_mfa.py").read_bytes() == (REPO / "scripts/keycloak_mfa.py").read_bytes()
    assert (FILES / "mfa-spec.json").read_bytes() == (REPO / "install/roles/tn_keycloak/files/mfa-spec.json").read_bytes()


def _by_kind_name(docs: list[dict]) -> dict[tuple[str, str], dict]:
    return {(d["kind"], d["metadata"]["name"]): d for d in docs}


@needs_helm
def test_the_hook_runs_after_the_migration_and_before_any_api_pod():
    docs = _by_kind_name(render())
    job = docs[("Job", "tn-truenorth-range-keycloak-mfa")]
    ann = job["metadata"]["annotations"]
    assert ann["helm.sh/hook"] == "pre-install,pre-upgrade"
    migrate = docs[("Job", "tn-truenorth-range-migrate")]["metadata"]["annotations"]
    assert int(ann["helm.sh/hook-weight"]) > int(migrate["helm.sh/hook-weight"])
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["python", "/opt/keycloak-mfa/keycloak_mfa_hook.py"]
    env = {e["name"]: e for e in container["env"]}
    assert env["KC_URL"]["value"] == "http://keycloak:8080/auth"
    assert env["TN_API_ADMIN_CLIENT_ID"]["value"] == "truenorth-api-admin"
    assert "secretKeyRef" in env["KC_ADMIN_PASSWORD"]["valueFrom"]
    cm = docs[("ConfigMap", "tn-truenorth-range-keycloak-mfa")]
    assert cm["data"]["keycloak_mfa.py"].strip() == (REPO / "scripts/keycloak_mfa.py").read_text().strip()
    secret = docs[("Secret", "tn-truenorth-range-keycloak-mfa")]
    assert "hook-succeeded" in secret["metadata"]["annotations"]["helm.sh/hook-delete-policy"]


@needs_helm
def test_the_admin_credential_never_reaches_the_platform_pods():
    docs = render()
    for d in docs:
        if d["kind"] == "Secret" and not d["metadata"]["name"].endswith("keycloak-mfa"):
            assert "KEYCLOAK_ADMIN_PASSWORD" not in (d.get("data") or {}), d["metadata"]["name"]
        if d["kind"] in ("Deployment",):
            text = yaml.safe_dump(d)
            assert "KC_ADMIN_PASSWORD" not in text and "keycloak-mfa" not in text, d["metadata"]["name"]


@needs_helm
def test_the_hook_needs_an_admin_credential_or_must_be_turned_off():
    no_pw = {k: v for k, v in SECRETS.items() if k != "keycloakMfaAdminPassword"}
    assert "secrets.keycloakMfaAdminPassword is required" in render_error(secrets=no_pw)
    docs = render("--set", "keycloakMfa.enabled=false", secrets=no_pw)
    assert not any(d["metadata"]["name"].endswith("keycloak-mfa") for d in docs)


@needs_helm
def test_an_admin_client_needs_its_secret():
    no_client_secret = {k: v for k, v in SECRETS.items() if k != "keycloakAdminClientSecret"}
    assert "secrets.keycloakAdminClientSecret is required" in render_error(secrets=no_client_secret)


@pytest.mark.parametrize("path", ["infra/k8s/kind/values-kind.yaml"])
def test_the_kind_smoke_runs_the_check_in_refuse_mode_against_a_real_keycloak(path):
    values = yaml.safe_load((REPO / path).read_text())
    assert values["config"]["keycloak"]["url"] == "http://keycloak:8080/auth"
    assert values["config"]["keycloak"]["adminClientId"] == "truenorth-api-admin"
    assert values["config"].get("mfaKeycloakCheck", "") in ("", "refuse")
    deps = (REPO / "infra/k8s/kind/deps.yaml").read_text()
    pin = re.search(r"quay.io/keycloak/keycloak:[^\s\"]+",
                    (REPO / "infra/platform/docker/compose.prod.yml").read_text()).group(0)
    assert f"image: {pin}" in deps
    smoke = (REPO / "infra/k8s/kind/smoke.sh").read_text()
    assert "truenorth-range-keycloak-mfa" in smoke and "MFA: Keycloak realm configured" in smoke
    assert "secrets.keycloakMfaAdminPassword" in smoke


def test_the_hook_runner_reads_the_token_from_its_own_request_not_argv():
    runner = (FILES / "keycloak_mfa_hook.py").read_text()
    assert "sys.stdin = io.StringIO(token" in runner
    assert not re.search(r"argv[^\n]*token", runner)
