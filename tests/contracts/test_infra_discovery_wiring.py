"""INFRA_DISCOVERY_SECONDS reaches the api from the installer and compose (app/infra_discovery).

The scheduled rediscovery runs in the api (the discovery backends and sealed connection
passwords are the api's), so the setting is the api service's, with the same default (300)
everywhere it is written down.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "infra/platform/docker"
INSTALL = ROOT / "install"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!reset", lambda loader, node: None)
_Loader.add_constructor("!override", lambda loader, node: None)


def test_compose_gives_the_api_the_setting_with_its_default():
    services = yaml.load((DOCKER / "compose.prod.yml").read_text(), Loader=_Loader)["services"]  # noqa: S506
    assert services["api"]["environment"]["INFRA_DISCOVERY_SECONDS"] == "${INFRA_DISCOVERY_SECONDS:-300}"


def test_installer_renders_it_from_a_documented_default():
    defaults = yaml.safe_load((INSTALL / "inventory/group_vars/all/main.yml").read_text())
    assert defaults["tn_infra_discovery_seconds"] == 300
    template = (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()
    assert re.search(r"^INFRA_DISCOVERY_SECONDS=\{\{ tn_infra_discovery_seconds \| default\(300\) \}\}$", template, re.M)
    readme = (INSTALL / "README.md").read_text()
    assert "`tn_infra_discovery_seconds` | `300`" in readme


def test_the_example_env_file_lists_it():
    assert re.search(r"^INFRA_DISCOVERY_SECONDS=300$", (DOCKER / ".env.production.example").read_text(), re.M)


def test_the_api_default_matches():
    import sys

    sys.path.insert(0, str(ROOT / "control-plane/api"))
    from app.infra_discovery.service import DEFAULT_INTERVAL

    assert DEFAULT_INTERVAL == 300
