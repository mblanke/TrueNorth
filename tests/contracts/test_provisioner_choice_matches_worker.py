"""The API's list of provisioner backends must equal the worker's registry.

The API may not import the worker (ADR 0001/0003), so app/provisioner_choice.py names the
backends a range may be created on. If they drift, the API accepts a range no worker can
build, or refuses one it could.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control-plane/worker"))
sys.path.insert(0, str(ROOT / "control-plane/api"))

from app import provisioner_choice  # noqa: E402
from worker import provisioners  # noqa: E402


def test_api_names_every_registered_backend():
    assert set(provisioners._REGISTRY) == provisioner_choice.SUPPORTED | provisioner_choice.EXPERIMENTAL


def test_api_and_worker_agree_on_what_is_experimental():
    assert provisioner_choice.EXPERIMENTAL == provisioners.EXPERIMENTAL
    assert {"mock", "vsphere_api"} == provisioner_choice.SUPPORTED


def test_no_terraform_backend_anywhere():
    assert not [n for n in provisioners._REGISTRY if n.startswith("terraform")]
    assert not (ROOT / "control-plane/worker/worker/provisioners/terraform.py").exists()


def test_installer_preflight_knows_the_same_lists():
    """install/roles/tn_preflight refuses an experimental backend without the flag."""
    text = (ROOT / "install/roles/tn_preflight/tasks/main.yml").read_text()
    assert "tn_experimental_provisioners" in text
    for name in sorted(provisioner_choice.SUPPORTED | provisioner_choice.EXPERIMENTAL):
        assert f"'{name}'" in text, name
