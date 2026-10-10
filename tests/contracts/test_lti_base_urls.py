"""Gap #6 (docs/moodle-integration.md): production gives the api its public LTI URLs, and
everything that tells a learning platform where TrueNorth is says the same thing.

- installer: group_vars tn_lti_tool_base_url / tn_lti_web_base_url, rendered into
  .env.production (tn_config), which compose reads (--env-file) and passes to the api;
- the Moodle role registers the tool at the same base (tn_moodle_tool_url);
- helm: the configmap the api reads sets both from the chart's public origin;
- the edge strips /api, so <tool base>/lti/... reaches the API's /lti/... routes, which
  are what GET /integrations/lti/tool-config and Moodle's registration name.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"
DOCKER = ROOT / "infra/platform/docker"
FQDN = "tn.example.test"


def _group_vars() -> dict:
    return yaml.safe_load((INSTALL / "inventory/group_vars/all/main.yml").read_text())


def _resolve(value: str, ctx: dict) -> str:
    """Render a group_vars string the way Ansible would (one level is enough here)."""
    return jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(value).render(**ctx)


@pytest.fixture
def ctx() -> dict:
    gv = _group_vars()
    base = {"tn_domain_fqdn": FQDN}
    return {
        "tn_domain_fqdn": FQDN,
        "tn_lti_tool_base_url": _resolve(gv["tn_lti_tool_base_url"], base),
        "tn_lti_web_base_url": _resolve(gv["tn_lti_web_base_url"], base),
    }


def test_group_vars_name_the_public_api_and_web_origins(ctx):
    assert ctx["tn_lti_tool_base_url"] == f"https://{FQDN}/api"
    assert ctx["tn_lti_web_base_url"] == f"https://{FQDN}"


def test_the_installer_renders_both_into_the_api_env_file(ctx):
    # Only the LTI lines: the rest of the template needs the whole Ansible run's facts.
    template = (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()
    lti = "\n".join(line for line in template.splitlines() if line.startswith("LTI_"))
    env = jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(lti).render(**ctx)
    lines = dict(line.split("=", 1) for line in env.splitlines())
    assert set(lines) == {"LTI_TOOL_BASE_URL", "LTI_WEB_BASE_URL"}
    assert lines["LTI_TOOL_BASE_URL"] == f"https://{FQDN}/api"
    assert lines["LTI_WEB_BASE_URL"] == f"https://{FQDN}"


def test_compose_hands_the_env_file_to_the_api():
    # The installer runs compose with the rendered file as its env file ...
    gv = _group_vars()
    assert gv["tn_env_file"].endswith("/.env.production")
    assert "--env-file {{ tn_env_file }}" in (INSTALL / "roles/tn_compose/tasks/main.yml").read_text()
    # ... and compose passes both to the api service.

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_constructor("!reset", lambda loader, node: None)
    _Loader.add_constructor("!override", lambda loader, node: None)
    api = yaml.load((DOCKER / "compose.prod.yml").read_text(), Loader=_Loader)["services"]["api"]  # noqa: S506
    env = api["environment"]
    assert env["LTI_TOOL_BASE_URL"].startswith("${LTI_TOOL_BASE_URL")
    assert env["LTI_WEB_BASE_URL"].startswith("${LTI_WEB_BASE_URL")


def test_the_moodle_role_registers_the_same_tool_base(ctx):
    defaults = yaml.safe_load((INSTALL / "roles/tn_moodle/defaults/main.yml").read_text())
    assert defaults["tn_moodle_tool_url"] == "{{ tn_lti_tool_base_url }}"
    assert "TN_TOOL_URL={{ tn_moodle_tool_url }}" in (INSTALL / "roles/tn_moodle/templates/moodle.env.j2").read_text()
    # The node registers <tool>/lti/launch and <tool>/lti/login (bootstrap/truenorth_lti_tool.php).
    php = (ROOT / "infra/platform/moodle/bootstrap/truenorth_lti_tool.php").read_text()
    assert '$launch = "$tool/lti/launch";' in php and '"$tool/lti/login"' in php


def test_the_edge_strips_api_so_the_tool_urls_reach_the_routes():
    conf = (ROOT / "infra/platform/nginx/conf.d/truenorth.conf").read_text()
    block = conf[conf.index("location /api/ {"):]
    assert re.search(r"rewrite \^/api/\(\.\*\) /\$1 break;", block[:600])


def test_the_tool_config_and_the_registration_name_real_routes(client, monkeypatch):
    from app import lti13

    monkeypatch.setattr(lti13, "TOOL_BASE_URL", f"https://{FQDN}/api")
    body = client.get("/integrations/lti/tool-config").json()
    paths = set(client.get("/openapi.json").json()["paths"])
    for url in (body["tool_url"], body["initiate_login_url"], body["public_keyset_url"]):
        assert url.startswith(f"https://{FQDN}/api/") and url.removeprefix(f"https://{FQDN}/api") in paths
    # What Moodle registers: <tool>/lti/launch and <tool>/lti/login, the same two URLs.
    assert body["tool_url"] == f"https://{FQDN}/api/lti/launch"
    assert body["initiate_login_url"] == f"https://{FQDN}/api/lti/login"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_the_chart_sets_both_from_its_public_origin():
    from test_helm_chart import render

    cms = [d for d in render() if d.get("kind") == "ConfigMap" and "LTI_TOOL_BASE_URL" in (d.get("data") or {})]
    assert cms, "no ConfigMap carries LTI_TOOL_BASE_URL"
    data = cms[0]["data"]
    origin = data["LTI_WEB_BASE_URL"]
    assert origin.startswith("https://")
    assert data["LTI_TOOL_BASE_URL"] == f"{origin}/api"
