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
        {"name": "nothing", "aliases": [], "os_families": [], "offline": True},
        {"name": "tool", "aliases": ["t1"], "os_families": ["windows", "linux"], "offline": False},
        {"name": "winonly", "aliases": [], "os_families": ["windows"], "offline": False},
    ]


def test_offline_flag(client, tmp_path, monkeypatch):
    path = tmp_path / "cat.yaml"
    path.write_text(yaml.safe_dump({"software": {
        "wrapper": {"windows": {"choco": "wrapper"}},                    # unknown Windows: false
        "embedded": {"windows": {"choco": "embedded"}, "offline": True},
        "flagged": {"windows": {"choco": "flagged"}, "offline": False},
        "linuxonly": {"linux": {"apt": ["x"]}},                          # apt cache: true
    }}))
    monkeypatch.setenv("TN_SOFTWARE_CATALOGUE", str(path))
    flags = {e["name"]: e["offline"] for e in _get(client)["software"]}
    assert flags == {"wrapper": False, "embedded": True, "flagged": False, "linuxonly": True}


def test_real_catalogue_offline_flags_match_content_choco(client):
    """Every content/choco package is marked offline, and every Windows entry says
    explicitly whether it is offline-ready (no silent defaults in the shipped file)."""
    from app.routers.software_catalogue import _REPO_COPY

    doc = yaml.safe_load(_REPO_COPY.read_text(encoding="utf-8"))
    by_name = {e["name"]: e for e in _get(client)["software"]}
    internalized = {p.stem for p in (_REPO_COPY.parents[1] / "choco").glob("*.yaml")}
    assert internalized, "content/choco has no definitions"
    choco_ids = {}
    for name, entry in doc["software"].items():
        if (entry or {}).get("windows", {}).get("choco"):
            assert isinstance(entry.get("offline"), bool), f"{name}: Windows entry without an offline flag"
            choco_ids[entry["windows"]["choco"].lower()] = name
    for pkg in internalized:
        assert pkg in choco_ids, f"content/choco/{pkg}.yaml is not a catalogue Chocolatey id"
        assert by_name[choco_ids[pkg]]["offline"] is True


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
    from app.routers.software_catalogue import router

    # The router's own routes: since FastAPI 0.13x app.routes holds an include wrapper,
    # not the APIRoutes (tests/api/test_auth_coverage_guard.py walks the app for that).
    route = next(r for r in router.routes if isinstance(r, APIRoute) and r.path == "/software-catalogue")
    assert route.methods == {"GET"}
    assert any(d.call is get_current_user for d in route.dependant.dependencies)
