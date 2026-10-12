"""The API's vSphere connection backend against vcsim, govmomi's vCenter simulator (v0.56.0).

vcsim has no ``/api`` (``POST /api/session`` is 404) and, of the legacy ``/rest`` API, only
the session and a few non-inventory services: no ``/rest/vcenter/host`` or
``/rest/appliance/system/version``. So against it the connection test proves what it can:
the port is honoured, the backend falls back to the legacy login, and the session it made
is deleted again. Discovery must report that this vCenter lists no hosts, not record none.

    docker compose -p tn-vcsim -f infra/platform/docker/compose.vcsim.yml up -d --wait
    VCSIM_URL=https://127.0.0.1:8989 .venv/bin/python -m pytest -m vcsim tests/api/test_vsphere_backend_vcsim.py

Skipped when nothing answers at VCSIM_URL (or VSPHERE_URL).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.vcsim]

URL = os.getenv("VCSIM_URL") or os.getenv("VSPHERE_URL") or "https://127.0.0.1:8989"


@pytest.fixture(scope="module")
def vcsim():
    try:
        r = httpx.get(f"{URL}/about", verify=False, timeout=3)
    except httpx.HTTPError:
        pytest.skip(f"no vcsim at {URL}")
    if r.status_code != 200:
        pytest.skip(f"no vcsim at {URL}")
    u = urlparse(URL)
    return u.hostname, u.port or 443


class _DB:
    def __init__(self):
        self.added = []

    def commit(self):
        pass

    def add(self, obj):
        self.added.append(obj)


def _conn(host, port):
    # host and port separately, as the connection row stores them
    return SimpleNamespace(host=host, port=port, username="user", password_encrypted="pass", verify_ssl=False,
                           is_active=None, last_seen_at=None)


def _sessions(monkeypatch, vsphere) -> list[tuple[str, str]]:
    """Record every session the backend creates and deletes (wrapping, not replacing, httpx)."""
    seen: list[tuple[str, str]] = []
    real_send = httpx.Client.send

    def send(self, request, **kw):
        resp = real_send(self, request, **kw)
        if request.url.path.endswith("/session"):
            seen.append((request.method, f"{request.url.path} {resp.status_code}"))
        return resp

    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setattr(vsphere, "unseal", lambda stored: stored)
    return seen


def test_connection_test_falls_back_to_rest_and_logs_out(vcsim, monkeypatch) -> None:
    from app.hypervisor_backends import vsphere

    seen = _sessions(monkeypatch, vsphere)
    conn = _conn(*vcsim)
    result = vsphere.check_connection(conn, _DB())

    assert result.success, result.message
    assert "legacy /rest" in result.message
    assert conn.is_active is True
    assert seen == [
        ("POST", "/api/session 404"),
        ("POST", "/rest/com/vmware/cis/session 200"),
        ("DELETE", "/rest/com/vmware/cis/session 200"),
    ]


def test_discovery_reports_the_missing_host_list(vcsim, monkeypatch) -> None:
    from app.hypervisor_backends import vsphere

    seen = _sessions(monkeypatch, vsphere)
    db = _DB()
    out = vsphere.discover("conn-1", _conn(*vcsim), db)

    assert out["nodes_discovered"] == 0
    assert "does not list hosts" in out["message"]
    assert db.added == []
    assert seen[-1] == ("DELETE", "/rest/com/vmware/cis/session 200")


def test_wrong_port_does_not_reach_vcsim(vcsim, monkeypatch) -> None:
    from app.hypervisor_backends import vsphere

    monkeypatch.setattr(vsphere, "unseal", lambda stored: stored)
    host, port = vcsim
    result = vsphere.check_connection(_conn(host, 443 if port != 443 else 8443), _DB())
    assert not result.success
