"""Noise deploy inputs cannot steer Ansible (security sweep M5).

Node ids went unchecked into INI inventory lines and host_vars file names, and the
controller URL into extra-vars that Ansible renders as Jinja templates.
"""

from __future__ import annotations

import copy

import pytest
import yaml

noise_tasks = pytest.importorskip("worker.noise_tasks")

GOOD = {
    "range_id": "4f7c2a1e-0000-4000-8000-000000000001",
    "controller_url": "https://10.255.0.1:8443/api",
    "mgmt_cidr": "10.255.0.0/24",
    "domain": "corp.local",
    "agents": [{"node": "lnx-01", "mgmt_ip": "10.255.0.10", "token": "A" * 43}],
}


def _with(path: str, value):
    inv = copy.deepcopy(GOOD)
    if path.startswith("agents."):
        inv["agents"][0][path.split(".", 1)[1]] = value
    else:
        inv[path] = value
    return inv


def test_a_well_formed_inventory_passes():
    assert noise_tasks.validate_inventory(copy.deepcopy(GOOD))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("agents.node", "lnx01 ansible_connection=local"),  # INI injection
        ("agents.node", "../../etc/cron.d/x"),  # host_vars path traversal
        ("agents.node", "LNX01"),
        ("agents.node", "-lnx"),
        ("agents.node", "a" * 64),
        ("agents.node", "lnx01\nevil ansible_host=1.2.3.4"),
        ("agents.mgmt_ip", "10.255.0.10 ansible_user=root"),
        ("agents.token", "{{ lookup('pipe', 'id') }}"),
        ("controller_url", "https://x/{{ lookup('pipe', 'id') }}"),
        ("controller_url", "http://10.255.0.1/api"),
        ("controller_url", "https://user:pw@10.255.0.1/api"),
        ("controller_url", "https://10.255.0.1/api?x={{y}}"),
        ("controller_url", "https://bad host/api"),
        ("mgmt_cidr", "not-a-cidr"),
        ("domain", "corp.local {{ x }}"),
        ("range_id", "r1; rm -rf /"),
    ],
)
def test_anything_else_is_refused(path, value):
    with pytest.raises(noise_tasks.InventoryError):
        noise_tasks.validate_inventory(_with(path, value))


def test_the_refusal_does_not_echo_the_value():
    with pytest.raises(noise_tasks.InventoryError) as exc:
        noise_tasks.validate_inventory(_with("agents.token", "{{ secret-looking-value }}"))
    assert "secret-looking" not in str(exc.value)


def test_the_task_refuses_before_any_file_or_ansible(monkeypatch, tmp_path):
    monkeypatch.setenv("NOISE_DEPLOY_MODE", "ansible")
    monkeypatch.setattr(noise_tasks.shutil, "which", lambda _: "/usr/bin/ansible-playbook")
    ran = []
    monkeypatch.setattr(noise_tasks.subprocess, "run", lambda *a, **k: ran.append(a))
    out = noise_tasks.deploy_noise_agents.run(_with("agents.node", "x ansible_connection=local"))
    assert out["status"] == "failed" and "invalid inventory" in out["error"] and ran == []


def test_extra_vars_are_unsafe_strings_ansible_will_not_template():
    text = noise_tasks.extra_vars_yaml({"noise_controller_url": "https://h/{{ x }}", "range_id": "r-1"})

    class UnsafeLoader(yaml.SafeLoader):
        pass

    UnsafeLoader.add_constructor("!unsafe", lambda loader, node: ("UNSAFE", loader.construct_scalar(node)))
    parsed = yaml.load(text, Loader=UnsafeLoader)  # noqa: S506 - SafeLoader subclass
    assert parsed == {"noise_controller_url": ("UNSAFE", "https://h/{{ x }}"), "range_id": ("UNSAFE", "r-1")}


def test_inventory_lines_are_only_validated_values(tmp_path):
    inv = noise_tasks.write_inventory(tmp_path, copy.deepcopy(GOOD))
    assert inv.read_text() == "[noise_agents]\nlnx-01 ansible_host=10.255.0.10\n"
