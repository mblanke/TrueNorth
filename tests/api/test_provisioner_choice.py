"""A range is created only on a backend that may build it (app/provisioner_choice.py).

proxmox_api and hyperv are experimental: refused unless EXPERIMENTAL_PROVISIONERS is true.
The Terraform backends are gone. The refusal names the fix and leaves no range row.
"""

from __future__ import annotations

import pytest
from app import provisioner_choice
from app.models import Range


def _template(client) -> str:
    return client.post(
        "/templates", json={"name": "pc-tmpl", "version": "1.0", "yaml": "assets: []", "is_public": True}
    ).json()["id"]


@pytest.mark.parametrize("name", ["proxmox_api", "hyperv"])
def test_experimental_backend_refused_without_flag(client, db_session, monkeypatch, name):
    tid = _template(client)
    monkeypatch.setenv("PROVISIONER_BACKEND", name)
    monkeypatch.delenv("EXPERIMENTAL_PROVISIONERS", raising=False)
    before = db_session.query(Range).count()
    resp = client.post("/ranges", json={"name": "r-exp", "template_id": tid})
    assert resp.status_code == 409
    assert "experimental" in resp.json()["detail"] and "EXPERIMENTAL_PROVISIONERS=true" in resp.json()["detail"]
    assert db_session.query(Range).count() == before


@pytest.mark.parametrize("name", ["proxmox_api", "hyperv"])
def test_experimental_backend_allowed_with_flag(client, monkeypatch, name):
    tid = _template(client)
    monkeypatch.setenv("PROVISIONER_BACKEND", name)
    monkeypatch.setenv("EXPERIMENTAL_PROVISIONERS", "true")
    resp = client.post("/ranges", json={"name": "r-exp", "template_id": tid})
    assert resp.status_code == 201, resp.text
    assert resp.json()["provisioner_backend"] == name


@pytest.mark.parametrize("name", ["terraform", "terraform_vsphere", "vsphere"])
def test_unknown_backend_refused(client, monkeypatch, name):
    tid = _template(client)
    monkeypatch.setenv("PROVISIONER_BACKEND", name)
    resp = client.post("/ranges", json={"name": "r-x", "template_id": tid})
    assert resp.status_code == 409
    assert "not a provisioner backend" in resp.json()["detail"]


@pytest.mark.parametrize("name", ["mock", "vsphere_api"])
def test_supported_backends_need_no_flag(monkeypatch, name):
    monkeypatch.setenv("PROVISIONER_BACKEND", name)
    monkeypatch.delenv("EXPERIMENTAL_PROVISIONERS", raising=False)
    assert provisioner_choice.configured_backend() == name


def test_flag_values(monkeypatch):
    for value, on in (("true", True), ("1", True), ("YES", True), ("false", False), ("", False), ("0", False)):
        monkeypatch.setenv("EXPERIMENTAL_PROVISIONERS", value)
        assert provisioner_choice.experimental_enabled() is on, value
