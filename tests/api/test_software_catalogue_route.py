"""GET /software-catalogue feeds the Range Designer's services autocomplete.

It must report what the worker would install from (the real catalogue file), require
an authenticated user, and fail loudly rather than return an empty list.
"""

from __future__ import annotations

import yaml
from fastapi.routing import APIRoute


def _get(client):
    resp = client.get("/software-catalogue")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_reports_the_real_catalogue(client):
    from app.routers.software_catalogue import _REPO_COPY

    body = _get(client)
    doc = yaml.safe_load(_REPO_COPY.read_text(encoding="utf-8"))
    assert {e["name"] for e in body["software"]} == {k.lower() for k in doc["software"]}
    assert set(body["roles"]) == {r.lower() for r in doc["roles"]}
    names = [e["name"] for e in body["software"]]
    assert names == sorted(names)


def test_os_families_and_aliases(client):
    by_name = {e["name"]: e for e in _get(client)["software"]}
    assert by_name["7zip"]["os_families"] == ["windows", "linux"]
    assert "7-zip" in by_name["7zip"]["aliases"]
    assert by_name["sysinternals"]["os_families"] == ["windows"]
    assert by_name["nginx"]["os_families"] == ["linux"]
    assert "httpd" in by_name["apache2"]["aliases"]


def test_env_override_and_family_detection(client, tmp_path, monkeypatch):
    path = tmp_path / "cat.yaml"
    path.write_text(yaml.safe_dump({
        "roles": ["DNS"],
        "software": {
            "Tool": {"aliases": ["T1"], "windows": {"choco": "tool"}, "linux": {"dnf": ["tool"]}},
            "winonly": {"windows": {"choco": "w"}, "linux": {}},
            "nothing": None,
        },
    }))
    monkeypatch.setenv("TN_SOFTWARE_CATALOGUE", str(path))
    body = _get(client)
    assert body["roles"] == ["dns"]
    assert body["software"] == [
        {"name": "nothing", "aliases": [], "os_families": []},
        {"name": "tool", "aliases": ["t1"], "os_families": ["windows", "linux"]},
        {"name": "winonly", "aliases": [], "os_families": ["windows"]},
    ]


def test_missing_file_is_503_not_empty(client, tmp_path, monkeypatch):
    monkeypatch.setenv("TN_SOFTWARE_CATALOGUE", str(tmp_path / "absent.yaml"))
    assert client.get("/software-catalogue").status_code == 503


def test_malformed_file_is_503(client, tmp_path, monkeypatch):
    path = tmp_path / "bad.yaml"
    path.write_text("roles: [dns]\n")  # no 'software' mapping
    monkeypatch.setenv("TN_SOFTWARE_CATALOGUE", str(path))
    assert client.get("/software-catalogue").status_code == 503


def test_requires_an_authenticated_user():
    from app.auth import get_current_user
    from app.main import app

    route = next(r for r in app.routes if isinstance(r, APIRoute) and r.path == "/software-catalogue")
    assert route.methods == {"GET"}
    assert any(d.call is get_current_user for d in route.dependant.dependencies)
