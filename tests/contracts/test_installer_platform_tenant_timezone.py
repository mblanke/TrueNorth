"""PLATFORM_TENANT_ID and SITE_TIMEZONE reach the api from the installer, compose and Helm (#318).

Before, the installer never set PLATFORM_TENANT_ID (#273): a second tenant left nobody the
platform administrator. Now ``tn_platform_tenant_id`` defaults to the bootstrap tenant
(80-seed records its id; 30-config renders it). SITE_TIMEZONE ("next free at" was always
UTC) comes from ``tn_site_timezone``, else the host's zone, else UTC.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "infra/platform/docker"
INSTALL = ROOT / "install"
HELM = ROOT / "infra/k8s/helm/truenorth-range"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!reset", lambda loader, node: None)
_Loader.add_constructor("!override", lambda loader, node: None)


def _api_env() -> dict:
    services = yaml.load((DOCKER / "compose.prod.yml").read_text(), Loader=_Loader)["services"]  # noqa: S506
    return services["api"]["environment"]


def test_compose_passes_both_to_the_api():
    env = _api_env()
    assert env["PLATFORM_TENANT_ID"] == "${PLATFORM_TENANT_ID:-}"
    assert env["SITE_TIMEZONE"] == "${SITE_TIMEZONE:-UTC}"


def test_installer_defaults_are_empty_and_documented():
    defaults = yaml.safe_load((INSTALL / "inventory/group_vars/all/main.yml").read_text())
    assert defaults["tn_platform_tenant_id"] == ""
    assert defaults["tn_site_timezone"] == ""
    readme = (INSTALL / "README.md").read_text()
    assert "| `tn_platform_tenant_id` | *(empty: the bootstrap tenant)*" in readme
    assert "| `tn_site_timezone` | *(empty: the host's zone, else UTC)*" in readme


def test_env_template_renders_both():
    template = (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()
    assert re.search(r"^PLATFORM_TENANT_ID=\{\{ tn_platform_tenant_id_effective \| default\(''\) \}\}$", template, re.M)
    assert re.search(r"^SITE_TIMEZONE=\{\{ tn_site_timezone_effective \| default\('UTC'\) \}\}$", template, re.M)


def test_env_tasks_resolve_the_bootstrap_tenant_and_the_host_zone():
    tasks = yaml.safe_load((INSTALL / "roles/tn_config/tasks/env.yml").read_text())
    by_name = {t["name"]: t for t in tasks}
    slurp = by_name["Config — read the recorded platform tenant id"]["ansible.builtin.slurp"]
    assert slurp["src"] == "{{ tn_state_dir }}/platform_tenant_id"
    platform = by_name["Config — the platform tenant"]["ansible.builtin.set_fact"]["tn_platform_tenant_id_effective"]
    assert "tn_platform_tenant_id" in platform and "tn_platform_tenant_state" in platform
    assert by_name["Config — the host's time zone"]["ansible.builtin.command"]["argv"][0] == "timedatectl"
    zone = by_name["Config — the site time zone"]["ansible.builtin.set_fact"]["tn_site_timezone_effective"]
    assert "tn_site_timezone" in zone and "'UTC'" in zone
    # Both are set before the file is rendered.
    names = [t["name"] for t in tasks]
    assert names.index("Config — the site time zone") < names.index("Config — render .env.production")
    assert names.index("Config — the platform tenant") < names.index("Config — render .env.production")


def test_seed_records_the_bootstrap_tenant_and_restarts_the_api_with_it():
    tasks = yaml.safe_load((INSTALL / "roles/tn_seed/tasks/main.yml").read_text())
    names = [t["name"] for t in tasks]
    bootstrap = names.index("Seed — create the tenant and bootstrap admin")
    lookup = names.index("Seed — the bootstrap tenant's id")
    record = names.index("Seed — record it as the platform tenant")
    patch = names.index("Seed — put the platform tenant in the environment")
    restart = names.index("Seed — the api runs with the platform tenant")
    assert bootstrap < lookup < record < patch < restart
    by_name = {t["name"]: t for t in tasks}
    assert "WHERE slug = " in by_name["Seed — the bootstrap tenant's id"]["ansible.builtin.command"]["cmd"]
    assert by_name["Seed — record it as the platform tenant"]["ansible.builtin.copy"]["dest"] == (
        "{{ tn_state_dir }}/platform_tenant_id"
    )
    assert by_name["Seed — put the platform tenant in the environment"]["ansible.builtin.lineinfile"]["regexp"] == (
        "^PLATFORM_TENANT_ID="
    )
    assert by_name["Seed — the api runs with the platform tenant"]["community.docker.docker_compose_v2"]["services"] == [
        "api"
    ]


def test_preflight_checks_their_form():
    text = (INSTALL / "roles/tn_preflight/tasks/main.yml").read_text()
    assert "Preflight — platform tenant and site time zone are well formed" in text


def test_the_example_env_file_lists_both():
    example = (DOCKER / ".env.production.example").read_text()
    assert re.search(r"^PLATFORM_TENANT_ID=$", example, re.M)
    assert re.search(r"^SITE_TIMEZONE=UTC$", example, re.M)


def test_helm_renders_both_when_set():
    values = yaml.safe_load((HELM / "values.yaml").read_text())
    assert values["config"]["platformTenantId"] == ""
    assert values["config"]["siteTimezone"] == ""
    configmap = (HELM / "templates/configmap.yaml").read_text()
    assert "PLATFORM_TENANT_ID: {{ . | quote }}" in configmap
    assert "SITE_TIMEZONE: {{ . | quote }}" in configmap
