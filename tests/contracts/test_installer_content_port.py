"""The content port's trusted keys reach the api, and are empty (nothing imported) on a
fresh install (docs/content-port.md)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "install/inventory/group_vars/all/main.yml"
ENV = ROOT / "install/roles/tn_config/templates/env.production.j2"
COMPOSE = ROOT / "infra/platform/docker/compose.prod.yml"
EXAMPLE = ROOT / "infra/platform/docker/.env.production.example"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!reset", lambda loader, node: None)
_Loader.add_constructor("!override", lambda loader, node: None)


def test_no_key_is_trusted_on_a_fresh_install():
    assert yaml.safe_load(MAIN.read_text())["tn_content_port_trusted_keys"] == ""
    assert re.search(r"^TN_CONTENT_PORT_TRUSTED_KEYS=$", EXAMPLE.read_text(), re.M)


def test_the_env_file_renders_it_and_the_api_gets_it():
    assert re.search(r"^TN_CONTENT_PORT_TRUSTED_KEYS=\{\{ tn_content_port_trusted_keys \|", ENV.read_text(), re.M)
    services = yaml.load(COMPOSE.read_text(), Loader=_Loader)["services"]  # noqa: S506
    assert services["api"]["environment"]["TN_CONTENT_PORT_TRUSTED_KEYS"] == "${TN_CONTENT_PORT_TRUSTED_KEYS:-}"
