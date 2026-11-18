"""CLASS_OBSERVER_CONSOLES reaches the api from the installer and compose (owner decision 2026-10-28).

Observer consoles are off by default; observers get live status only. The API reads
1/true/yes/on as on (app/classes/consoles.py). ``tn_class_observer_consoles`` (default false)
is how a site turns them on without hand-editing the env file.
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


def test_compose_passes_it_to_the_api_defaulting_off():
    services = yaml.load((DOCKER / "compose.prod.yml").read_text(), Loader=_Loader)["services"]  # noqa: S506
    assert services["api"]["environment"]["CLASS_OBSERVER_CONSOLES"] == "${CLASS_OBSERVER_CONSOLES:-false}"


def test_installer_default_is_off_and_documented():
    defaults = yaml.safe_load((INSTALL / "inventory/group_vars/all/main.yml").read_text())
    assert defaults["tn_class_observer_consoles"] is False
    readme = (INSTALL / "README.md").read_text()
    assert "| `tn_class_observer_consoles` | `false` |" in readme


def test_env_template_renders_a_value_the_api_reads():
    template = (INSTALL / "roles/tn_config/templates/env.production.j2").read_text()
    assert re.search(
        r"^CLASS_OBSERVER_CONSOLES=\{\{ 'true' if tn_class_observer_consoles \| default\(false\) \| bool else 'false' \}\}$",
        template,
        re.M,
    )


def test_the_rendered_values_are_what_the_api_accepts():
    source = (ROOT / "control-plane/api/app/classes/consoles.py").read_text()
    assert 'OBSERVER_CONSOLES_ENV = "CLASS_OBSERVER_CONSOLES"' in source
    accepted = re.search(r'os\.getenv\(OBSERVER_CONSOLES_ENV, ""\)\.strip\(\)\.lower\(\) in \(([^)]*)\)', source)
    assert accepted, "the API's accepted values moved: update the env template and this test"
    values = re.findall(r'"([^"]+)"', accepted.group(1))
    assert "true" in values and "false" not in values


def test_preflight_refuses_a_non_boolean():
    tasks = yaml.safe_load((INSTALL / "roles/tn_preflight/tasks/main.yml").read_text())
    by_name = {t["name"]: t for t in tasks}
    that = by_name["Preflight — tn_class_observer_consoles is a boolean"]["ansible.builtin.assert"]["that"]
    assert "['true', 'false']" in str(that)


def test_the_example_env_file_lists_it_off():
    example = (DOCKER / ".env.production.example").read_text()
    assert re.search(r"^CLASS_OBSERVER_CONSOLES=false$", example, re.M)
