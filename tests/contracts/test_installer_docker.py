"""Installer contracts for the container runtime role (install/roles/tn_docker).

The installer used to assume Docker was already on the platform host. 00-docker now
installs it on Ubuntu 24.04: from Docker's own apt repository with a pinned signing-key
fingerprint, at pinned and held versions, with log rotation and the containerd image
store the air-gapped digest check depends on. Each test pins one of those properties.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install"
ROLE = INSTALL / "roles/tn_docker"
DOCKER_KEY_FINGERPRINT = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
DOCKER_PACKAGES = {"docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"}


def _yaml(path: Path):
    return yaml.safe_load(path.read_text())


def _group_vars() -> dict:
    return _yaml(INSTALL / "inventory/group_vars/all/main.yml")


def _defaults() -> dict:
    return _yaml(ROLE / "defaults/main.yml")


def _tasks() -> list[dict]:
    """Every task in the role, blocks flattened."""

    def walk(items):
        for t in items or []:
            yield t
            for section in ("block", "rescue", "always"):
                yield from walk(t.get(section))

    out: list[dict] = []
    for f in sorted((ROLE / "tasks").glob("*.yml")):
        out.extend(walk(_yaml(f)))
    return out


def _module(task: dict, name: str):
    return task.get(f"ansible.builtin.{name}")


def test_docker_role_runs_before_preflight_in_site_yml():
    imports = [entry["import_playbook"] for entry in _yaml(INSTALL / "site.yml")]
    assert "playbooks/00-docker.yml" in imports
    assert imports.index("playbooks/00-docker.yml") < imports.index("playbooks/00-preflight.yml")
    play = _yaml(INSTALL / "playbooks/00-docker.yml")[0]
    assert [r["role"] for r in play["roles"]] == ["tn_docker"]


def test_opt_out_variable_exists_and_gates_the_whole_role():
    gv = _group_vars()
    default = gv["tn_install_docker"]
    assert "Ubuntu" in default and "24.04" in default, "default on only for Ubuntu 24.04"
    assert gv["tn_docker_manage_daemon_json"] is True
    main = _yaml(ROLE / "tasks/main.yml")
    include = next(t for t in main if "ansible.builtin.include_tasks" in t)
    assert include["ansible.builtin.include_tasks"] == "install.yml"
    assert include["when"] == "tn_install_docker | bool"
    # Nothing else in main.yml changes the host.
    assert all("ansible.builtin.debug" in t or t is include for t in main)


def test_apt_key_fingerprint_is_pinned_and_checked():
    d = _defaults()
    assert d["tn_docker_apt_key_fingerprint"] == DOCKER_KEY_FINGERPRINT
    assert d["tn_docker_apt_keyring"].startswith("/etc/apt/keyrings/")
    tasks = _tasks()
    gpg = next(
        t for t in tasks if isinstance(_module(t, "command"), dict) and _module(t, "command")["argv"][0] == "gpg"
    )
    assert "--show-keys" in gpg["ansible.builtin.command"]["argv"]
    assert any(
        "tn_docker_apt_key_fingerprint" in " ".join(_module(t, "assert")["that"])
        for t in tasks
        if _module(t, "assert") and isinstance(_module(t, "assert")["that"], list)
    )
    # An unverified key is deleted, not left for apt to trust.
    block = next(t for t in tasks if "rescue" in t and any("get_url" in str(x) for x in t["block"]))
    assert any((_module(r, "file") or {}).get("state") == "absent" for r in block["rescue"])
    # The apt source trusts only that keyring.
    source = next(t for t in tasks if (_module(t, "copy") or {}).get("dest") == "{{ tn_docker_apt_source }}")
    assert "Signed-By: {{ tn_docker_apt_keyring }}" in source["ansible.builtin.copy"]["content"]


def test_versions_are_pinned_through_variables():
    gv = _group_vars()
    for var in ("tn_docker_version", "tn_compose_version", "tn_containerd_version", "tn_buildx_version"):
        assert re.fullmatch(r"\d+\.\d+\.\d+", gv[var]), f"{var} must be an exact version"
    pkgs = _defaults()["tn_docker_packages"]
    assert set(pkgs) == DOCKER_PACKAGES
    assert pkgs["docker-ce"] == pkgs["docker-ce-cli"] == "5:{{ tn_docker_version }}"
    assert pkgs["docker-compose-plugin"] == "{{ tn_compose_version }}"
    assert pkgs["containerd.io"] == "{{ tn_containerd_version }}"
    assert pkgs["docker-buildx-plugin"] == "{{ tn_buildx_version }}"
    install = next(t for t in _tasks() if "tn_docker_packages.items()" in str((_module(t, "apt") or {}).get("name")))
    apt = install["ansible.builtin.apt"]
    assert apt["install_recommends"] is False
    assert apt["allow_downgrade"] is False


def test_pinned_versions_meet_the_preflight_floor():
    gv = _group_vars()

    def v(s):
        return tuple(int(x) for x in s.split("."))

    assert v(gv["tn_docker_version"]) >= v(gv["tn_min_docker_version"])
    assert v(gv["tn_compose_version"]) >= v(gv["tn_min_compose_version"])


def test_packages_are_held():
    hold = next(t for t in _tasks() if _module(t, "dpkg_selections"))
    assert hold["ansible.builtin.dpkg_selections"]["selection"] == "hold"
    assert hold["loop"] == "{{ tn_docker_packages.keys() | list }}"
    # A version bump has to move held packages.
    install = next(t for t in _tasks() if "tn_docker_packages.items()" in str((_module(t, "apt") or {}).get("name")))
    assert install["ansible.builtin.apt"]["allow_change_held_packages"] is True


def test_daemon_json_rotates_logs_and_uses_the_containerd_store():
    settings = _defaults()["tn_docker_daemon_settings"]
    assert settings["log-driver"] == "json-file"
    assert settings["log-opts"] == {"max-size": "50m", "max-file": "5"}
    assert settings["features"] == {"containerd-snapshotter": True}
    json.dumps(settings)  # what the role writes must be JSON
    tasks = _tasks()
    block = next(t for t in tasks if t.get("when") == "tn_docker_manage_daemon_json | bool")
    write = next(t for t in block["block"] if (_module(t, "copy") or {}).get("dest") == "/etc/docker/daemon.json")
    copy = write["ansible.builtin.copy"]
    assert "combine(tn_docker_daemon_settings" in copy["content"], "merged over the operator's keys"
    assert copy["backup"] is True
    assert write["notify"] == "Restart docker"
    handlers = _yaml(ROLE / "handlers/main.yml")
    assert any(h["name"] == "Restart docker" for h in handlers)


def test_chrony_and_docker_group_with_connection_reset():
    tasks = _tasks()
    assert any((_module(t, "apt") or {}).get("name") == "chrony" for t in tasks)
    user = next(t for t in tasks if _module(t, "user"))
    assert user["ansible.builtin.user"]["groups"] == "docker"
    assert user["ansible.builtin.user"]["append"] is True
    names = [t.get("ansible.builtin.meta") for t in tasks]
    assert "reset_connection" in names
    assert names.index("reset_connection") > tasks.index(user)


def test_preflight_compose_floor_is_2_24():
    gv = _group_vars()
    assert gv["tn_min_compose_version"] == "2.24.0"
    # Preflight's registers must not shadow the pinned versions for the rest of the run.
    preflight = (INSTALL / "roles/tn_preflight/tasks/main.yml").read_text()
    assert "register: tn_docker_version" not in preflight
    assert "register: tn_compose_version" not in preflight
